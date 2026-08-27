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
import uuid
import logging

import requests

from . import config

logger = logging.getLogger("wechat")

WECHAT_PATTERN = re.compile(
    r"(https?://)?(www\.)?weixin\.qq\.com/sph/\S+", re.IGNORECASE
)

TIKHUB_BASE = "https://api.tikhub.io/api/v1/wechat_channels/v2"


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
    """يستدعي TikHub fetch_video_detail ويرجع البيانات الخام (dict)."""
    resp = requests.get(
        f"{TIKHUB_BASE}/fetch_video_detail",
        headers=_tikhub_headers(),
        params={"share_url": share_url},
        timeout=30,  # التوثيق يحذر: سيرفر ويشات بطيء، لازم مهلة 30 ثانية
    )
    resp.raise_for_status()
    return resp.json()


def _extract_media(detail: dict) -> dict:
    """يستخرج رابط الفيديو المشفر + decode_key + معلومات صاحب المنشور من استجابة TikHub."""
    data = detail.get("data", {})
    object_desc = data.get("object_desc", {})
    media_list = object_desc.get("media", [])
    if not media_list:
        raise ValueError("ماكو ميديا بهذا المنشور (ممكن يكون منشور نصي/صور بس)")

    media = media_list[0]
    url = media.get("url", "")
    url_token = media.get("url_token", "")
    decode_key = media.get("decode_key", "")

    if not url or not decode_key:
        raise ValueError("الاستجابة ناقصة - ماكو رابط فيديو او decode_key")

    full_url = url + url_token  # التوثيق يوضح: لازم تركيبهم مع بعض لتفادي حماية الرابط

    return {
        "download_url": full_url,
        "decode_key": decode_key,
        "uploader": data.get("nickname") or "",
        "uploader_id": data.get("username") or "",
        "description": object_desc.get("description") or "",
    }


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
    with requests.get(media["download_url"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(encrypted_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

    # إرسال الملف المشفر لخدمة فك التشفير
    decrypted_path = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_wx_decrypted.mp4")
    try:
        with open(encrypted_path, "rb") as f:
            resp = requests.post(
                f"{config.WECHAT_DECRYPT_API_URL}/api/decrypt",
                files={"video": f},
                data={"decode_key": str(media["decode_key"])},
                timeout=60,
            )
        resp.raise_for_status()
        with open(decrypted_path, "wb") as f:
            f.write(resp.content)
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
