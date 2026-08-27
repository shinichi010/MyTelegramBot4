"""
تكامل بسيط مع TikHub API - يستخدم حالياً بس لجلب رصيد الحساب (استهلاك) بالإحصائيات.
لاحقاً يمديه يتوسع ليشمل تحميل فيديوهات ويشات (视频号).
"""
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
