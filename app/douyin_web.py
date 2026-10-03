"""Lightweight Douyin webpage fallback.

Used only after yt-dlp fails for Douyin (both for link verification and for the actual
download). It avoids launching a browser: fetches the public Douyin page with the
configured cookies, extracts embedded router/render data, finds a direct video URL, and
downloads it with requests.

V5.1: the downloaded response is validated before it is kept (Content-Type + MP4 magic
bytes + size/truncation checks), the size limit is the bot's own admin setting, and every
step writes safe diagnostics to the Render logs with the "[DOUYIN-WEB]" prefix (never
cookie names/values, headers, query strings, tokens or response bodies).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import traceback
import uuid
from http.cookiejar import MozillaCookieJar
from urllib.parse import unquote, urlparse

import requests

from . import config, db

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_SCRIPT_RE = re.compile(
    r"<script[^>]*?(?:id=[\"'](?P<id>[^\"']+)[\"'][^>]*)?>(?P<body>.*?)</script>",
    re.I | re.S,
)


def _load_cookies(path: str | None) -> MozillaCookieJar:
    jar = MozillaCookieJar()
    if not path or not os.path.isfile(path):
        return jar
    try:
        jar.load(path, ignore_discard=True, ignore_expires=False)
    except Exception as e:  # ملف كوكيز تالف: نكمل بدون كوكيز بدل ما نفشل، ونسجل السبب
        _log(logging.WARNING, f"cookie file could not be loaded ({type(e).__name__}); continuing without cookies")
        return MozillaCookieJar()
    return jar


def _decode_json_candidate(value: str):
    value = html.unescape(value.strip())
    # RENDER_DATA is commonly URL encoded.
    candidates = [value]
    try:
        decoded = unquote(value)
        if decoded != value:
            candidates.append(decoded)
    except Exception:
        pass
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _embedded_objects(page: str):
    """Yield JSON objects from known Douyin script containers."""
    for m in _SCRIPT_RE.finditer(page):
        sid = (m.group("id") or "").upper()
        body = m.group("body")
        if sid in {"RENDER_DATA", "ROUTER_DATA", "__ROUTER_DATA__"}:
            obj = _decode_json_candidate(body)
            if obj is not None:
                yield obj

    # Some page versions put the data in a JS assignment rather than a script id.
    for marker in ("_ROUTER_DATA", "RENDER_DATA"):
        pos = page.find(marker)
        if pos < 0:
            continue
        tail = page[pos:pos + 2_000_000]
        # Try the first balanced JSON object after the marker.
        start = tail.find("{")
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        end = None
        for i in range(start, len(tail)):
            ch = tail[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            obj = _decode_json_candidate(tail[start:end])
            if obj is not None:
                yield obj


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _first_url(value):
    if isinstance(value, dict):
        urls = value.get("url_list") or value.get("urlList") or value.get("url")
    else:
        urls = value
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list):
        return None
    for u in urls:
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            return html.unescape(unquote(u))
    return None


def _find_video_data(objects):
    """Return (direct_url, metadata) from embedded Douyin JSON."""
    best = None
    for root in objects:
        for d in _walk(root):
            # Normal aweme structure: video.play_addr / video.download_addr.
            video = d.get("video") if isinstance(d, dict) else None
            if not isinstance(video, dict):
                continue
            for key in ("play_addr", "playAddr", "download_addr", "downloadAddr"):
                u = _first_url(video.get(key))
                if not u:
                    continue
                # Prefer the no-watermark endpoint when the site embeds playwm.
                u = u.replace("/playwm/", "/play/")
                u = u.replace("playwm", "play")
                author = d.get("author") or {}
                if not isinstance(author, dict):
                    author = {}
                meta = {
                    "uploader": author.get("nickname") or d.get("nickname") or "",
                    "uploader_id": author.get("unique_id") or author.get("sec_uid") or "",
                    "description": (d.get("desc") or d.get("title") or "").strip(),
                    "webpage_url": d.get("share_url") or "",
                }
                return u, meta
            best = best or (video, d)
    return (None, None)


# ==================== V5.1: logging / validation / size limit ====================

_LOG = "[DOUYIN-WEB]"
MIN_VIDEO_BYTES = 4096
_MP4_ATOMS = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"styp", b"pnot")
_OK_CONTENT_TYPES = ("application/octet-stream", "binary/octet-stream", "application/mp4")


class DouyinWebError(RuntimeError):
    """فشل بمرحلة محددة من الـ Web Fallback (stage يظهر بالـ logs وبنص الخطأ)."""

    def __init__(self, stage: str, message: str):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage


def _scrub(text: str) -> str:
    """حماية اضافية: يخفي اي سر معروف للبوت لو ظهر بالغلط بنص الخطأ."""
    for name in ("BOT_TOKEN", "TIKHUB_API_KEY", "MONGO_URI"):
        secret = getattr(config, name, "") or ""
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


def _log(level: int, text: str):
    try:
        logger.log(level, _scrub(f"{_LOG} {text}"))
    except Exception:
        pass


def _safe_url(u: str) -> str:
    """scheme + host + path فقط (بدون query/fragment) حتى ما تظهر معرفات/توكنات بالـ logs."""
    try:
        p = urlparse(u or "")
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return "?"


def _max_file_size() -> tuple[int, int]:
    """نفس إعداد حد الحجم اللي يستخدمه البوت (bot._max_file_size_mb): قيمة /admin
    "max_file_size_mb" وترجع لـ MAX_FILE_SIZE_MB البيئية كافتراضي. يرجع (bytes, mb)."""
    try:
        mb = int(db.get_setting("max_file_size_mb", config.MAX_FILE_SIZE_MB))
    except Exception:
        mb = int(config.MAX_FILE_SIZE_MB)
    return mb * 1024 * 1024, mb


def _looks_like_video(head: bytes) -> bool:
    """يتأكد من بداية الملف: MP4/MOV (atom type بعد اول 4 بايت) او WebM/Matroska."""
    if len(head) < 12:
        return False
    if head[4:8] in _MP4_ATOMS:
        return True
    return head[:4] == b"\x1a\x45\xdf\xa3"


def _content_type_ok(ctype: str) -> bool:
    base = (ctype or "").split(";")[0].strip().lower()
    if not base:
        return True  # ما اكو Content-Type: نعتمد على فحص بداية الملف
    return base.startswith("video/") or base in _OK_CONTENT_TYPES


def _page_diagnostics(page: str, objects: list) -> str:
    """وصف آمن لمحتوى الصفحة (علامات وأعداد بس، بدون اي جزء من النص ماعدا عنوان الصفحة)."""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:80]
    video_dicts = 0
    for root in objects:
        for d in _walk(root):
            if isinstance(d.get("video"), dict):
                video_dicts += 1
    low = page.lower()
    return (
        f"page_chars={len(page)} title={title!r} has_ROUTER_DATA={'_ROUTER_DATA' in page} "
        f"has_RENDER_DATA={'RENDER_DATA' in page} captcha_word={'captcha' in low} "
        f"json_objects={len(objects)} dicts_with_video={video_dicts}"
    )


def _fetch_page(url: str, cookies_path: str | None):
    jar = _load_cookies(cookies_path)
    session = requests.Session()
    session.cookies.update(jar)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.douyin.com/",
        "Connection": "keep-alive",
    }
    t0 = time.time()
    try:
        response = session.get(url, headers=headers, timeout=(20, 40), allow_redirects=True)
    except requests.RequestException as e:
        raise DouyinWebError("page_fetch", f"{type(e).__name__}: {e}")
    _log(
        logging.INFO,
        f"page fetched status={response.status_code} final_url={_safe_url(response.url)} "
        f"redirects={len(response.history)} content_type={response.headers.get('Content-Type')!r} "
        f"chars={len(response.text or '')} cookies_loaded={len(jar)} elapsed_ms={int((time.time() - t0) * 1000)}",
    )
    if response.status_code >= 400:
        raise DouyinWebError("page_fetch", f"HTTP {response.status_code}")
    return response.text, response.url or "", session.cookies


def _resolve(url: str):
    """صفحة دويين -> (رابط الفيديو المباشر, metadata, الرابط النهائي, كوكيز الجلسة)."""
    page, final_url, cookies = _fetch_page(url, config.DOUYIN_COOKIES_FILE)
    objects = list(_embedded_objects(page))
    direct_url, meta = _find_video_data(objects)
    _log(logging.INFO, "parse " + _page_diagnostics(page, objects))
    if not direct_url:
        raise DouyinWebError("parse", "Douyin webpage did not expose an embedded video URL")
    _log(
        logging.INFO,
        f"video url found host={urlparse(direct_url).netloc} path={urlparse(direct_url).path[:60]} "
        f"uploader_present={bool(meta.get('uploader'))} description_present={bool(meta.get('description'))}",
    )
    return direct_url, meta, final_url, cookies


def _download_direct(url: str, output_path: str, cookies, referer: str, max_bytes: int, max_mb: int):
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer or "https://www.douyin.com/",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=(20, 180), allow_redirects=True)
    except requests.RequestException as e:
        raise DouyinWebError("video_request", f"{type(e).__name__}: {e}")
    with r:
        ctype = r.headers.get("Content-Type") or ""
        try:
            total = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        _log(
            logging.INFO,
            f"video response status={r.status_code} host={urlparse(r.url or url).netloc} "
            f"content_type={ctype!r} content_length={total} max_mb={max_mb}",
        )
        if r.status_code >= 400:
            raise DouyinWebError("video_request", f"HTTP {r.status_code}")
        if not _content_type_ok(ctype):
            raise DouyinWebError("validate", f"unexpected Content-Type {ctype!r} (not a video)")
        if total and total > max_bytes:
            raise DouyinWebError("size", f"file is {total} bytes, over the bot limit of {max_mb} MB")

        written = 0
        head = b""
        magic_checked = False
        try:
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if not magic_checked:
                        head += chunk[: 16 - len(head)] if len(head) < 16 else b""
                        if len(head) >= 12:
                            magic_checked = True
                            if not _looks_like_video(head):
                                raise DouyinWebError(
                                    "validate", f"response is not a video (first bytes: {head[:12].hex()})"
                                )
                    written += len(chunk)
                    if written > max_bytes:
                        raise DouyinWebError("size", f"download exceeded the bot limit of {max_mb} MB")
                    f.write(chunk)
        except requests.RequestException as e:
            raise DouyinWebError("video_stream", f"{type(e).__name__}: {e}")

        if not magic_checked and not _looks_like_video(head):
            raise DouyinWebError("validate", f"response too short to be a video ({written} bytes)")
        if written < MIN_VIDEO_BYTES:
            raise DouyinWebError("validate", f"video too small ({written} bytes)")
        if total and written < total:
            raise DouyinWebError("validate", f"truncated download ({written} of {total} bytes)")
        _log(
            logging.INFO,
            f"video downloaded bytes={written} magic={head[:8].hex()} elapsed_ms={int((time.time() - t0) * 1000)}",
        )


def verify(url: str) -> bool:
    """يتحقق ان صفحة دويين تعرض رابط فيديو (بدون تحميل الملف) - يستخدمه verify_link لما yt-dlp يفشل.
    يرفع DouyinWebError اذا ما لقى فيديو."""
    _log(logging.INFO, f"verify start url={_safe_url(url)}")
    try:
        _resolve(url)
    except Exception as e:
        _log(logging.ERROR, f"verify FAILED stage={getattr(e, 'stage', 'unexpected')} error={e}")
        raise
    _log(logging.INFO, "verify OK (embedded video URL found)")
    return True


def download(url: str) -> tuple[list[str], dict]:
    """Try downloading one Douyin video from the rendered webpage.

    Raises on any failure so the caller can continue to the existing paid fallback.
    """
    max_bytes, max_mb = _max_file_size()
    _log(logging.INFO, f"download start url={_safe_url(url)} max_mb={max_mb}")
    output_path = None
    try:
        direct_url, meta, final_url, cookies = _resolve(url)
        prefix = str(uuid.uuid4())
        output_path = os.path.join(config.DOWNLOAD_DIR, f"{prefix}_1.mp4")
        _download_direct(direct_url, output_path, cookies, final_url, max_bytes, max_mb)
    except Exception as e:
        if output_path:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
        _log(
            logging.ERROR,
            f"download FAILED stage={getattr(e, 'stage', 'unexpected')} error={type(e).__name__}: {e}\n"
            f"{traceback.format_exc()}",
        )
        raise

    if not meta.get("webpage_url"):
        meta["webpage_url"] = final_url or url
    _log(logging.INFO, f"download SUCCESS file_bytes={os.path.getsize(output_path)}")
    return [output_path], meta
