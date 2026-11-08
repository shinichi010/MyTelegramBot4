"""
تكامل تحميل فيديوهات ويشات (视频号 / WeChat Channels).

الفكرة:
1. نستدعي TikHub (fetch_video_detail) بالرابط اللي أرسله المستخدم (share_url)
   -> يرجعلنا رابط الفيديو المشفر + decode_key + معلومات صاحب المنشور.
2. ننزل الفيديو المشفر من الرابط اللي رجع.
3. نرسله لخدمة فك التشفير المنفصلة (Render ثانية، Docker image evil0ctal/wechat-decrypt-api)
   مع الـ decode_key، وتترجعلنا نسخة مفكوكة قابلة للتشغيل.
4. نمرر الفيديو المفكوك بنفس مسار الإرسال المستخدم بـ X ودويين.
"""
import os
import re
import time
import uuid
import logging

import requests

from . import config

logger = logging.getLogger("wechat")

WECHAT_PATTERN = re.compile(
    r"(https?://)?(www\.)?weixin\.qq\.com/sph/\S+", re.IGNORECASE
)

TIKHUB_BASE = "https://api.tikhub.io/api/v1/wechat_channels/v2"

# إعادة محاولة فك التشفير (بسبب نوم خدمة Render المجانية)
DECRYPT_ATTEMPTS = 3
DECRYPT_TIMEOUT = 90          # ثانية لكل محاولة (الاستيقاظ يأخذ وقت)
DECRYPT_BACKOFF_SEC = 8       # انتظار 8 ثم 16 ثانية بين المحاولات

# إعادة محاولة تنزيل الفيديو المشفر من سيرفر ويشات (wxapp.tc.qq.com): بطيء ويقطع الاتصال أحياناً
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_CONNECT_TIMEOUT = 20
DOWNLOAD_READ_TIMEOUT = 90    # ثانية بين أي دفعتين بيانات (كانت 60)
DOWNLOAD_BACKOFF_SEC = 3      # انتظار 3 ثم 6 ثواني بين المحاولات


def detect(text: str) -> bool:
    return bool(WECHAT_PATTERN.search(text))


def extract_url(text: str) -> str:
    match = WECHAT_PATTERN.search(text)
    return match.group(0) if match else text.strip()


def is_configured() -> bool:
    return bool(config.TIKHUB_API_KEY) and bool(config.WECHAT_DECRYPT_API_URL)


def _tikhub_headers():
    return {"Authorization": f"Bearer {config.TIKHUB_API_KEY}"}


def fetch_video_detail(share_url: str) -> dict:
    """يستدعي TikHub fetch_video_detail (POST) ويرجع البيانات الخام (dict)."""
    resp = requests.post(
        f"{TIKHUB_BASE}/fetch_video_detail",
        headers={**_tikhub_headers(), "Content-Type": "application/json"},
        json={"share_url": share_url, "raw": False},  # raw=false = بنية مبسطة، أسهل للتحميل
        timeout=30,  # التوثيق يحذر: سيرفر ويشات بطيء، لازم مهلة 30 ثانية
    )
    resp.raise_for_status()
    return resp.json()


def _extract_media(detail: dict) -> dict:
    """يستخرج رابط الفيديو المشفر + decode_key + معلومات صاحب المنشور من استجابة TikHub (raw=false)."""
    data = detail.get("data")
    if not data:
        raise ValueError("الاستجابة فاضية - تأكد الرابط صحيح ومتاح")

    media = data.get("media")
    if not media:
        raise ValueError("ماكو ميديا بهذا المنشور (ممكن يكون منشور نصي/صور بس)")

    full_url = media.get("full_url", "")
    decode_key = media.get("decode_key", "")

    if not full_url or not decode_key:
        raise ValueError("الاستجابة ناقصة - ماكو رابط فيديو او decode_key")

    return {
        "download_url": full_url,
        "decode_key": decode_key,
        "uploader": data.get("nickname") or "",
        "uploader_id": data.get("username") or "",
        "description": data.get("title") or "",
    }


class _IncompleteDownload(Exception):
    """الملف المشفر وصل فاضي او ناقص (مؤقت، يستحق إعادة المحاولة)."""


def _remove_quietly(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _is_temporary_download_error(e: Exception) -> bool:
    """أخطاء مؤقتة تستاهل إعادة المحاولة بنفس الرابط: انقطاع/مهلة/تقطيع، او HTTP 5xx/408/429.
    4xx الباقي (403/404...) = الرابط نفسه مرفوض/منتهي، اعادة المحاولة ما تفيد."""
    if isinstance(e, requests.HTTPError):
        code = getattr(e.response, "status_code", 0) or 0
        return code >= 500 or code in (408, 429)
    return isinstance(e, (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                          _IncompleteDownload))


def _download_encrypted(url: str, dest: str):
    """ينزل الفيديو المشفر لـ dest مع إعادة محاولة محدودة للأخطاء المؤقتة. كل محاولة تبدأ من الصفر
    (الملف الجزئي ينحذف) حتى ما يتلخبط المحتوى، وعند الفشل النهائي ما يبقى ملف جزئي بالقرص."""
    last_err = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with requests.get(url, stream=True, timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT)) as r:
                r.raise_for_status()
                try:
                    expected = int(r.headers.get("Content-Length") or 0)
                except ValueError:
                    expected = 0
                written = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
            if written == 0:
                raise _IncompleteDownload("سيرفر ويشات رجع ملف فاضي")
            if expected and written < expected:
                raise _IncompleteDownload(f"التنزيل ناقص ({written} من {expected} بايت)")
            return
        except Exception as e:
            last_err = e
            _remove_quietly(dest)
            if not _is_temporary_download_error(e):
                raise
            logger.warning("wechat download attempt %s/%s failed: %s", attempt, DOWNLOAD_ATTEMPTS, e)
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(DOWNLOAD_BACKOFF_SEC * attempt)
    raise RuntimeError(f"فشل تنزيل فيديو ويشات بعد {DOWNLOAD_ATTEMPTS} محاولات: {last_err}") from last_err


def download_and_decrypt(share_url: str) -> tuple[str, dict]:
    """
    يسوي الدورة الكاملة: يجيب تفاصيل الفيديو من TikHub، ينزل النسخة المشفرة،
    يرسلها لخدمة فك التشفير، ويرجع مسار الملف المفكوك + معلومات صاحب المنشور.
    """
    if not is_configured():
        raise RuntimeError("ميزة ويشات غير مفعّلة (ناقص TIKHUB_API_KEY او WECHAT_DECRYPT_API_URL)")

    detail = fetch_video_detail(share_url)
    media = _extract_media(detail)

    # تنزيل الفيديو المشفر
    encrypted_path = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_wx_encrypted.mp4")
    _download_encrypted(media["download_url"], encrypted_path)

    # إرسال الملف المشفر لخدمة فك التشفير.
    # خدمة Render المجانية تنام بعد ~15 دقيقة، فأول طلب غالباً يفشل/يتأخر (استيقاظ).
    # نعيد المحاولة داخلياً 3 مرات مع انتظار متزايد قبل ما نعتبرها فشل فعلي (حتى ما ينزعج المستخدم).
    decrypted_path = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_wx_decrypted.mp4")
    last_err = None
    try:
        for attempt in range(1, DECRYPT_ATTEMPTS + 1):
            try:
                with open(encrypted_path, "rb") as f:
                    resp = requests.post(
                        f"{config.WECHAT_DECRYPT_API_URL}/api/decrypt",
                        files={"video": f},
                        data={"decode_key": str(media["decode_key"])},
                        timeout=DECRYPT_TIMEOUT,
                    )
                resp.raise_for_status()
                if not resp.content:
                    raise ValueError("خدمة فك التشفير رجعت ملف فاضي")
                with open(decrypted_path, "wb") as f:
                    f.write(resp.content)
                last_err = None
                break
            except Exception as e:
                last_err = e
                logger.warning("wechat decrypt attempt %s/%s failed: %s", attempt, DECRYPT_ATTEMPTS, e)
                if attempt < DECRYPT_ATTEMPTS:
                    time.sleep(DECRYPT_BACKOFF_SEC * attempt)
        if last_err is not None:
            raise last_err
    finally:
        if os.path.exists(encrypted_path):
            os.remove(encrypted_path)

    meta = {
        "uploader": media["uploader"],
        "uploader_id": media["uploader_id"],
        "description": media["description"],
        "webpage_url": share_url,
    }
    return decrypted_path, meta
