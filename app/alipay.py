"""دعم تحميل فيديوهات Alipay (روابط المشاركة من نوع https://ur.lepudding.com/xxxx).

الطريقة:
1) رابط المشاركة المختصر يسوي Redirect -> نستخرج contentId من الرابط النهائي
   (او من الـ scheme / الـ HTML اذا لزم).
2) POST لـ Alipay webgw API بالـ contentId -> resultObj.data[0].video.vid = رابط الفيديو.
3) ننزل الفيديو لمجلد التحميل المؤقت بنفس صيغة أسماء باقي المنصات، والبوت الرئيسي
   يكمل (فحص الحجم، الطابور، الرفع، التنظيف) بنفس النظام الموجود.

كل دوال هذا الملف متزامنة (blocking) وتنادى عبر asyncio.to_thread من downloader.py.
"""

import os
import re
import time
import uuid
import logging
from urllib.parse import unquote, urljoin

import requests

from . import config

logger = logging.getLogger("alipay")

ALIPAY_PATTERN = re.compile(r"(https?://)?(www\.)?ur\.lepudding\.com/\S+", re.IGNORECASE)

API_URL = (
    "https://webgw-internet.alipay.com/contentservice/"
    "com.alipay.sofa.function.SOFAFunction/apply/"
    "myjf.yuyan.contentservice.default.ContentWebGWController.list"
)
API_HEADERS = {
    "x-webgw-appId": "180020010001266490",
    "x-webgw-version": "2.0",
    "Content-Type": "application/json",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_REDIRECTS = 10
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 30
DOWNLOAD_READ_TIMEOUT = 60
MAX_ATTEMPTS = 3          # عدد المحاولات الكلي (محدود، مو لانهائي)
RETRY_BACKOFF_SEC = 2     # 2 ثم 4 ثواني بين المحاولات
CHUNK_SIZE = 1024 * 256

_CONTENT_ID_RE = re.compile(r"contentId[\"']?\s*[=:]\s*[\"']?([0-9A-Za-z_\-]{8,64})")


class AlipayError(Exception):
    """خطأ Alipay. kind يحدد رسالة المستخدم:
    invalid_link | unavailable | api | download
    message_key = مفتاح الرسالة بنظام الرسائل بـ db.py (يستخدمه bot.py)."""

    _KEYS = {
        "invalid_link": "alipay_invalid_link",
        "unavailable": "alipay_unavailable",
        "api": "alipay_api_error",
        "download": "alipay_download_failed",
    }

    def __init__(self, kind: str, detail: str):
        super().__init__(f"Alipay [{kind}]: {detail}")
        self.kind = kind
        self.detail = detail
        self.message_key = self._KEYS.get(kind, "alipay_download_failed")


class _Retryable(Exception):
    """خطأ مؤقت (timeout / اتصال / 5xx) يستحق إعادة المحاولة."""


class _StaleUrl(Exception):
    """رابط الفيديو نفسه ما عاد يصلح (مرفوض 4xx / انتهت صلاحيته / يرجع صفحة خطأ بدل ميديا).
    إعادة المحاولة بنفس الرابط ما تفيد - الحل نجيب رابط جديد من الـ API."""


def detect(text: str) -> bool:
    return bool(ALIPAY_PATTERN.search(text))


def _with_retry(fn, what: str):
    """ينفذ fn بحد أقصى MAX_ATTEMPTS محاولات، يعيد المحاولة فقط عند _Retryable."""
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except _Retryable as e:
            last = e
            logger.warning("alipay %s failed (attempt %d/%d): %s", what, attempt, MAX_ATTEMPTS, e)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
    raise last


# ==================== 1) استخراج contentId ====================

def _find_content_id(text: str) -> str | None:
    """يبحث عن contentId بنص (يفك الترميز %XX لين 3 مرات لأن الـ scheme يكون متداخل)."""
    if not text:
        return None
    candidate = text
    for _ in range(4):
        m = _CONTENT_ID_RE.search(candidate)
        if m:
            return m.group(1)
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    return None


def extract_content_id(url: str) -> str:
    """يتبع الـ redirects يدوياً (حتى نقدر نقرأ Location حتى لو كان scheme مثل alipays://
    اللي مكتبة requests ما تتبعه) ويستخرج contentId من الرابط النهائي، وإذا ما لكاه
    يبحث داخل الـ HTML (fallback)."""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    def _run():
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        current = url
        body_text = ""
        for _ in range(MAX_REDIRECTS):
            try:
                resp = session.get(
                    current, allow_redirects=False, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                raise _Retryable(f"redirect request failed: {e}")

            if resp.status_code >= 500:
                raise _Retryable(f"redirect HTTP {resp.status_code}")

            # contentId ممكن يكون بنفس الرابط الحالي قبل لا نكمل
            cid = _find_content_id(current)
            if cid:
                return cid

            location = resp.headers.get("Location")
            if resp.is_redirect and location:
                cid = _find_content_id(location)
                if cid:
                    return cid
                if not location.lower().startswith(("http://", "https://")):
                    # scheme غير http (مثل alipays://) - ما نكدر نتبعه، وما لكينا فيه contentId
                    if "://" in location:
                        raise AlipayError("invalid_link", f"redirect to non-http scheme without contentId: {location[:120]}")
                    location = urljoin(current, location)
                current = location
                continue

            body_text = resp.text or ""
            break

        cid = _find_content_id(body_text)
        if cid:
            return cid
        return None

    try:
        cid = _with_retry(_run, "resolve redirect")
    except _Retryable as e:
        raise AlipayError("api", f"could not resolve share link after {MAX_ATTEMPTS} attempts: {e}")
    if not cid:
        raise AlipayError("invalid_link", f"contentId not found for {url[:200]}")
    return cid


# ==================== 2) Alipay API ====================

def _pick(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, "", {}):
            return v
    return None


def _cover_url(*candidates) -> str | None:
    """يطلع رابط الصورة (string) من coverPic. Alipay ممكن يرجعه string او object مثل
    {"url": "...", "width": 656, "height": 492} (او قائمة منها) - نرجع الرابط بس."""
    for value in candidates:
        if isinstance(value, (list, tuple)):
            value = _cover_url(*value)
        elif isinstance(value, dict):
            value = _cover_url(*(value.get(k) for k in ("url", "imageUrl", "imgUrl", "src")))
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("//"):
                value = "https:" + value
            if value.lower().startswith(("http://", "https://")):
                return value
    return None


def _author_name(item: dict, video: dict) -> str:
    author = _pick(item, "author", "authorInfo", "user", "userInfo")
    if isinstance(author, dict):
        name = _pick(author, "nickName", "nickname", "name", "userName")
        if name:
            return str(name)
    elif isinstance(author, str) and author.strip():
        return author.strip()
    for src in (item, video):
        name = _pick(src, "nickName", "nickname", "authorName", "authorNickName")
        if name:
            return str(name)
    return ""


def parse_api_response(data: dict) -> dict:
    """يحلل رد الـ API ويرجع {video_url, meta}. يرفع AlipayError بحالات الفشل."""
    if not isinstance(data, dict):
        raise AlipayError("api", "response is not a JSON object")

    result = data.get("resultObj")
    if not isinstance(result, dict):
        raise AlipayError("api", f"missing resultObj (keys={list(data)[:8]})")

    items = result.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        # الـ API رد بنجاح بس ما اكو محتوى = الفيديو محذوف / غير متوفر
        raise AlipayError("unavailable", "no content item in resultObj.data")

    item = items[0]
    video = item.get("video")
    if not isinstance(video, dict):
        raise AlipayError("unavailable", "content has no video object")

    vid = video.get("vid")
    if not isinstance(vid, str) or not vid.strip():
        raise AlipayError("unavailable", "video.vid is missing")
    vid = vid.strip()
    if not vid.lower().startswith(("http://", "https://")):
        raise AlipayError("api", f"video.vid is not a URL: {vid[:120]}")

    duration = _pick(video, "duration") or _pick(item, "duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    title = str(_pick(item, "title", "content", "desc", "description") or _pick(video, "title") or "").strip()
    meta = {
        "uploader": _author_name(item, video),
        "uploader_id": "",
        "description": title,
        "webpage_url": "",
        "duration": duration,
        "thumbnail": _cover_url(
            _pick(item, "coverPic", "cover"), _pick(video, "coverPic", "cover"),
        ),
        "width": _pick(video, "widthRatio"),
        "height": _pick(video, "heightRatio"),
    }
    return {"video_url": vid, "meta": meta}


def fetch_content(content_id: str) -> dict:
    """POST لـ Alipay API (مع retry محدود للأخطاء المؤقتة) ويرجع {video_url, meta}."""

    def _call():
        try:
            resp = requests.post(
                API_URL,
                json={"contentId": content_id},
                headers={**API_HEADERS, "User-Agent": USER_AGENT},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            raise _Retryable(f"API request failed: {e}")

        if resp.status_code >= 500:
            raise _Retryable(f"API HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise AlipayError("api", f"API HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise AlipayError("api", f"API returned non-JSON: {resp.text[:120]!r}")

    try:
        data = _with_retry(_call, "API call")
    except _Retryable as e:
        raise AlipayError("api", f"API unavailable after {MAX_ATTEMPTS} attempts: {e}")

    parsed = parse_api_response(data)
    parsed["content_id"] = content_id
    return parsed


# كاش قصير العمر لنتيجة الاستخراج: التحقق من الرابط (افتراضياً مفعّل) + المعاينة + التحميل كلهم
# يحتاجون نفس النتيجة، فما نضرب Alipay 2-3 مرات لنفس الرابط خلال ثواني. ننخزن النجاح بس.
_INFO_TTL_SEC = 120
_INFO_CACHE_MAX = 100
_info_cache: dict[str, tuple[float, dict]] = {}


def invalidate(url: str):
    """يحذف نتيجة الرابط من الكاش (مثلاً بعد فشل التنزيل، حتى إعادة المحاولة تجيب رابط جديد)."""
    _info_cache.pop(url, None)


def fetch_info(url: str) -> dict:
    """الخطوة الكاملة قبل التحميل: رابط المشاركة -> contentId -> API.
    يرجع {video_url, meta, content_id}."""
    now = time.time()
    hit = _info_cache.get(url)
    if hit and now - hit[0] < _INFO_TTL_SEC:
        cached = hit[1]
        return {**cached, "meta": dict(cached["meta"])}

    content_id = extract_content_id(url)
    info = fetch_content(content_id)
    info["meta"]["webpage_url"] = url

    if len(_info_cache) >= _INFO_CACHE_MAX:
        for k in [k for k, (ts, _) in _info_cache.items() if now - ts >= _INFO_TTL_SEC]:
            _info_cache.pop(k, None)
        if len(_info_cache) >= _INFO_CACHE_MAX:
            _info_cache.clear()
    _info_cache[url] = (now, info)
    return {**info, "meta": dict(info["meta"])}


# ==================== 3) التنزيل ====================

def _download_file(video_url: str, dest: str):
    """ينزل الفيديو بالـ streaming لـ dest (محاولة واحدة، اللي يستدعيها يطبق retry)."""
    try:
        with requests.get(
            video_url,
            stream=True,
            headers={"User-Agent": USER_AGENT},
            timeout=(CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
        ) as resp:
            if resp.status_code >= 500:
                raise _Retryable(f"video HTTP {resp.status_code}")
            if resp.status_code in (408, 429):
                # ضغط/مهلة مؤقتة من السيرفر، مو مشكلة بالرابط - نعيد المحاولة بنفس الرابط بدون refresh
                raise _Retryable(f"video HTTP {resp.status_code}")
            if resp.status_code != 200:
                # 400/401/403/404/410...: الرابط مرفوض او انتهت صلاحيته
                raise _StaleUrl(f"video HTTP {resp.status_code}")

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if any(t in ctype for t in ("text/html", "application/json", "mpegurl")):
                raise _StaleUrl(f"unexpected Content-Type: {ctype}")

            written = 0
            first_chunk = b""
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    if not first_chunk:
                        first_chunk = chunk[:16]
                    f.write(chunk)
                    written += len(chunk)
    except (requests.Timeout, requests.ConnectionError) as e:
        raise _Retryable(f"video download interrupted: {e}")

    if written == 0:
        raise _Retryable("empty video file")
    if first_chunk.startswith((b"#EXTM3U", b"<", b"{")):
        raise _StaleUrl("video URL did not return a media file")


def _refresh_video_url(info: dict, reason: str) -> str:
    """يجيب video.vid جديد من الـ API (يتجاوز الكاش) ويحدّث info والكاش.
    يرفع AlipayError اذا ما نكدر نجدد (مثلاً الفيديو انحذف)."""
    content_id = info.get("content_id")
    share_url = (info.get("meta") or {}).get("webpage_url")
    logger.warning("alipay video URL rejected (%s) - refreshing from API", reason)

    if content_id:
        fresh = fetch_content(content_id)          # ما يمر على الكاش، ويوفر خطوة الـ redirect
    elif share_url:
        invalidate(share_url)
        fresh = fetch_info(share_url)
    else:
        raise AlipayError("download", f"{reason} (cannot refresh: no contentId/share URL)")

    info["video_url"] = fresh["video_url"]
    if share_url:
        # الكاش يمسك الرابط الجديد حتى أي طلب لاحق لنفس الرابط ما يرجع للقديم
        _info_cache[share_url] = (time.time(), {
            "video_url": fresh["video_url"],
            "meta": dict(info.get("meta") or fresh["meta"]),
            "content_id": content_id or fresh.get("content_id"),
        })
    return fresh["video_url"]


def download(info: dict) -> list[str]:
    """ينزل الفيديو من info["video_url"] لمجلد التحميل المؤقت (نفس صيغة اسم باقي المنصات:
    {uuid}_0.mp4) ويرجع قائمة الملفات. عند الفشل ينظف أي ملف جزئي قبل رفع الخطأ.

    - اخطاء الاتصال المؤقتة (timeout / اتصال / 5xx): نفس retry السابق بنفس الرابط، بدون refresh.
    - اذا الرابط نفسه انرفض/انتهت صلاحيته (4xx / صفحة خطأ بدل ميديا): نجيب video.vid جديد من
      الـ API مرة وحدة بس ونحاول فيه. اذا رجع نفس الرابط القديم ما نعيد المحاولة بلا فايدة."""
    prefix = str(uuid.uuid4())
    dest = os.path.join(config.DOWNLOAD_DIR, f"{prefix}_0.mp4")

    def _cleanup():
        try:
            os.remove(dest)
        except OSError:
            pass

    def _attempt():
        try:
            _download_file(info["video_url"], dest)
        except BaseException:
            _cleanup()
            raise

    refreshed = False
    while True:
        try:
            _with_retry(_attempt, "video download")
            return [dest]
        except _Retryable as e:
            raise AlipayError("download", f"download failed after {MAX_ATTEMPTS} attempts: {e}")
        except _StaleUrl as e:
            if refreshed:
                raise AlipayError("download", f"{e} (still failing after refreshing the video URL)")
            refreshed = True
            old_url = info["video_url"]
            new_url = _refresh_video_url(info, str(e))
            if new_url == old_url:
                raise AlipayError("download", f"{e} (API returned the same video URL)")
        except AlipayError:
            raise
        except OSError as e:
            raise AlipayError("download", f"file error: {e}")
