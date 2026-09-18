"""
تكامل بسيط مع TikHub API:
- جلب رصيد الحساب (استهلاك) بالإحصائيات
- تحميل فيديو دويين كطريقة بديلة (fallback) لما yt-dlp يفشل (مشكلة الكوكيز)
"""
import os
import uuid
import logging
import requests

from . import config

logger = logging.getLogger("tikhub")

BASE_URL = "https://api.tikhub.io/api/v1"


def is_configured() -> bool:
    return bool(config.TIKHUB_API_KEY)


def get_usage() -> dict | None:
    """يرجع {'balance': float, 'free_credit': float} او None اذا مو مفعّل او صار خطأ."""
    if not is_configured():
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/tikhub/user/get_user_info",
            headers={"Authorization": f"Bearer {config.TIKHUB_API_KEY}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        user_data = data.get("user_data", {})
        return {
            "balance": user_data.get("balance", 0),
            "free_credit": user_data.get("free_credit", 0),
        }
    except Exception:
        logger.exception("failed to fetch TikHub usage")
        return None


def _tikhub_headers():
    return {"Authorization": f"Bearer {config.TIKHUB_API_KEY}"}


def fetch_douyin_video_detail(share_url: str) -> dict:
    """يستدعي TikHub لجلب تفاصيل فيديو دويين من رابط مشاركة مباشرة (بدون علامة مائية)."""
    resp = requests.get(
        f"{BASE_URL}/douyin/web/fetch_one_video_by_share_url",
        headers=_tikhub_headers(),
        params={"share_url": share_url},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_douyin_media(detail: dict) -> dict:
    """يستخرج رابط تحميل الفيديو الصافي (بدون علامة مائية) ومعلومات صاحب المنشور."""
    data = detail.get("data", {})
    aweme_detail = data.get("aweme_detail", data)  # بعض الاستجابات تجي مباشرة بدون aweme_detail

    video = aweme_detail.get("video", {})
    # نحاول أكثر من مسار محتمل لرابط التحميل الصافي حسب شكل الاستجابة
    play_addr = (
        video.get("play_addr", {})
        or video.get("download_addr", {})
        or video.get("bit_rate", [{}])[0].get("play_addr", {})
    )
    url_list = play_addr.get("url_list", [])
    if not url_list:
        raise ValueError("ماكو رابط تحميل بهذا المنشور")

    author = aweme_detail.get("author", {})

    return {
        "download_url": url_list[0],
        "uploader": author.get("nickname") or "",
        "uploader_id": author.get("unique_id") or author.get("short_id") or "",
        "description": aweme_detail.get("desc") or "",
    }


def download_douyin_via_api(share_url: str) -> tuple[str, dict]:
    """يحمل فيديو دويين كطريقة بديلة عبر TikHub، يرجع مسار الملف المحلي + معلومات صاحب المنشور."""
    if not is_configured():
        raise RuntimeError("TikHub غير مفعّل (ناقص TIKHUB_API_KEY)")

    detail = fetch_douyin_video_detail(share_url)
    media = _extract_douyin_media(detail)

    path = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_douyin_api.mp4")
    with requests.get(media["download_url"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

    meta = {
        "uploader": media["uploader"],
        "uploader_id": media["uploader_id"],
        "description": media["description"],
        "webpage_url": share_url,
    }
    return path, meta


def fetch_rednote_video_detail(share_text: str) -> dict:
    """يستدعي TikHub App V2 لجلب تفاصيل فيديو RedNote من نص/رابط مشاركة مباشر."""
    resp = requests.get(
        f"{BASE_URL}/xiaohongshu/app_v2/get_video_note_detail",
        headers=_tikhub_headers(),
        params={"share_text": share_text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_rednote_media(detail: dict) -> dict:
    """يستخرج رابط تحميل الفيديو الصافي ومعلومات صاحب المنشور من استجابة RedNote."""
    data = detail.get("data", {})
    note = data.get("note", data)  # بعض الاستجابات تجي مباشرة بدون مفتاح note

    video = note.get("video", {})
    download_url = video.get("masterUrl") or video.get("master_url")
    if not download_url:
        backup_urls = video.get("backupUrls") or video.get("backup_urls") or []
        download_url = backup_urls[0] if backup_urls else None
    if not download_url:
        raise ValueError("ماكو رابط تحميل بهذا المنشور")

    user = note.get("user", {})

    return {
        "download_url": download_url,
        "uploader": user.get("nickname") or "",
        "uploader_id": user.get("userId") or user.get("user_id") or "",
        "description": note.get("desc") or note.get("title") or "",
    }


def download_rednote_via_api(share_url: str) -> tuple[str, dict]:
    """يحمل فيديو RedNote كطريقة بديلة عبر TikHub، يرجع مسار الملف المحلي + معلومات صاحب المنشور."""
    if not is_configured():
        raise RuntimeError("TikHub غير مفعّل (ناقص TIKHUB_API_KEY)")

    detail = fetch_rednote_video_detail(share_url)
    media = _extract_rednote_media(detail)

    path = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_rednote_api.mp4")
    with requests.get(media["download_url"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

    meta = {
        "uploader": media["uploader"],
        "uploader_id": media["uploader_id"],
        "description": media["description"],
        "webpage_url": share_url,
    }
    return path, meta


def get_daily_usage() -> dict | None:
    """يرجع استهلاك اليوم الحالي من TikHub (عدد الطلبات والتكلفة)، او None اذا فشل."""
    if not is_configured():
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/user/get_user_daily_usage",
            headers=_tikhub_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})
    except Exception:
        logger.exception("failed to fetch TikHub daily usage")
        return None
