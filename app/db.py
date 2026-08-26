"""
طبقة قاعدة البيانات (MongoDB) - تخزن فقط بيانات نصية:
- مستخدمين (يوزر/اسم/آيدي)
- رسائل قابلة للتعديل (بداية، جودة، أخطاء، انتظار...)
- سجل روابط
- قائمة محظورين
- منصات موقوفة مؤقتاً
- إحصائيات

ملاحظة: لا تُخزَّن أي فيديوهات أو صور هنا إطلاقاً - القاعدة نصوص فقط.
"""
import logging
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING

from . import config

logger = logging.getLogger("db")

_client = None
_db = None

# --- الرسائل الافتراضية (تُستخدم أول مرة فقط، بعدها تنقرأ وتتعدل من القاعدة) ---
DEFAULT_MESSAGES = {
    "welcome": (
        "هلا بيك 👋\n\n"
        "ارسلي رابط فيديو من *X (تويتر)* او *دويين* وراح أنزلّك المحتوى.\n\n"
        "▪️ روابط X: راح تطلع الك خيارات جودة تختار منها.\n"
        "▪️ روابط دويين: يتنزل تلقائياً بأعلى جودة متوفرة (فيديو او صور)."
    ),
    "unsupported_link": "بس روابط X (تويتر) او دويين مدعومة حالياً 🙏",
    "fetching_qualities": "🔍 اجيب خيارات الجودة...",
    "choose_quality": "اختار الجودة اللي تريدها 👇",
    "downloading": "⬇️ جاري التحميل...",
    "downloading_douyin": "⬇️ جاري التحميل بأعلى جودة...",
    "download_error": "صار خطأ بالتحميل ❌\n{error}",
    "quality_fetch_error": "ما گدرت اجيب معلومات الرابط ❌\n{error}",
    "expired_request": "انتهت صلاحية هذا الطلب، ارسل الرابط مرة اخرى 🔄",
    "platform_disabled": "التحميل من هذه المنصة متوقف حالياً 🚫",
    "user_banned": "ما تكدر تستخدم البوت حالياً 🚫",
    "file_too_large": "الملف حجمه اكبر من {max_size} ميكا، ما يگدر البوت يرسله ❌",
    "queue_wait": (
        "⏳ حالياً اكو تحميل ثقيل شغال. انت رقمك {position} بالطابور.\n"
        "راح يبدأ تحميلك تلقائياً بعد ما يخلص اللي گبلك."
    ),
}


def init():
    """تهيئة الاتصال بقاعدة البيانات، تُستدعى مرة وحدة عند تشغيل البوت."""
    global _client, _db
    if not config.MONGO_URI:
        logger.warning("MONGO_URI مو محدد - ميزات القاعدة (رسائل قابلة للتعديل، إحصائيات...) متوقفة")
        return

    _client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=8000)
    _db = _client[config.MONGO_DB_NAME]

    # نتأكد الاتصال شغال
    _client.admin.command("ping")

    _db.messages.create_index("key", unique=True)
    _db.users.create_index("user_id", unique=True)
    _db.links.create_index([("created_at", ASCENDING)])
    _db.banned_users.create_index("user_id", unique=True)
    _db.disabled_platforms.create_index("platform", unique=True)
    _db.stickers.create_index("key", unique=True)

    # نزرع الرسائل الافتراضية اذا مو موجودة
    for key, text in DEFAULT_MESSAGES.items():
        _db.messages.update_one(
            {"key": key}, {"$setOnInsert": {"key": key, "text": text}}, upsert=True
        )

    logger.info("✅ اتصال MongoDB جاهز")


def is_connected() -> bool:
    return _db is not None


# ---------- الرسائل ----------

def get_message(key: str, **kwargs) -> str:
    """يجيب رسالة من القاعدة (او الافتراضية اذا القاعدة غير متصلة) ويعبي المتغيرات."""
    text = DEFAULT_MESSAGES.get(key, "")
    if is_connected():
        doc = _db.messages.find_one({"key": key})
        if doc and doc.get("text"):
            text = doc["text"]
    try:
        return text.format(**kwargs) if kwargs else text
    except (KeyError, IndexError):
        return text


def set_message(key: str, text: str) -> bool:
    if not is_connected():
        return False
    _db.messages.update_one({"key": key}, {"$set": {"text": text}}, upsert=True)
    return True


def all_message_keys() -> list[str]:
    return list(DEFAULT_MESSAGES.keys())


# ---------- الستيكرات (رفع/خطأ لكل منصة) ----------

STICKER_KEYS = ["upload_x", "error_x", "upload_douyin", "error_douyin"]


def get_sticker(key: str) -> str | None:
    """يرجع file_id للستيكر المحدد، او None اذا مو محدد."""
    if not is_connected():
        return None
    doc = _db.stickers.find_one({"key": key})
    return doc.get("file_id") if doc else None


def set_sticker(key: str, file_id: str) -> bool:
    if not is_connected():
        return False
    _db.stickers.update_one({"key": key}, {"$set": {"file_id": file_id}}, upsert=True)
    return True


# ---------- المستخدمين ----------

def upsert_user(user_id: int, username: str, full_name: str) -> bool:
    """يسجل المستخدم اذا جديد، يرجع True اذا كان جديد فعلاً."""
    if not is_connected():
        return False
    result = _db.users.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "username": username,
                "full_name": full_name,
                "joined_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return result.upserted_id is not None


def count_users() -> int:
    if not is_connected():
        return 0
    return _db.users.count_documents({})


# ---------- الحظر ----------

def ban_user(user_id: int):
    if not is_connected():
        return
    _db.banned_users.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "banned_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def unban_user(user_id: int):
    if not is_connected():
        return
    _db.banned_users.delete_one({"user_id": user_id})


def is_banned(user_id: int) -> bool:
    if not is_connected():
        return False
    return _db.banned_users.find_one({"user_id": user_id}) is not None


# ---------- توقيف منصة ----------

def disable_platform(platform: str):
    if not is_connected():
        return
    _db.disabled_platforms.update_one(
        {"platform": platform}, {"$set": {"platform": platform}}, upsert=True
    )


def enable_platform(platform: str):
    if not is_connected():
        return
    _db.disabled_platforms.delete_one({"platform": platform})


def is_platform_disabled(platform: str) -> bool:
    if not is_connected():
        return False
    return _db.disabled_platforms.find_one({"platform": platform}) is not None


# ---------- سجل الروابط ----------

def log_link(user_id: int, username: str, platform: str, url: str):
    """يسجل رابط أرسله مستخدم. روابط الأدمن (ADMIN_CHAT_ID) ما تنسجل - يُتحقق منها قبل الاستدعاء."""
    if not is_connected():
        return
    _db.links.insert_one({
        "user_id": user_id,
        "username": username,
        "platform": platform,
        "url": url,
        "created_at": datetime.now(timezone.utc),
    })


def export_links_text(limit: int = 2000) -> str:
    """يبني نص فيه كل الروابط المسجلة، جاهز يرسل كملف."""
    if not is_connected():
        return ""
    docs = _db.links.find().sort("created_at", -1).limit(limit)
    lines = []
    for d in docs:
        ts = d["created_at"].strftime("%Y-%m-%d %H:%M")
        uname = f"@{d['username']}" if d.get("username") else str(d["user_id"])
        lines.append(f"[{ts}] {uname} ({d['platform']}): {d['url']}")
    return "\n".join(reversed(lines))


def count_links() -> int:
    if not is_connected():
        return 0
    return _db.links.count_documents({})


# ---------- إحصائيات ----------

def get_stats() -> dict:
    return {
        "users": count_users(),
        "links": count_links(),
        "banned": _db.banned_users.count_documents({}) if is_connected() else 0,
    }
