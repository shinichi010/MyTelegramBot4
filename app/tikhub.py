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


# أفضلية الترميز: h264 يشتغل بكل أجهزة تيليگرام، h265 احتياط، والباقي (av1/h266) آخر خيار
_CODEC_PRIORITY = ("h264", "h265", "av1", "h266")


def _pick_stream_url(stream) -> str | None:
    """يختار رابط فيديو من قاموس stream (مفاتيحه الترميزات: h264/h265/av1/h266...)."""
    if not isinstance(stream, dict):
        return None
    codecs = list(_CODEC_PRIORITY) + [k for k in stream if k not in _CODEC_PRIORITY]
    for codec in codecs:
        items = stream.get(codec) or []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("master_url") or item.get("masterUrl")
            if not url:
                backups = item.get("backup_urls") or item.get("backupUrls") or []
                url = backups[0] if isinstance(backups, list) and backups else None
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


def _walk_dicts(obj, depth: int = 0):
    """يمشي على كل القواميس داخل الاستجابة (الأعلى أولاً) بعمق محدود."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v, depth + 1)


def _shape_summary(detail) -> str:
    """وصف مختصر لهيكل الاستجابة (مفاتيح فقط، بدون محتوى) - يظهر بتقرير الخطأ للمطور
    حتى لو تغير شكل استجابة TikHub نعرف بالضبط شنو رجع."""
    try:
        data = detail.get("data") if isinstance(detail, dict) else detail
        parts = [f"top={sorted(detail.keys())[:8]}" if isinstance(detail, dict) else f"top={type(detail).__name__}"]
        if isinstance(data, dict):
            parts.append(f"data={sorted(data.keys())[:10]}")
            inner = data.get("data")
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                parts.append(f"data.data[0]={sorted(inner[0].keys())[:14]}")
            elif isinstance(inner, dict):
                parts.append(f"data.data={sorted(inner.keys())[:14]}")
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            parts.append(f"data[0]={sorted(data[0].keys())[:14]}")
        return " | ".join(parts)[:350]
    except Exception:
        return "shape unavailable"


def _extract_rednote_media(detail: dict) -> dict:
    """يستخرج رابط تحميل الفيديو الصافي ومعلومات صاحب المنشور من استجابة RedNote.
    الشكل الحالي لاستجابة TikHub (app_v2): data.data[0].video_info_v2.media.stream.<codec>[0].master_url
    ونبقي الأشكال القديمة (data.note.video...) كاحتياط، مع بحث عام لو تغير الهيكل مرة ثانية."""
    note = None
    download_url = None

    for node in _walk_dicts(detail):
        url = None
        vi = node.get("video_info_v2")
        if isinstance(vi, dict):
            url = _pick_stream_url((vi.get("media") or {}).get("stream"))
        if not url:
            video = node.get("video")
            if isinstance(video, dict):
                # شكل قديم: video.media.stream، او رابط مباشر داخل video
                url = _pick_stream_url((video.get("media") or {}).get("stream"))
                if not url:
                    url = video.get("masterUrl") or video.get("master_url")
                if not url:
                    backups = video.get("backupUrls") or video.get("backup_urls") or []
                    url = backups[0] if isinstance(backups, list) and backups else None
        if isinstance(url, str) and url.startswith("http"):
            note, download_url = node, url
            break

    if not download_url:
        raise ValueError(f"ماكو رابط تحميل بهذا المنشور | {_shape_summary(detail)}")

    user = note.get("user") or note.get("author") or {}
    if not isinstance(user, dict):
        user = {}

    return {
        "download_url": download_url,
        "uploader": user.get("nickname") or user.get("name") or "",
        "uploader_id": user.get("userId") or user.get("user_id") or user.get("userid") or user.get("id") or "",
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
