"""
نظام المحفظة (رصيد مدفوع دائم لكل منصة) + المجاني الأسبوعي لكل منصة + سجل العمليات.

المبادئ:
- الرصيد لكل منصة منفصل: {douyin: 8, rednote: 0, wechat: 3}.
- ترتيب الاستهلاك: المجاني الأسبوعي (للمنصة نفسها) أولاً، بعدين المدفوع/الهدية.
- الخصم يصير بعد نجاح التحميل (reserve -> commit/rollback) حتى ما يخسر المستخدم على تحميل ما وصل.
- كل حركة تنسجل بجدول transactions (شراء / هدية / سحب / استهلاك / استرجاع).
- الأدمن ما ينخصم منه شي (يُتحقق منها بالـ bot قبل استدعاء أي دالة هنا).
"""
import logging
from datetime import datetime, timezone

from . import db

logger = logging.getLogger("wallet")

# المنصات اللي تدعم المحاولة البديلة المدفوعة
PAID_PLATFORMS = ("douyin", "rednote", "wechat")

# ---------- الباقات الافتراضية (نجوم) ----------
# تكلفتك التقريبية على TikHub: دويين ~0.001$، RedNote ~0.01$، ويشات ~0.01$ + استهلاك Render.
# النجمة الواحدة تعطيك ~0.013$، فكل الباقات فيها هامش واضح. تعدلها من /admin بدون كود.
DEFAULT_PACKAGES = {
    "douyin": [
        {"credits": 10, "stars": 15},
        {"credits": 30, "stars": 40},
        {"credits": 100, "stars": 120},
    ],
    "rednote": [
        {"credits": 10, "stars": 30},
        {"credits": 30, "stars": 80},
        {"credits": 100, "stars": 240},
    ],
    "wechat": [
        {"credits": 10, "stars": 40},
        {"credits": 30, "stars": 110},
        {"credits": 100, "stars": 330},
    ],
}

# المجاني الأسبوعي الافتراضي لكل منصة (يتعدل من /admin)
DEFAULT_WEEKLY_FREE = {"douyin": 5, "rednote": 5, "wechat": 1}


def _now():
    return datetime.now(timezone.utc)


def _col(name: str):
    return getattr(db._db, name) if db.is_connected() else None


def init_indexes():
    if not db.is_connected():
        return
    db._db.wallets.create_index("user_id", unique=True)
    db._db.transactions.create_index([("user_id", 1), ("created_at", -1)])
    db._db.transactions.create_index("charge_id", unique=True, sparse=True)
    db._db.transactions.create_index("type")


# ---------- الإعدادات (كلها قابلة للتعديل من /admin) ----------

def payments_enabled(platform: str | None = None) -> bool:
    """الدفع مفعّل عام، ولو ذكرنا منصة: مفعّل لها أيضاً."""
    if not db.get_setting("payments_enabled", True):
        return False
    if platform and not db.get_setting(f"payments_enabled_{platform}", True):
        return False
    return True


def get_weekly_free_limit(platform: str) -> int:
    default = DEFAULT_WEEKLY_FREE.get(platform, 0)
    return int(db.get_setting(f"{platform}_fallback_weekly_limit", default))


def get_packages(platform: str) -> list[dict]:
    """الباقات المخزنة بالقاعدة (لو الأدمن عدلها) وإلا الافتراضية."""
    stored = db.get_setting(f"packages_{platform}", None)
    if isinstance(stored, list) and stored:
        return stored
    return DEFAULT_PACKAGES.get(platform, [])


def set_packages(platform: str, packages: list[dict]):
    db.set_setting(f"packages_{platform}", packages)


# ---------- الأرصدة ----------

def get_balance(user_id: int, platform: str) -> int:
    if not db.is_connected():
        return 0
    doc = _db().wallets.find_one({"user_id": user_id})
    return int((doc or {}).get("balances", {}).get(platform, 0))


def get_all_balances(user_id: int) -> dict:
    if not db.is_connected():
        return {p: 0 for p in PAID_PLATFORMS}
    doc = _db().wallets.find_one({"user_id": user_id}) or {}
    bal = doc.get("balances", {})
    return {p: int(bal.get(p, 0)) for p in PAID_PLATFORMS}


def _db():
    return db._db


def get_free_left(user_id: int, platform: str) -> int:
    limit = get_weekly_free_limit(platform)
    used = db.get_fallback_usage(user_id, platform)
    return max(limit - used, 0)


def availability(user_id: int, platform: str) -> dict:
    """يرجع كل شي تحتاجه رسالة التأكيد: مجاني متبقي + مدفوع + هل يقدر يكمل."""
    free_left = get_free_left(user_id, platform)
    paid = get_balance(user_id, platform)
    return {
        "free_left": free_left,
        "paid": paid,
        "can_use": (free_left + paid) > 0,
    }


def _log(user_id: int, tx_type: str, platform: str, credits: int, **extra):
    doc = {
        "user_id": user_id,
        "type": tx_type,          # purchase | gift | adjust | consume_free | consume_paid | refund_credit | refund_stars
        "platform": platform,
        "credits": credits,       # موجب = زيادة رصيد، سالب = نقصان
        "created_at": _now(),
    }
    doc.update({k: v for k, v in extra.items() if v is not None})
    try:
        _db().transactions.insert_one(doc)
    except Exception:
        logger.exception("failed to log transaction")


def _inc_balance(user_id: int, platform: str, delta: int) -> int:
    """زيادة/نقصان ذري للرصيد. يرجع الرصيد الجديد."""
    doc = _db().wallets.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {f"balances.{platform}": delta}, "$setOnInsert": {"created_at": _now()}},
        upsert=True,
        return_document=True,
    )
    return int(doc.get("balances", {}).get(platform, 0))


# ---------- الاستهلاك (يستدعى بعد نجاح التحميل) ----------

def consume(user_id: int, platform: str, url: str = "") -> str | None:
    """يخصم 1 بعد نجاح التحميل. المجاني أولاً ثم المدفوع.
    يرجع 'free' او 'paid' حسب المصدر، او None اذا ما كان في شي يتخصم (ما يحصل عادةً لأننا نتحقق قبل)."""
    if not db.is_connected():
        return None

    if get_free_left(user_id, platform) > 0:
        db.increment_fallback_usage(user_id, platform)
        _log(user_id, "consume_free", platform, 0, url=url)
        return "free"

    # خصم مدفوع ذري: ما ينزل تحت الصفر حتى لو ضغط المستخدم مرتين بنفس الوقت
    doc = _db().wallets.find_one_and_update(
        {"user_id": user_id, f"balances.{platform}": {"$gte": 1}},
        {"$inc": {f"balances.{platform}": -1}},
        return_document=True,
    )
    if doc is None:
        return None
    _log(user_id, "consume_paid", platform, -1, url=url)
    return "paid"


def give_back(user_id: int, platform: str, source: str, url: str = ""):
    """يرجع الرصيد تلقائياً لو صار خطأ بعد الخصم (ما يحصل عادةً لأن الخصم بعد النجاح،
    بس نخليها كشبكة أمان لو فشل شي بعد consume)."""
    if not db.is_connected() or source not in ("free", "paid"):
        return
    if source == "free":
        try:
            week = db._current_week_key()
            _db().fallback_usage.update_one(
                {"user_id": user_id, "platform": platform, "week": week, "count": {"$gt": 0}},
                {"$inc": {"count": -1}},
            )
        except Exception:
            logger.exception("failed to give back free credit")
    else:
        _inc_balance(user_id, platform, 1)
    _log(user_id, "refund_credit", platform, 1 if source == "paid" else 0, url=url, source=source)


# ---------- الشراء / الهدية / التصحيح ----------

def add_purchase(user_id: int, platform: str, credits: int, stars: int, charge_id: str) -> tuple[bool, int]:
    """يضيف رصيد بعد دفع ناجح. يمنع معالجة نفس الدفعة مرتين عبر charge_id (فهرس unique).
    يرجع (نجحت العملية؟، الرصيد الجديد). اذا (False, رصيد) فمعناها الدفعة مكررة."""
    if not db.is_connected():
        return False, 0
    try:
        _db().transactions.insert_one({
            "user_id": user_id, "type": "purchase", "platform": platform,
            "credits": credits, "stars": stars, "charge_id": charge_id,
            "refunded": False, "created_at": _now(),
        })
    except Exception as e:
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            return False, get_balance(user_id, platform)
        raise
    return True, _inc_balance(user_id, platform, credits)


def gift(user_id: int, platform: str, credits: int, admin_id: int, note: str = "") -> int:
    """هدية من الأدمن (credits موجب)، او سحب/تصحيح (credits سالب). يرجع الرصيد الجديد."""
    if not db.is_connected():
        return 0
    if credits < 0:
        # ما ننزل الرصيد تحت الصفر
        current = get_balance(user_id, platform)
        credits = -min(current, abs(credits))
        if credits == 0:
            return current
    new_balance = _inc_balance(user_id, platform, credits)
    _log(user_id, "gift" if credits > 0 else "adjust", platform, credits, admin_id=admin_id, note=note or None)
    return new_balance


# ---------- الاسترجاع (Refund بالنجوم) ----------

def get_purchase(charge_id: str) -> dict | None:
    if not db.is_connected():
        return None
    return _db().transactions.find_one({"type": "purchase", "charge_id": charge_id})


def recent_purchases(user_id: int | None = None, limit: int = 10) -> list[dict]:
    if not db.is_connected():
        return []
    q = {"type": "purchase"}
    if user_id is not None:
        q["user_id"] = user_id
    return list(_db().transactions.find(q).sort("created_at", -1).limit(limit))


def mark_refunded(charge_id: str) -> tuple[bool, int]:
    """يعلّم الشراء كمسترجع وينقص الرصيد الممنوح بقدر ما يقدر (ما ينزل تحت الصفر).
    يرجع (نجح؟، عدد الرصيد اللي انسحب فعلاً)."""
    if not db.is_connected():
        return False, 0
    tx = _db().transactions.find_one_and_update(
        {"type": "purchase", "charge_id": charge_id, "refunded": False},
        {"$set": {"refunded": True, "refunded_at": _now()}},
    )
    if tx is None:
        return False, 0
    clawed = 0
    current = get_balance(tx["user_id"], tx["platform"])
    clawed = min(current, tx["credits"])
    if clawed > 0:
        _inc_balance(tx["user_id"], tx["platform"], -clawed)
    _log(tx["user_id"], "refund_stars", tx["platform"], -clawed, charge_id=None,
         ref_charge_id=charge_id, stars=tx.get("stars"))
    return True, clawed


# ---------- إحصائيات للأدمن ----------

def revenue_stats() -> dict:
    if not db.is_connected():
        return {"purchases": 0, "stars": 0, "refunded_stars": 0, "outstanding": {}}
    pipeline = [
        {"$match": {"type": "purchase"}},
        {"$group": {
            "_id": "$refunded",
            "n": {"$sum": 1},
            "stars": {"$sum": "$stars"},
        }},
    ]
    purchases = 0
    stars = 0
    refunded_stars = 0
    for d in _db().transactions.aggregate(pipeline):
        if d["_id"]:
            refunded_stars += d["stars"]
        else:
            purchases += d["n"]
            stars += d["stars"]

    outstanding = {p: 0 for p in PAID_PLATFORMS}
    for w in _db().wallets.find({}, {"balances": 1}):
        for p, v in (w.get("balances") or {}).items():
            if p in outstanding:
                outstanding[p] += int(v)
    return {
        "purchases": purchases, "stars": stars,
        "refunded_stars": refunded_stars, "outstanding": outstanding,
    }


def top_spenders(limit: int = 10) -> list[dict]:
    if not db.is_connected():
        return []
    pipeline = [
        {"$match": {"type": "purchase", "refunded": False}},
        {"$group": {"_id": "$user_id", "stars": {"$sum": "$stars"}, "n": {"$sum": 1}}},
        {"$sort": {"stars": -1}},
        {"$limit": limit},
    ]
    return [{"user_id": d["_id"], "stars": d["stars"], "n": d["n"]} for d in _db().transactions.aggregate(pipeline)]


def user_history(user_id: int, limit: int = 10) -> list[dict]:
    if not db.is_connected():
        return []
    return list(_db().transactions.find({"user_id": user_id}).sort("created_at", -1).limit(limit))


def find_user_id(query: str) -> int | None:
    """يقبل آيدي رقمي او @يوزر (من جدول المستخدمين المسجلين بالبوت)."""
    query = (query or "").strip()
    if not query:
        return None
    if query.lstrip("-").isdigit():
        return int(query)
    if not db.is_connected():
        return None
    uname = query.lstrip("@")
    doc = _db().users.find_one({"username": {"$regex": f"^{uname}$", "$options": "i"}})
    return doc["user_id"] if doc else None
