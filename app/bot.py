import logging
import time
import uuid
from types import SimpleNamespace

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters, PreCheckoutQueryHandler,
)

from . import config, downloader, db, tikhub, wechat, wallet, payments, admin_wallet

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# تخزين مؤقت بالذاكرة: id قصير -> الرابط (لأن callback_data محدود بـ 64 بايت)
PENDING: dict[str, str] = {}

# نفس req_id مالت PENDING -> {height: expected_size_bytes} (منصة X فقط، مستخرجة أصلاً
# لحظة عرض قائمة الجودات). تستخدم لمعرفة الحجم المتوقع قبل التحميل بدون طلب yt-dlp إضافي.
PENDING_QUALITY_SIZES: dict[str, dict] = {}

# ==================== نظام الطابور للتحميلات الثقيلة ====================
# لما يصير تحميل "ثقيل" (اكبر من HEAVY_FILE_THRESHOLD_MB)، أي طلب جديد
# ينتظر دوره بدل ما يشتغل بالتوازي ويزنق موارد السيرفر المحدودة.
import asyncio

_heavy_lock = asyncio.Lock()
_queue_waiters: list[uuid.UUID] = []  # ترتيب الدخول للطابور


async def _acquire_heavy_slot(update, context, chat_id: int):
    """ينتظر دوره اذا اكو تحميل ثقيل شغال، ويرسل رسالة الانتظار مع رقم الدور."""
    if not _heavy_lock.locked():
        await _heavy_lock.acquire()
        return

    token = uuid.uuid4()
    _queue_waiters.append(token)
    position = len(_queue_waiters)
    lang = _lang(update.effective_user.id)
    wait_msg = await context.bot.send_message(
        chat_id, db.get_message("queue_wait", lang, position=position)
    )

    await _heavy_lock.acquire()
    if token in _queue_waiters:
        _queue_waiters.remove(token)

    try:
        await wait_msg.delete()
    except Exception:
        pass


def _release_heavy_slot():
    if _heavy_lock.locked():
        _heavy_lock.release()

# مفاتيح رسائل قابلة للتعديل، لعرضها بلوحة تحكم الأدمن مع أسماء مفهومة
EDITABLE_MESSAGES = {
    # --- أساسية ---
    "welcome": "رسالة البداية (/start)",
    "help": "رسالة /help",
    "help_admin": "ملحق /help للأدمن",
    "unsupported_link": "رابط غير مدعوم",
    "fetching_qualities": "جلب خيارات الجودة (X)",
    "choose_quality": "اختيار الجودة (X)",
    "downloading": "جاري التحميل (X)",
    "downloading_douyin": "جاري التحميل (دويين)",
    "downloading_audio": "جاري تحميل الصوت",
    "download_error": "خطأ بالتحميل",
    "post_info_error": "خطأ بمعلومات المنشور",
    "post_info_template": "قالب معلومات المنشور",
    "quality_fetch_error": "خطأ بجلب الجودة",
    "expired_request": "انتهاء صلاحية الطلب",
    "invalid_request": "طلب غير صالح",
    "no_files": "ما گدرت انزل المنشور",
    "platform_disabled": "منصة موقوفة",
    "user_banned": "مستخدم محظور",
    "maintenance_mode": "وضع الصيانة",
    "file_too_large": "الملف كبير جداً",
    "queue_wait": "انتظار بالطابور",
    "multi_links": "عدة روابط بالرسالة",
    "verifying_link": "جاري التحقق من الرابط",
    "link_verify_failed": "فشل التحقق من الرابط",
    "retrying": "جاري إعادة المحاولة",
    "deeplink_invalid": "Deep Link غير صالح",
    "cancel_done": "تم إلغاء العملية",
    "cancel_none": "ماكو عملية معلقة",
    # --- المحاولة البديلة ---
    "fallback_retrying": "جاري المحاولة البديلة",
    "fallback_failed": "فشلت المحاولة البديلة",
    "fallback_limit_reached": "وصل حد المحاولة البديلة الأسبوعي",
    "fallback_confirm": "تأكيد المحاولة البديلة (عنده رصيد)",
    "fallback_no_credit": "المحاولة البديلة (ما عنده رصيد)",
    "fallback_admin": "المحاولة البديلة (للأدمن)",
    "fallback_disabled": "المحاولة البديلة موقفة",
    "wechat_disabled": "ويشات غير مفعّل",
    "low_balance_one": "تنبيه: باقي تحميل واحد",
    "low_balance_zero": "تنبيه: خلص الرصيد",
    "size_limit_active": "رسالة لمت حجم الملفات",
    "direct_link_prompt": "رسالة عرض الرابط المباشر (X)",
    "btn_direct_download": "نص زر تحميل من المتصفح",
    # --- المتجر والدفع ---
    "shop_title": "المتجر: العنوان",
    "shop_pick_platform": "المتجر: اختيار المنصة",
    "shop_platform_title": "المتجر: عنوان باقات منصة",
    "shop_pick_package": "المتجر: اختيار الباقة",
    "shop_disabled": "المتجر: الشراء موقف",
    "shop_invoice_error": "المتجر: تعذر إنشاء الفاتورة",
    "invoice_title": "عنوان الفاتورة",
    "invoice_desc": "وصف الفاتورة",
    "pay_success": "تم الدفع بنجاح",
    "pay_unknown_payload": "دفعة payload غير مفهوم",
    "pay_invalid_invoice": "رفض: فاتورة غير صالحة",
    "pay_platform_disabled": "رفض: الشراء موقف للمنصة",
    "pay_price_changed": "رفض: تغير السعر",
    "pay_unavailable": "رفض: الخدمة غير متاحة",
    "pay_error": "رفض: خطأ عام",
    "paysupport": "رسالة /paysupport",
    "refund_user_notice": "إشعار المستخدم بالاسترجاع",
    "gift_user_notice": "إشعار المستخدم بالهدية",
    # --- /stats ---
    "stats_title": "/stats: العنوان",
    "stats_no_downloads": "/stats: ما عنده تحميلات",
    "stats_balance_title": "/stats: عنوان الرصيد",
    "stats_balance_line": "/stats: سطر رصيد منصة",
}

# شرح المتغيرات المتوفرة لكل رسالة قابلة للتعديل، يطلع للأدمن وقت التعديل
MESSAGE_VARIABLE_HINTS = {
    "post_info_template": (
        "المتغيرات المتوفرة (لازم تخلي الأقواس المجعّدة `{}` كما هي):\n"
        "• `{uploader}` — اسم صاحب الحساب\n"
        "• `{handle}` — اليوزر (@username) او 'بدون يوزر' لو مو متوفر\n"
        "• `{description}` — وصف المنشور (مختصر لحد 400 حرف)\n"
        "• `{count_line}` — سطر عدد المقاطع/الصور (يظهر بس لو المنشور فيه اكثر من وحدة، خله بالمكان اللي تريده يطلع)\n\n"
        "مثال: `👤 {uploader}\\n🔗 {handle}\\n📝 {description}`"
    ),
    "download_error": "المتغير المتوفر: `{error}` — نص الخطأ الفعلي من نظام التحميل.",
    "post_info_error": "المتغير المتوفر: `{error}` — نص الخطأ الفعلي.",
    "file_too_large": "المتغير المتوفر: `{max_size}` — الحد الأقصى المسموح بالميكابايت.",
    "queue_wait": "المتغير المتوفر: `{position}` — رقم دور المستخدم بالطابور.",
    "fallback_failed": "المتغير المتوفر: `{error}` — نص الخطأ الفعلي.",
    "fallback_limit_reached": "المتغير المتوفر: `{limit}` — الحد الأسبوعي الحالي.",
    "link_verify_failed": "المتغير المتوفر: `{url}` — الرابط اللي فشل التحقق منه.",
    "fallback_confirm": "المتغيرات: `{platform}` اسم المنصة، `{free_left}` المجانية المتبقية، `{paid_balance}` الرصيد المدفوع.",
    "fallback_no_credit": "المتغير: `{platform}` — اسم المنصة.",
    "multi_links": "المتغير: `{count}` — عدد الروابط.",
    "low_balance_one": "المتغير: `{platform}` — اسم المنصة.",
    "low_balance_zero": "المتغير: `{platform}` — اسم المنصة.",
    "shop_platform_title": "المتغير: `{platform}` — اسم المنصة.",
    "invoice_title": "المتغيرات: `{credits}` عدد التحميلات، `{platform}` المنصة.",
    "invoice_desc": "المتغيرات: `{credits}` عدد التحميلات، `{platform}` المنصة.",
    "pay_success": "المتغيرات: `{credits}` المضاف، `{platform}` المنصة، `{balance}` الرصيد الحالي.",
    "gift_user_notice": "المتغيرات: `{credits}` عدد التحميلات، `{platform}` المنصة.",
    "stats_balance_line": "المتغيرات: `{platform}` المنصة، `{free}` المجانية، `{paid}` المدفوعة.",
}

# محادثة تعديل رسالة (أدمن فقط): user_id -> (key الرسالة، اللغة) اللي ينتظر نصها الجديد
AWAITING_MESSAGE_EDIT: dict[int, tuple[str, str]] = {}

# محادثة تعديل ستيكر (أدمن فقط): user_id -> key الستيكر اللي ينتظر يرسله
AWAITING_STICKER_EDIT: dict[int, str] = {}

# محادثة تعديل حد رقمي (أدمن فقط): user_id -> اسم الإعداد
AWAITING_LIMIT_EDIT: dict[int, str] = {}

AUTODELETE_LABELS = {
    "download_error": "رسالة خطأ التحميل",
    "post_info_error": "رسالة خطأ معلومات المنشور",
}

LIMIT_LABELS = {
    "max_file_size_mb": "أقصى حجم ملف (ميكا)",
    "heavy_file_threshold_mb": "حد تفعيل الطابور (ميكا)",
    "main_ping_interval_min": "فاصل بينك البوت الرئيسي (دقايق)",
    "wechat_ping_interval_min": "فاصل بينك خدمة ويشات (دقايق)",
    "download_error_autodelete_min": "مدة حذف رسالة خطأ التحميل",
    "post_info_error_autodelete_min": "مدة حذف رسالة خطأ معلومات المنشور",
    "douyin_fallback_weekly_limit": "حد المحاولة البديلة الأسبوعي لكل مستخدم (دويين)",
    "fallback_failure_autodelete_sec": "مدة حذف رسالة فشل المحاولة البديلة",
    "failure_alert_threshold": "عدد الفشل المتتالي قبل تنبيه الأدمن",
    "rednote_fallback_weekly_limit": "حد المحاولة البديلة الأسبوعي لكل مستخدم (RedNote)",
    "wechat_fallback_weekly_limit": "حد المحاولة البديلة الأسبوعي لكل مستخدم (ويشات)",
    "low_tikhub_balance_usd": "حد تنبيه رصيد TikHub (دولار)",
    "size_limit_trigger_mb": "عتبة تفعيل لمت الحجم (ميكا)",
    "size_limit_duration_hours": "مدة لمت الحجم (ساعات)",
    "size_limit_small_mb": "أقصى حجم مسموح أثناء اللمت (ميكا)",
    "direct_link_threshold_mb": "حد تفعيل الرابط المباشر لمنصة X (ميكا)",
}
LIMIT_UNITS = {
    "max_file_size_mb": "ميكا",
    "heavy_file_threshold_mb": "ميكا",
    "main_ping_interval_min": "دقايق",
    "wechat_ping_interval_min": "دقايق",
    "download_error_autodelete_min": "دقايق",
    "post_info_error_autodelete_min": "دقايق",
    "douyin_fallback_weekly_limit": "محاولة/أسبوع",
    "fallback_failure_autodelete_sec": "ثانية",
    "failure_alert_threshold": "حالة فشل",
    "rednote_fallback_weekly_limit": "محاولة/أسبوع",
    "wechat_fallback_weekly_limit": "محاولة/أسبوع",
    "low_tikhub_balance_usd": "دولار",
    "size_limit_trigger_mb": "ميكا",
    "size_limit_duration_hours": "ساعة",
    "size_limit_small_mb": "ميكا",
    "direct_link_threshold_mb": "ميكا",
}

STICKER_LABELS = {
    "upload_x": "ستيكر الرفع - X",
    "error_x": "ستيكر الخطأ - X",
    "upload_douyin": "ستيكر الرفع - دويين",
    "error_douyin": "ستيكر الخطأ - دويين",
    "upload_wechat": "ستيكر الرفع - ويشات",
    "error_wechat": "ستيكر الخطأ - ويشات",
    "upload_rednote": "ستيكر الرفع - RedNote",
    "error_rednote": "ستيكر الخطأ - RedNote",
    "upload_bilibili": "ستيكر الرفع - Bilibili",
    "error_bilibili": "ستيكر الخطأ - Bilibili",
}

PLATFORM_LABELS = (
    ("x", "X (تويتر)"),
    ("douyin", "دويين"),
    ("wechat", "ويشات"),
    ("rednote", "RedNote (小红书)"),
    ("bilibili", "Bilibili"),
)


def _is_admin(user_id: int) -> bool:
    return bool(config.ADMIN_CHAT_ID) and str(user_id) == str(config.ADMIN_CHAT_ID)


def _post_info_enabled(user_id: int) -> bool:
    """معلومات المنشور: اذا الأدمن أطفاها عام، تنطفي للكل بلا استثناء.
    غير هيچ، كل مستخدم يقرر لحاله (افتراضياً مفعّلة)."""
    if not db.get_setting("post_info_global_enabled", True):
        return False
    return db.get_user_pref(user_id, "show_post_info", True)


def _verify_link_enabled(user_id: int) -> bool:
    """التحقق من الرابط: الأدمن يحدد الافتراضي العام، وكل مستخدم يقدر يغيره لحاله."""
    global_default = db.get_setting("verify_link_before_download", True)
    return db.get_user_pref(user_id, "verify_link", global_default)


def _preview_enabled(user_id: int) -> bool:
    """معاينة سريعة (صورة مصغرة + مدة) قبل التحميل: اذا الأدمن أطفاها عام، تنطفي للكل.
    غير هيچ، كل مستخدم يقرر لحاله (افتراضياً موقفة - المعاينة تبطئ التحميل شوي)."""
    if not db.get_setting("preview_global_enabled", True):
        return False
    return db.get_user_pref(user_id, "show_preview", False)


def _max_file_size_mb() -> int:
    """حد أقصى حجم الملف بالميكابايت - قابل للتعديل من /admin، ويرجع لقيمة MAX_FILE_SIZE_MB
    البيئية كافتراضي أول تشغيل."""
    return int(db.get_setting("max_file_size_mb", config.MAX_FILE_SIZE_MB))


def _max_file_size_bytes() -> int:
    return _max_file_size_mb() * 1024 * 1024


def _heavy_threshold_mb() -> int:
    """الحجم اللي فوقه يفعّل نظام الطابور - قابل للتعديل من /admin."""
    return int(db.get_setting("heavy_file_threshold_mb", config.HEAVY_FILE_THRESHOLD_MB))


def _heavy_threshold_bytes() -> int:
    return _heavy_threshold_mb() * 1024 * 1024


def _get_limit_value(key: str) -> int:
    """يجيب القيمة الحالية لأي إعداد رقمي قابل للتعديل من قائمة الحدود بـ /admin."""
    defaults = {
        "max_file_size_mb": config.MAX_FILE_SIZE_MB,
        "heavy_file_threshold_mb": config.HEAVY_FILE_THRESHOLD_MB,
        "main_ping_interval_min": config.PING_INTERVAL // 60,
        "wechat_ping_interval_min": 10,
        "douyin_fallback_weekly_limit": 5,
        "fallback_failure_autodelete_sec": 15,
        "failure_alert_threshold": 5,
        "rednote_fallback_weekly_limit": 5,
        "wechat_fallback_weekly_limit": 1,
        "low_tikhub_balance_usd": 2,
        "size_limit_trigger_mb": 70,
        "size_limit_duration_hours": 6,
        "size_limit_small_mb": 30,
        "direct_link_threshold_mb": 250,
    }
    return int(db.get_setting(key, defaults.get(key, 0)))


def _build_fallback_menu_buttons(platform: str) -> list:
    """يبني أزرار قائمة إعدادات المحاولة البديلة لمنصة معينة (دويين/RedNote).
    مدة/تفعيل حذف رسالة الفشل مشتركة بين كل المنصات، والبقية لكل منصة لحالها."""
    fb_enabled = db.get_setting(f"{platform}_fallback_enabled", True)
    fail_del_enabled = db.get_setting("fallback_failure_autodelete_enabled", False)
    weekly_key = f"{platform}_fallback_weekly_limit"
    toggle_cb = {"rednote": "adm:rednote_fallback_toggle", "wechat": "adm:wechat_fallback_toggle"}.get(platform, "adm:fallback_toggle")
    fail_del_cb = {"rednote": "adm:rednote_fallback_fail_del_toggle", "wechat": "adm:wechat_fallback_fail_del_toggle"}.get(platform, "adm:fallback_fail_del_toggle")

    return [
        [InlineKeyboardButton(
            f"🔀 المحاولة البديلة: {'🟢 مفعّلة' if fb_enabled else '🔴 موقفة'}",
            callback_data=toggle_cb,
        )],
        [InlineKeyboardButton(
            f"📅 {LIMIT_LABELS[weekly_key]}: {_get_limit_value(weekly_key)}",
            callback_data=f"adm:limit_edit:{weekly_key}",
        )],
        [InlineKeyboardButton(
            f"🗑️ حذف رسالة الفشل تلقائياً: {'🟢 مفعّل' if fail_del_enabled else '🔴 موقف'}",
            callback_data=fail_del_cb,
        )],
        [InlineKeyboardButton(
            f"⏱️ {LIMIT_LABELS['fallback_failure_autodelete_sec']}: {_get_limit_value('fallback_failure_autodelete_sec')} ثانية",
            callback_data="adm:limit_edit:fallback_failure_autodelete_sec",
        )],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="adm:limits")],
    ]


async def _record_download_failure(context: ContextTypes.DEFAULT_TYPE, platform: str, error: str):
    count = db.record_failure(platform)
    threshold = _get_limit_value("failure_alert_threshold")
    if count == threshold:
        try:
            await payments.notify(
                context,
                f"⚠️ تنبيه: صار {count} حالات فشل متتالية بمنصة *{platform}*.\n"
                f"آخر خطأ: {error[:300]}\n\n"
                "ممكن الروابط تحتاج تحديث كوكيز، او فيه مشكلة بالمنصة نفسها.",
                markdown=True, kind="failure",
            )
        except Exception:
            logger.exception("failed to send failure alert to admin")


def _record_download_success(platform: str):
    db.reset_failures(platform)


async def _notify_admin_if_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    is_new = db.upsert_user(user.id, user.username or "", user.full_name or "")
    if not is_new:
        return
    if not payments.target_for("new_user"):
        return

    name = user.full_name or "بدون اسم"
    username = f"@{user.username}" if user.username else "ما عنده يوزرنيم"
    caption = (
        "🆕 مستخدم جديد استخدم البوت!\n\n"
        f"👤 الاسم: {name}\n"
        f"🔗 اليوزر: {username}\n"
        f"🆔 الآيدي: {user.id}"
    )

    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        sent = False
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            sent = await payments.notify_photo(context, "new_user", file_id, caption)
        if not sent:
            await payments.notify(context, caption, kind="new_user")
    except Exception:
        logger.exception("failed to notify admin about new user")


async def _safe_markdown(send_fn, text: str, **kwargs):
    """يرسل/يعدل رسالة بـ Markdown، ولو تيليگرام رفضها (رمز * او _ غير مغلق بنص عدله الأدمن)
    يعيد الإرسال كنص عادي بدل ما تنهار العملية."""
    from telegram.error import BadRequest
    try:
        return await send_fn(text, parse_mode="Markdown", **kwargs)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "can't find end" in str(e).lower():
            return await send_fn(text.replace("*", "").replace("`", ""), **kwargs)
        raise


# آيدي بلاغ قصير -> السياق (المنصة/الرابط/نص الخطأ الحقيقي/نوع الخطأ) لعرضه بتقرير المطور
# ولإرفاقه لما المستخدم يكتب شكواه. ينتهي صلاحيته بعد ساعة عشان الذاكرة ما تتراكم.
PENDING_ERROR_REPORTS: dict[str, dict] = {}

# user_id -> report_id: المستخدم بانتظار يكتب نص شكواه بعد ما ضغط زر الإبلاغ
AWAITING_PROBLEM_REPORT: dict[int, str] = {}


_MD_SPECIAL = ("_", "*", "`", "[")


def _md_escape(text) -> str:
    """يحيّد رموز Markdown (النمط القديم legacy) داخل نص متغير قبل حقنه بقالب رسالة،
    حتى رمز غريب بنص المستخدم او رسالة خطأ من مكتبة خارجية ما يكسر التنسيق ويلغي الإرسال كامل."""
    text = str(text)
    for ch in _MD_SPECIAL:
        text = text.replace(ch, "\\" + ch)
    return text


async def _send_dev_report(context, kind: str, text: str) -> bool:
    """يرسل تقرير للمطور بـ Markdown، ولو انكسر التنسيق (نص لسه فيه رمز ما انحيّد، او خطأ ثاني)
    يعيد الإرسال كنص عادي بدل ما يضيع التقرير بالكامل، ويرجع True/False حسب النجاح."""
    from telegram.error import BadRequest
    target = payments.target_for(kind)
    if not target:
        return False
    try:
        await context.bot.send_message(target, text, parse_mode="Markdown")
        return True
    except BadRequest:
        try:
            await context.bot.send_message(target, text.replace("*", "").replace("`", "").replace("_", ""))
            return True
        except Exception:
            logger.exception("failed to send %s report even as plain text", kind)
            return False
    except Exception:
        logger.exception("failed to send %s report", kind)
        return False


async def _report_error_to_dev(context, kind: str, user_id, platform: str, url: str, error: str, username: str = "") -> str:
    """يسجل الخطأ تلقائياً لقناة/خاص المطور (نوع 'errors')، ويرجع report_id لبناء زر الإبلاغ للمستخدم.
    user_id يقبل رقم آيدي مباشر او كائن User (نستخرج منه .id/.username تلقائياً)."""
    if hasattr(user_id, "id"):
        username = f"@{user_id.username}" if user_id.username else "—"
        user_id = user_id.id
    username = username or "—"

    report_id = uuid.uuid4().hex[:10]
    PENDING_ERROR_REPORTS[report_id] = {
        "platform": platform, "url": url, "error": error, "kind": kind,
        "user_id": user_id, "username": username, "ts": time.time(),
    }
    # تنظيف بسيط: نحذف البلاغات الأقدم من ساعة حتى القاموس ما يتراكم
    cutoff = time.time() - 3600
    for k in [k for k, v in PENDING_ERROR_REPORTS.items() if v["ts"] < cutoff]:
        PENDING_ERROR_REPORTS.pop(k, None)

    text = db.get_message(
        "dev_error_report", "ar",
        kind=_md_escape(kind),
        user_id=user_id if user_id is not None else "—",
        username=_md_escape(username),
        platform=_md_escape(platform or "—"),
        url=_md_escape(url or "—"),
        error=_md_escape(str(error)[:600]),
    )
    await _send_dev_report(context, "errors", text)
    return report_id


def _report_button(report_id: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        db.get_message("btn_report_problem", lang), callback_data=f"report:{report_id}"
    )]])


async def handle_report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج زر 'أبلغ عن المشكلة' - يطلب من المستخدم يكتب شكواه."""
    query = update.callback_query
    await query.answer()
    lang = _lang(query.from_user.id)
    try:
        _, report_id = query.data.split(":", 1)
    except ValueError:
        return
    if report_id not in PENDING_ERROR_REPORTS:
        await context.bot.send_message(query.message.chat_id, db.get_message("expired_request", lang))
        return
    _clear_awaiting_states(query.from_user.id)
    AWAITING_PROBLEM_REPORT[query.from_user.id] = report_id
    await context.bot.send_message(query.message.chat_id, db.get_message("report_ask", lang))


async def _handle_problem_report_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يستقبل نص شكوى المستخدم ويحولها للقناة مع السياق التلقائي. يرجع True لو تعامل معها."""
    user = update.effective_user
    report_id = AWAITING_PROBLEM_REPORT.pop(user.id, None)
    if not report_id:
        return False
    lang = _lang(user.id)
    ctx_data = PENDING_ERROR_REPORTS.get(report_id, {})
    text = db.get_message(
        "user_report", "ar",
        user_id=user.id,
        username=_md_escape(f"@{user.username}") if user.username else "—",
        platform=_md_escape(ctx_data.get("platform") or "—"),
        url=_md_escape(ctx_data.get("url") or "—"),
        error=_md_escape(str(ctx_data.get("error") or "—")[:400]),
        message=_md_escape(update.message.text or ""),
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
        db.get_message("btn_seen", "ar"), callback_data="reportseen:1"
    )]])
    target = payments.target_for("errors")
    if target:
        from telegram.error import BadRequest
        try:
            await context.bot.send_message(target, text, parse_mode="Markdown", reply_markup=markup)
        except BadRequest:
            try:
                plain = text.replace("*", "").replace("`", "").replace("_", "")
                await context.bot.send_message(target, plain, reply_markup=markup)
            except Exception:
                logger.exception("failed to send user report even as plain text")
        except Exception:
            logger.exception("failed to send user report")
    await update.message.reply_text(db.get_message("report_sent", lang))
    return True


async def handle_report_seen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """زر 'شفتها' بالقناة/الخاص: يعلّم البلاغ كمقروء باسم الأدمن اللي ضغطه."""
    query = update.callback_query
    admin = query.from_user
    await query.answer("✅")
    name = admin.full_name or (f"@{admin.username}" if admin.username else str(admin.id))
    try:
        await query.edit_message_text(f"✅ شفتها {name}")
    except Exception:
        pass



def _lang(user_id: int) -> str:
    """يجيب لغة المستخدم المحفوظة، افتراضياً عربي لو ما اختار بعد."""
    return db.get_user_language(user_id) or "ar"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_awaiting_states(update.effective_user.id)
    await _notify_admin_if_new(update, context)

    # دعم Deep Link: t.me/البوت?start=رابط_مشفر_base64 يبدأ التحميل تلقائياً (يتخطى اختيار اللغة)
    if context.args:
        url = _decode_deep_link(context.args[0])
        if url:
            platform = downloader.detect_platform(url) or ("wechat" if wechat.detect(url) else None)
            if platform:
                user = update.effective_user
                await _process_single_link(update, context, user, platform, url)
                return
        await update.message.reply_text(db.get_message("deeplink_invalid", _lang(update.effective_user.id)))
        return

    # كل ضغطة /start تعرض اختيار اللغة أول، بعدها رسالة الترحيب باللغة المختارة
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇶 العربية", callback_data="setlang:ar")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="setlang:en")],
    ])
    await update.message.reply_text(
        "اختار لغة الواجهة:\nChoose interface language:",
        reply_markup=buttons,
    )


async def handle_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, lang = query.data.split(":", 1)
    except ValueError:
        return

    user_id = query.from_user.id
    db.set_user_language(user_id, lang)

    try:
        await query.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        query.message.chat_id, db.get_message("welcome", lang), parse_mode="Markdown"
    )


def _decode_deep_link(payload: str) -> str | None:
    """يفك ترميز base64url المستخدم بـ Deep Link ويرجع الرابط الأصلي، او None لو فشل."""
    import base64
    try:
        # تليگرام يمنع = بنهاية base64 العادي، نضيفها احتياطياً حتى الفك يصير صحيح
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        return decoded if decoded.startswith("http") else None
    except Exception:
        return None


def build_deep_link(bot_username: str, url: str) -> str:
    """يبني رابط Deep Link من رابط منصة عادي - يستخدم خارج البوت (بموقع/تطبيق آخر)."""
    import base64
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"https://t.me/{bot_username}?start={encoded}"


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_awaiting_states(update.effective_user.id)
    lang = _lang(update.effective_user.id)
    text = db.get_message("help", lang)
    if _is_admin(update.effective_user.id):
        text += db.get_message("help_admin", lang)
    await _safe_markdown(update.message.reply_text, text)


async def deeplink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر أدمن: يبني رابط Deep Link جاهز من رابط منصة عادي، للاستخدام بموقع/تطبيق خارجي."""
    if not _is_admin(update.effective_user.id):
        return
    _clear_awaiting_states(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            "استخدم: /deeplink <رابط>\n"
            "مثال: /deeplink https://x.com/user/status/123"
        )
        return

    url = context.args[0]
    bot_username = (await context.bot.get_me()).username
    link = build_deep_link(bot_username, url)
    await update.message.reply_text(
        f"🔗 رابط Deep Link جاهز:\n`{link}`\n\n"
        "أي شخص يضغط عليه يفتح البوت ويبدأ التحميل تلقائياً.",
        parse_mode="Markdown",
    )


def _clear_awaiting_states(user_id: int) -> bool:
    """يمسح اي حالة انتظار تعديل معلقة (رسالة/ستيكر/حد رقمي) لهذا المستخدم.
    يرجع True لو كان فيه حالة انتظار فعلاً. تُستدعى بأول كل أمر (/command) حتى
    اي أمر يقطع تلقائياً اي تعديل معلق بدل ما ينحفظ نص الأمر نفسه بالغلط."""
    return (
        AWAITING_PROBLEM_REPORT.pop(user_id, None) is not None
        or admin_wallet.AWAITING_WALLET.pop(user_id, None) is not None
        or AWAITING_MESSAGE_EDIT.pop(user_id, None) is not None
        or AWAITING_STICKER_EDIT.pop(user_id, None) is not None
        or AWAITING_LIMIT_EDIT.pop(user_id, None) is not None
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يلغي اي محادثة تعديل معلقة (رسالة/ستيكر/حد رقمي) عالقة بانتظار نص من الأدمن."""
    was_waiting = _clear_awaiting_states(update.effective_user.id)
    lang = _lang(update.effective_user.id)
    await update.message.reply_text(db.get_message("cancel_done" if was_waiting else "cancel_none", lang))


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إحصائيات شخصية للمستخدم نفسه - كم رابط حمّل ومن اي منصة + إعداداته الشخصية."""
    user = update.effective_user
    _clear_awaiting_states(user.id)
    text, markup = _build_stats_view(user.id)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


def _build_stats_view(user_id: int):
    info = db.get_user_info(user_id)
    stats = db.get_user_link_stats(user_id)
    lang = _lang(user_id)
    gm = lambda k, **kw: db.get_message(k, lang, **kw)

    lines = [gm("stats_title")]
    if info and info.get("joined_at"):
        lines.append(f"{gm('member_since')}: {info['joined_at'].strftime('%Y-%m-%d')}")
    lines.append(f"{gm('total_downloads')}: {stats['total']}")
    for platform, count in stats["by_platform"].items():
        name = gm(f"platform_{platform}") if f"platform_{platform}" in db.DEFAULT_MESSAGES else platform
        lines.append(f"  • {name}: {count}")
    if stats["total"] == 0:
        lines.append(gm("stats_no_downloads"))

    lines.append(gm("stats_balance_title"))
    for p in wallet.PAID_PLATFORMS:
        a = wallet.availability(user_id, p)
        lines.append(gm("stats_balance_line", platform=payments.pname(p, lang), free=a["free_left"], paid=a["paid"]))
    text = "\n".join(lines)

    on_f, off_f = gm("state_on_f"), gm("state_off_f")
    on_m, off_m = gm("state_on_m"), gm("state_off_m")
    buttons = [
        [InlineKeyboardButton(f"{gm('btn_post_info')}: {on_f if _post_info_enabled(user_id) else off_f}", callback_data="pref:toggle_post_info")],
        [InlineKeyboardButton(f"{gm('btn_verify_link')}: {on_m if _verify_link_enabled(user_id) else off_m}", callback_data="pref:toggle_verify_link")],
        [InlineKeyboardButton(f"{gm('btn_preview')}: {on_f if _preview_enabled(user_id) else off_f}", callback_data="pref:toggle_preview")],
    ]
    if wallet.payments_enabled():
        buttons.append([InlineKeyboardButton(gm("btn_buy"), callback_data="buy:menu")])
    return text, InlineKeyboardMarkup(buttons)


async def handle_pref_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if query.data == "pref:toggle_post_info":
        if not db.get_setting("post_info_global_enabled", True):
            await query.answer(db.get_message("post_info_disabled", _lang(query.from_user.id)), show_alert=True)
        else:
            current = db.get_user_pref(user_id, "show_post_info", True)
            db.set_user_pref(user_id, "show_post_info", not current)
            await query.answer()
    elif query.data == "pref:toggle_verify_link":
        current = _verify_link_enabled(user_id)
        db.set_user_pref(user_id, "verify_link", not current)
        await query.answer()
    elif query.data == "pref:toggle_preview":
        if not db.get_setting("preview_global_enabled", True):
            await query.answer(db.get_message("preview_disabled", _lang(query.from_user.id)), show_alert=True)
        else:
            current = _preview_enabled(user_id)
            db.set_user_pref(user_id, "show_preview", not current)
            await query.answer()
    else:
        await query.answer()
        return

    text, markup = _build_stats_view(user_id)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)


# ==================== لوحة تحكم الأدمن ====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    _clear_awaiting_states(update.effective_user.id)

    buttons = [
        [InlineKeyboardButton("💰 الأرصدة والدفع", callback_data="adm:w:menu")],
        [InlineKeyboardButton("✏️ تعديل الرسائل", callback_data="adm:msgs")],
        [InlineKeyboardButton("🖼️ تعديل الستيكرات", callback_data="adm:stickers")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="adm:stats")],
        [InlineKeyboardButton("🏆 أكثر المستخدمين نشاطاً", callback_data="adm:top_users")],
        [InlineKeyboardButton("📄 سجل الروابط", callback_data="adm:links")],
        [InlineKeyboardButton("🚫 توقيف/تفعيل منصة", callback_data="adm:platforms")],
        [InlineKeyboardButton("🔎 التحقق من الرابط قبل التحميل (افتراضي)", callback_data="adm:verify_toggle")],
        [InlineKeyboardButton("ℹ️ معلومات المنشور (عام)", callback_data="adm:postinfo_toggle")],
        [InlineKeyboardButton("👁️ معاينة سريعة قبل التحميل (عام)", callback_data="adm:preview_toggle")],
        [InlineKeyboardButton("👥 تفعيل/تعطيل البوت بالمجاميع", callback_data="adm:groups_toggle")],
        [InlineKeyboardButton("⚙️ حدود الأحجام (تحميل/طابور)", callback_data="adm:limits")],
        [InlineKeyboardButton("🛠️ وضع الصيانة (إيقاف الرد للمستخدمين)", callback_data="adm:maintenance_toggle")],
        [InlineKeyboardButton("🗑️ حذف رسائل الخطأ تلقائياً", callback_data="adm:autodelete")],
        [InlineKeyboardButton("⛔ حظر مستخدم", callback_data="adm:ban_help")],
    ]
    await update.message.reply_text(
        "🛠️ لوحة تحكم الأدمن", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_admin(query.from_user.id):
        await query.answer("هذا القسم للأدمن بس 🚫", show_alert=True)
        return
    await query.answer()

    data = query.data

    if data.startswith("adm:w:"):
        await admin_wallet.handle_wallet_callback(update, context, _is_admin)
        return

    if data == "adm:msgs" or data.startswith("adm:msgs:"):
        # تقسيم بصفحات: 60+ رسالة ما تنحط بشاشة وحدة (حد تيليگرام 100 زر و ~4000 حرف)
        page = int(data.split(":")[2]) if data.startswith("adm:msgs:") else 0
        per_page = 12
        items = list(EDITABLE_MESSAGES.items())
        total_pages = max((len(items) + per_page - 1) // per_page, 1)
        page = min(max(page, 0), total_pages - 1)
        chunk = items[page * per_page:(page + 1) * per_page]

        buttons = [[InlineKeyboardButton(label, callback_data=f"adm:msg:{key}")] for key, label in chunk]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"adm:msgs:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="adm:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"adm:msgs:{page + 1}"))
        buttons.append(nav)
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
        await query.edit_message_text(
            "اختار الرسالة اللي تريد تعدلها 👇\n(بعدها تختار العربي او الإنكليزي)",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:noop":
        pass

    elif data.startswith("adm:msg:"):
        # الصيغة: adm:msg:{key}            -> خطوة اختيار اللغة
        #         adm:msg:{key}:{ar|en}    -> عرض النص الحالي وانتظار النص الجديد
        parts = data.split(":")
        key = parts[2]
        lang_sel = parts[3] if len(parts) > 3 else None

        if lang_sel not in ("ar", "en"):
            buttons = [
                [InlineKeyboardButton("🇮🇶 عربي", callback_data=f"adm:msg:{key}:ar"),
                 InlineKeyboardButton("🇬🇧 English", callback_data=f"adm:msg:{key}:en")],
                [InlineKeyboardButton("⬅️ رجوع", callback_data=f"adm:msgs:{list(EDITABLE_MESSAGES).index(key) // 12 if key in EDITABLE_MESSAGES else 0}")],
            ]
            await query.edit_message_text(
                f"✏️ *{EDITABLE_MESSAGES.get(key, key)}*\n\nأي نسخة تريد تعدل؟ 👇",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            current = db.get_message(key, lang_sel)
            AWAITING_MESSAGE_EDIT[query.from_user.id] = (key, lang_sel)
            flag = "🇮🇶 عربي" if lang_sel == "ar" else "🇬🇧 English"
            var_hint = MESSAGE_VARIABLE_HINTS.get(
                key,
                "تكدر تستخدم `{error}` او `{max_size}` او `{position}` حسب نوع الرسالة، "
                "خلهم كما هم لو ما تعرف وين تنحط."
            )
            await query.edit_message_text(
                f"📝 النص الحالي لـ *{EDITABLE_MESSAGES.get(key, key)}* ({flag}):\n\n"
                f"`{current}`\n\n"
                f"ارسل النص الجديد هسه كرسالة عادية.\n\n{var_hint}",
                parse_mode="Markdown",
            )

    elif data == "adm:stickers":
        buttons = []
        for key, label in STICKER_LABELS.items():
            state = "✅" if db.get_sticker(key) else "❌"
            buttons.append([InlineKeyboardButton(
                f"{state} {label}", callback_data=f"adm:sticker:{key}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
        await query.edit_message_text(
            "اختار الستيكر اللي تريد تحدده 👇", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("adm:sticker:"):
        key = data.split(":", 2)[2]
        AWAITING_STICKER_EDIT[query.from_user.id] = key
        current = db.get_sticker(key)
        buttons = []
        if current:
            buttons.append([InlineKeyboardButton("🗑️ إلغاء الستيكر (رجوع للنص العادي)", callback_data=f"adm:sticker_rm:{key}")])
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:stickers")])
        await query.edit_message_text(
            f"📤 ارسل الستيكر اللي تريده لـ *{STICKER_LABELS.get(key, key)}* هسه.\n\n"
            + ("او الغيه بالزر تحت 👇" if current else ""),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("adm:sticker_rm:"):
        key = data.split(":", 2)[2]
        AWAITING_STICKER_EDIT.pop(query.from_user.id, None)
        db.remove_sticker(key)
        await query.answer("تم الإلغاء ✅", show_alert=False)
        # نرجع لقائمة الستيكرات محدثة
        buttons = []
        for k, label in STICKER_LABELS.items():
            state = "✅" if db.get_sticker(k) else "❌"
            buttons.append([InlineKeyboardButton(
                f"{state} {label}", callback_data=f"adm:sticker:{k}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
        await query.edit_message_text(
            "اختار الستيكر اللي تريد تحدده 👇", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "adm:stats":
        stats = db.get_stats()
        text = (
            "📊 *إحصائيات البوت*\n\n"
            f"👥 عدد المستخدمين: {stats['users']}\n"
            f"🔗 عدد الروابط المسجلة: {stats['links']}\n"
            f"⛔ عدد المحظورين: {stats['banned']}\n"
        )

        # استهلاك MongoDB (من 512 ميكا المجانية)
        storage = db.get_storage_stats()
        if storage:
            text += (
                f"\n🗄️ *قاعدة البيانات (MongoDB)*\n"
                f"مستخدم: {storage['total_mb']} ميكا من {storage['free_tier_limit_mb']} "
                f"({storage['percent_used']}%)\n"
            )

        # استهلاك TikHub (اذا مفعّل)
        if tikhub.is_configured():
            usage = tikhub.get_usage()
            if usage:
                text += (
                    f"\n🌐 *TikHub*\n"
                    f"الرصيد: ${usage['balance']:.4f}\n"
                    f"الرصيد المجاني المتبقي: ${usage['free_credit']:.4f}\n"
                )
            else:
                text += "\n🌐 *TikHub*: تعذر جلب البيانات حالياً\n"

            daily = tikhub.get_daily_usage()
            if daily:
                requests_today = daily.get("total_requests") or daily.get("requests") or "؟"
                cost_today = daily.get("total_cost") or daily.get("cost")
                text += f"📅 استهلاك اليوم: {requests_today} طلب"
                if cost_today is not None:
                    text += f" (${float(cost_today):.4f})"
                text += "\n"

        # رابط Render (استهلاك RAM/المساحة ما يوصله البوت برمجياً)
        if config.RENDER_DASHBOARD_URL:
            text += f"\n☁️ استهلاك السيرفر (RAM/مساحة): [افتح لوحة Render]({config.RENDER_DASHBOARD_URL})"

        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )

    elif data == "adm:top_users":
        top = db.get_top_users(10)
        if not top:
            text = "ماكو بيانات كافية لحد هسه 📭"
        else:
            lines = ["🏆 *أكثر 10 مستخدمين نشاطاً*\n"]
            medals = ["🥇", "🥈", "🥉"]
            for i, u in enumerate(top):
                medal = medals[i] if i < 3 else f"{i + 1}."
                name = f"@{u['username']}" if u["username"] else f"آيدي {u['user_id']}"
                lines.append(f"{medal} {name} — {u['count']} تحميل")
            text = "\n".join(lines)
        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "adm:links":
        text = db.export_links_text()
        if not text:
            await query.edit_message_text(
                "ماكو روابط مسجلة لحد هسه 📭",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
                ),
            )
            return
        import io
        buf = io.BytesIO(text.encode("utf-8"))
        buf.name = "links.txt"
        await context.bot.send_document(query.message.chat_id, buf, filename="links.txt")

    elif data == "adm:platforms":
        buttons = []
        for platform, label in PLATFORM_LABELS:
            state = "🔴 موقوفة" if db.is_platform_disabled(platform) else "🟢 شغالة"
            action = "enable" if db.is_platform_disabled(platform) else "disable"
            buttons.append([InlineKeyboardButton(
                f"{label}: {state}", callback_data=f"adm:plat:{action}:{platform}"
            )])
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
        await query.edit_message_text("اضغط على المنصة لتغيير حالتها 👇", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm:plat:"):
        _, _, action, platform = data.split(":", 3)
        if action == "disable":
            db.disable_platform(platform)
        else:
            db.enable_platform(platform)
        await admin_callback_refresh_platforms(query)

    elif data == "adm:ban_help":
        await query.edit_message_text(
            "لحظر مستخدم استخدم الأمر:\n`/ban <آيدي المستخدم>`\n\n"
            "ولإلغاء الحظر:\n`/unban <آيدي المستخدم>`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
            ),
        )

    elif data == "adm:back":
        buttons = [
            [InlineKeyboardButton("💰 الأرصدة والدفع", callback_data="adm:w:menu")],
            [InlineKeyboardButton("✏️ تعديل الرسائل", callback_data="adm:msgs")],
            [InlineKeyboardButton("🖼️ تعديل الستيكرات", callback_data="adm:stickers")],
            [InlineKeyboardButton("📊 إحصائيات", callback_data="adm:stats")],
        [InlineKeyboardButton("🏆 أكثر المستخدمين نشاطاً", callback_data="adm:top_users")],
            [InlineKeyboardButton("📄 سجل الروابط", callback_data="adm:links")],
            [InlineKeyboardButton("🚫 توقيف/تفعيل منصة", callback_data="adm:platforms")],
            [InlineKeyboardButton("🔎 التحقق من الرابط قبل التحميل (افتراضي)", callback_data="adm:verify_toggle")],
            [InlineKeyboardButton("ℹ️ معلومات المنشور (عام)", callback_data="adm:postinfo_toggle")],
        [InlineKeyboardButton("👁️ معاينة سريعة قبل التحميل (عام)", callback_data="adm:preview_toggle")],
        [InlineKeyboardButton("👥 تفعيل/تعطيل البوت بالمجاميع", callback_data="adm:groups_toggle")],
        [InlineKeyboardButton("⚙️ حدود الأحجام (تحميل/طابور)", callback_data="adm:limits")],
        [InlineKeyboardButton("🛠️ وضع الصيانة (إيقاف الرد للمستخدمين)", callback_data="adm:maintenance_toggle")],
        [InlineKeyboardButton("🗑️ حذف رسائل الخطأ تلقائياً", callback_data="adm:autodelete")],
            [InlineKeyboardButton("⛔ حظر مستخدم", callback_data="adm:ban_help")],
        ]
        await query.edit_message_text("🛠️ لوحة تحكم الأدمن", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "adm:verify_toggle":
        current = db.get_setting("verify_link_before_download", True)
        db.set_setting("verify_link_before_download", not current)
        new_state = "🟢 مفعّل" if not current else "🔴 موقف"
        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
        await query.edit_message_text(
            f"التحقق من الرابط قبل التحميل (الافتراضي العام) صار: {new_state}\n\n"
            "ملاحظة: هذا يحدد الافتراضي بس - كل مستخدم يقدر يغيره لحاله من /stats.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:postinfo_toggle":
        current = db.get_setting("post_info_global_enabled", True)
        db.set_setting("post_info_global_enabled", not current)
        new_state = "🟢 مفعّلة" if not current else "🔴 موقفة بالكامل"
        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
        await query.edit_message_text(
            f"معلومات المنشور (عام لكل المستخدمين) صارت: {new_state}\n\n"
            + ("" if not current else "ملاحظة: هذا يوقفها للكل بلا استثناء، حتى لو المستخدم مفعّلها لحاله."),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:preview_toggle":
        current = db.get_setting("preview_global_enabled", True)
        db.set_setting("preview_global_enabled", not current)
        new_state = "🟢 مفعّلة" if not current else "🔴 موقفة بالكامل"
        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
        await query.edit_message_text(
            f"المعاينة السريعة (عام لكل المستخدمين) صارت: {new_state}\n\n"
            + ("" if not current else "ملاحظة: هذا يوقفها للكل بلا استثناء، حتى لو المستخدم مفعّلها لحاله."),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:groups_toggle":
        current = db.get_setting("groups_enabled", True)
        db.set_setting("groups_enabled", not current)
        new_state = "🟢 مفعّل" if not current else "🔴 موقف"
        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
        await query.edit_message_text(
            f"عمل البوت داخل المجاميع/القنوات صار: {new_state}\n\n"
            "ملاحظة: لازم تعطل Privacy Mode من BotFather حتى يقدر البوت يشوف "
            "روابط بالمجموعة (مو بس الرسائل اللي تمنشنه او تكون /command).",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:limits":
        buttons = [
            [InlineKeyboardButton(
                f"📦 {LIMIT_LABELS['max_file_size_mb']}: {_max_file_size_mb()} ميكا",
                callback_data="adm:limit_edit:max_file_size_mb",
            )],
            [InlineKeyboardButton(
                f"⏳ {LIMIT_LABELS['heavy_file_threshold_mb']}: {_heavy_threshold_mb()} ميكا",
                callback_data="adm:limit_edit:heavy_file_threshold_mb",
            )],
            [InlineKeyboardButton(
                f"🤖 {LIMIT_LABELS['main_ping_interval_min']}: {_get_limit_value('main_ping_interval_min')} دقايق",
                callback_data="adm:limit_edit:main_ping_interval_min",
            )],
            [InlineKeyboardButton(
                f"🈶 {LIMIT_LABELS['wechat_ping_interval_min']}: {_get_limit_value('wechat_ping_interval_min')} دقايق",
                callback_data="adm:limit_edit:wechat_ping_interval_min",
            )],
            [InlineKeyboardButton(
                f"🈶 بينك خدمة ويشات: {'🟢 مفعّل' if db.get_setting('wechat_ping_enabled', True) else '🔴 موقف'}",
                callback_data="adm:wechat_ping_toggle",
            )],
            [InlineKeyboardButton(
                f"⚠️ {LIMIT_LABELS['failure_alert_threshold']}: {_get_limit_value('failure_alert_threshold')}",
                callback_data="adm:limit_edit:failure_alert_threshold",
            )],
            [InlineKeyboardButton("🔀 المحاولة البديلة (دويين)", callback_data="adm:fallback_menu")],
            [InlineKeyboardButton("🔀 المحاولة البديلة (RedNote)", callback_data="adm:rednote_fallback_menu")],
            [InlineKeyboardButton("🔀 المحاولة البديلة (ويشات)", callback_data="adm:wechat_fallback_menu")],
            [InlineKeyboardButton(
                f"💵 {LIMIT_LABELS['low_tikhub_balance_usd']}: {_get_limit_value('low_tikhub_balance_usd')}$",
                callback_data="adm:limit_edit:low_tikhub_balance_usd",
            )],
            [InlineKeyboardButton("⏱️ لمت حجم الملفات (توفير البندويث)", callback_data="adm:size_limit_menu")],
            [InlineKeyboardButton("🔗 الرابط المباشر (X)", callback_data="adm:direct_link_menu")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")],
        ]
        await query.edit_message_text(
            "اضغط على الحد اللي تريد تغيره 👇", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "adm:direct_link_menu" or data == "adm:direct_link_toggle":
        if data == "adm:direct_link_toggle":
            db.set_setting("direct_link_enabled", not db.get_setting("direct_link_enabled", True))
            await query.answer("تم التغيير ✅")
        enabled = db.get_setting("direct_link_enabled", True)
        buttons = [
            [InlineKeyboardButton(
                f"الرابط المباشر: {'🟢 مفعّل' if enabled else '🔴 موقف'}", callback_data="adm:direct_link_toggle",
            )],
            [InlineKeyboardButton(
                f"📦 {LIMIT_LABELS['direct_link_threshold_mb']}: {_get_limit_value('direct_link_threshold_mb')} ميكا",
                callback_data="adm:limit_edit:direct_link_threshold_mb",
            )],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="adm:limits")],
        ]
        await query.edit_message_text(
            "🔗 *الرابط المباشر لمنصة X*\n\n"
            "لما حجم فيديو X المتوقع يتجاوز الحد المحدد، بدل ما نحمله على سيرفرنا ونرفعه "
            "لتليگرام، نعرض للمستخدم زر يفتح رابط الفيديو المباشر بمتصفحه - يوفر بندويث "
            "سيرفرنا بالكامل تقريباً لهذي الحالة.\n\n"
            "غيّر أي قيمة 👇",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:size_limit_menu":
        enabled = db.get_setting("size_limit_enabled", True)
        bypassed = db.list_size_limit_bypass()
        buttons = [
            [InlineKeyboardButton(
                f"لمت حجم الملفات: {'🟢 مفعّل' if enabled else '🔴 موقف'}", callback_data="adm:size_limit_toggle",
            )],
            [InlineKeyboardButton(
                f"📦 {LIMIT_LABELS['size_limit_trigger_mb']}: {_get_limit_value('size_limit_trigger_mb')} ميكا",
                callback_data="adm:limit_edit:size_limit_trigger_mb",
            )],
            [InlineKeyboardButton(
                f"⏳ {LIMIT_LABELS['size_limit_duration_hours']}: {_get_limit_value('size_limit_duration_hours')} ساعة",
                callback_data="adm:limit_edit:size_limit_duration_hours",
            )],
            [InlineKeyboardButton(
                f"📉 {LIMIT_LABELS['size_limit_small_mb']}: {_get_limit_value('size_limit_small_mb')} ميكا",
                callback_data="adm:limit_edit:size_limit_small_mb",
            )],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="adm:limits")],
        ]
        await query.edit_message_text(
            "⏱️ *لمت حجم الملفات*\n\n"
            "أي ملف يوصل حجمه لعتبة التفعيل يبدأ فترة لمت للمستخدم، خلالها ما يكدر يحمل "
            "إلا ملفات أصغر من الحد المسموح. المشتركين اللي عندهم رصيد مدفوع (أي منصة) "
            "والأدمن يتخطون اللمت تلقائياً. لإعفاء مستخدم يدوياً: `/sizebypass <آيدي>` "
            f"(المعفيين حالياً: {len(bypassed)}).\n\n"
            "غيّر أي قيمة 👇",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:size_limit_toggle":
        current = db.get_setting("size_limit_enabled", True)
        db.set_setting("size_limit_enabled", not current)
        await query.answer("تم التغيير ✅")
        enabled = not current
        bypassed = db.list_size_limit_bypass()
        buttons = [
            [InlineKeyboardButton(
                f"لمت حجم الملفات: {'🟢 مفعّل' if enabled else '🔴 موقف'}", callback_data="adm:size_limit_toggle",
            )],
            [InlineKeyboardButton(
                f"📦 {LIMIT_LABELS['size_limit_trigger_mb']}: {_get_limit_value('size_limit_trigger_mb')} ميكا",
                callback_data="adm:limit_edit:size_limit_trigger_mb",
            )],
            [InlineKeyboardButton(
                f"⏳ {LIMIT_LABELS['size_limit_duration_hours']}: {_get_limit_value('size_limit_duration_hours')} ساعة",
                callback_data="adm:limit_edit:size_limit_duration_hours",
            )],
            [InlineKeyboardButton(
                f"📉 {LIMIT_LABELS['size_limit_small_mb']}: {_get_limit_value('size_limit_small_mb')} ميكا",
                callback_data="adm:limit_edit:size_limit_small_mb",
            )],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="adm:limits")],
        ]
        await query.edit_message_text(
            "⏱️ *لمت حجم الملفات*\n\n"
            "أي ملف يوصل حجمه لعتبة التفعيل يبدأ فترة لمت للمستخدم، خلالها ما يكدر يحمل "
            "إلا ملفات أصغر من الحد المسموح. المشتركين اللي عندهم رصيد مدفوع (أي منصة) "
            "والأدمن يتخطون اللمت تلقائياً. لإعفاء مستخدم يدوياً: `/sizebypass <آيدي>` "
            f"(المعفيين حالياً: {len(bypassed)}).\n\n"
            "غيّر أي قيمة 👇",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:fallback_menu":
        buttons = _build_fallback_menu_buttons("douyin")
        await query.edit_message_text(
            "إعدادات المحاولة البديلة لدويين (عبر TikHub) 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:wechat_fallback_menu":
        buttons = _build_fallback_menu_buttons("wechat")
        await query.edit_message_text(
            "إعدادات المحاولة البديلة لويشات (TikHub + فك التشفير) 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:wechat_fallback_toggle":
        current = db.get_setting("wechat_fallback_enabled", True)
        db.set_setting("wechat_fallback_enabled", not current)
        await query.answer("تم التغيير ✅")
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(_build_fallback_menu_buttons("wechat"))
        )

    elif data == "adm:rednote_fallback_menu":
        buttons = _build_fallback_menu_buttons("rednote")
        await query.edit_message_text(
            "إعدادات المحاولة البديلة لـ RedNote (عبر TikHub) 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:fallback_toggle" or data == "adm:rednote_fallback_toggle":
        platform = "rednote" if data == "adm:rednote_fallback_toggle" else "douyin"
        current = db.get_setting(f"{platform}_fallback_enabled", True)
        db.set_setting(f"{platform}_fallback_enabled", not current)
        await query.answer("تم التغيير ✅")
        label = "RedNote" if platform == "rednote" else "دويين"
        buttons = _build_fallback_menu_buttons(platform)
        await query.edit_message_text(
            f"إعدادات المحاولة البديلة لـ{label} (عبر TikHub) 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:fallback_fail_del_toggle":
        current = db.get_setting("fallback_failure_autodelete_enabled", False)
        db.set_setting("fallback_failure_autodelete_enabled", not current)
        await query.answer("تم التغيير ✅")
        buttons = _build_fallback_menu_buttons("douyin")
        await query.edit_message_text(
            "إعدادات المحاولة البديلة لدويين (عبر TikHub) 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:rednote_fallback_fail_del_toggle":
        current = db.get_setting("fallback_failure_autodelete_enabled", False)
        db.set_setting("fallback_failure_autodelete_enabled", not current)
        await query.answer("تم التغيير ✅")
        buttons = _build_fallback_menu_buttons("rednote")
        await query.edit_message_text(
            "إعدادات المحاولة البديلة لـ RedNote (عبر TikHub) 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:wechat_fallback_fail_del_toggle":
        current = db.get_setting("fallback_failure_autodelete_enabled", False)
        db.set_setting("fallback_failure_autodelete_enabled", not current)
        await query.answer("تم التغيير ✅")
        buttons = _build_fallback_menu_buttons("wechat")
        await query.edit_message_text(
            "إعدادات المحاولة البديلة لويشات (TikHub + فك التشفير) 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("adm:limit_edit:"):
        key = data.split(":", 2)[2]
        current = _get_limit_value(key)
        unit = LIMIT_UNITS.get(key, "")
        AWAITING_LIMIT_EDIT[query.from_user.id] = key
        await query.edit_message_text(
            f"القيمة الحالية لـ *{LIMIT_LABELS[key]}*: {current} {unit}\n\n"
            f"ارسل الرقم الجديد هسه (بـ{unit}).",
            parse_mode="Markdown",
        )

    elif data == "adm:wechat_ping_toggle":
        current = db.get_setting("wechat_ping_enabled", True)
        db.set_setting("wechat_ping_enabled", not current)
        new_state = "🔴 موقف" if current else "🟢 مفعّل"
        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:limits")]]
        await query.edit_message_text(
            f"بينك خدمة ويشات صار: {new_state}\n\n"
            "ملاحظة: هذا مالة علاقة بالبوت الرئيسي إطلاقاً - يوقف بس بينك خدمة فك تشفير ويشات المنفصلة.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:maintenance_toggle":
        current = db.get_setting("maintenance_mode", False)
        db.set_setting("maintenance_mode", not current)
        new_state = "🔴 مفعّل (البوت متوقف عن الرد للمستخدمين)" if not current else "🟢 موقف (البوت شغال عادي)"
        buttons = [[InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")]]
        await query.edit_message_text(
            f"وضع الصيانة صار: {new_state}\n\n"
            "ملاحظة: انت (الأدمن) تقدر تستخدم البوت عادي حتى وهو بوضع الصيانة.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "adm:autodelete":
        buttons = []
        for key, label in AUTODELETE_LABELS.items():
            enabled = db.get_setting(f"{key}_autodelete_enabled", False)
            minutes = db.get_setting(f"{key}_autodelete_min", 5)
            state = f"🟢 مفعّل ({minutes} دقيقة)" if enabled else "🔴 موقف"
            buttons.append([InlineKeyboardButton(f"{label}: {state}", callback_data=f"adm:ad_toggle:{key}")])
            buttons.append([InlineKeyboardButton(f"⏱️ عدل مدة {label}", callback_data=f"adm:ad_time:{key}")])
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
        await query.edit_message_text(
            "تحكم بحذف رسائل الخطأ تلقائياً 👇", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("adm:ad_toggle:"):
        key = data.split(":", 2)[2]
        current = db.get_setting(f"{key}_autodelete_enabled", False)
        db.set_setting(f"{key}_autodelete_enabled", not current)
        await query.answer("تم التغيير ✅")
        # نرجع نبني نفس قائمة adm:autodelete محدثة
        buttons = []
        for k, label in AUTODELETE_LABELS.items():
            enabled = db.get_setting(f"{k}_autodelete_enabled", False)
            minutes = db.get_setting(f"{k}_autodelete_min", 5)
            state = f"🟢 مفعّل ({minutes} دقيقة)" if enabled else "🔴 موقف"
            buttons.append([InlineKeyboardButton(f"{label}: {state}", callback_data=f"adm:ad_toggle:{k}")])
            buttons.append([InlineKeyboardButton(f"⏱️ عدل مدة {label}", callback_data=f"adm:ad_time:{k}")])
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
        await query.edit_message_text(
            "تحكم بحذف رسائل الخطأ تلقائياً 👇", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("adm:ad_time:"):
        key = data.split(":", 2)[2]
        current = db.get_setting(f"{key}_autodelete_min", 5)
        AWAITING_LIMIT_EDIT[query.from_user.id] = f"{key}_autodelete_min"
        await query.edit_message_text(
            f"المدة الحالية لحذف *{AUTODELETE_LABELS[key]}*: {current} دقيقة\n\n"
            "ارسل عدد الدقايق الجديد هسه.",
            parse_mode="Markdown",
        )


async def admin_callback_refresh_platforms(query):
    buttons = []
    for platform, label in PLATFORM_LABELS:
        state = "🔴 موقوفة" if db.is_platform_disabled(platform) else "🟢 شغالة"
        action = "enable" if db.is_platform_disabled(platform) else "disable"
        buttons.append([InlineKeyboardButton(
            f"{label}: {state}", callback_data=f"adm:plat:{action}:{platform}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
    await query.edit_message_text("اضغط على المنصة لتغيير حالتها 👇", reply_markup=InlineKeyboardMarkup(buttons))


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    _clear_awaiting_states(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("استخدم: /ban <آيدي المستخدم>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("آيدي غير صالح ❌")
        return
    db.ban_user(target_id)
    await update.message.reply_text(f"تم حظر المستخدم {target_id} ✅")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    _clear_awaiting_states(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("استخدم: /unban <آيدي المستخدم>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("آيدي غير صالح ❌")
        return
    db.unban_user(target_id)
    await update.message.reply_text(f"تم إلغاء حظر المستخدم {target_id} ✅")


async def sizebypass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أدمن: يعفي مستخدم يدوياً من لمت حجم الملفات (منفصل عن إعفاء المشتركين المدفوعين التلقائي)."""
    if not _is_admin(update.effective_user.id):
        return
    _clear_awaiting_states(update.effective_user.id)
    if not context.args:
        bypassed = db.list_size_limit_bypass()
        text = ("لا يوجد مستخدمين معفيين حالياً." if not bypassed
                else "المستخدمين المعفيين من لمت الحجم:\n" + "\n".join(f"• `{u}`" for u in bypassed))
        text += "\n\nلإضافة: `/sizebypass <آيدي المستخدم>`\nللإزالة: `/unsizebypass <آيدي المستخدم>`"
        await update.message.reply_text(text, parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("آيدي غير صالح ❌")
        return
    db.add_size_limit_bypass(target_id)
    await update.message.reply_text(f"تم إعفاء المستخدم {target_id} من لمت حجم الملفات ✅")


async def unsizebypass_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    _clear_awaiting_states(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("استخدم: /unsizebypass <آيدي المستخدم>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("آيدي غير صالح ❌")
        return
    db.remove_size_limit_bypass(target_id)
    await update.message.reply_text(f"تم إلغاء إعفاء المستخدم {target_id} ✅")



async def handle_admin_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id) or user.id not in AWAITING_STICKER_EDIT:
        return

    key = AWAITING_STICKER_EDIT.pop(user.id)
    file_id = update.message.sticker.file_id
    db.set_sticker(key, file_id)
    await update.message.reply_text(f"✅ تحدد ستيكر: {STICKER_LABELS.get(key, key)}")


# ==================== الرسائل العادية ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    raw_text = update.message.text or ""

    # طبقة حماية إضافية: أي نص يبدأ بـ / (أمر) ما ينحفظ كقيمة تعديل معلقة إطلاقاً،
    # حتى لو وصل لهذا المعالج بأي طريقة - نلغي الحالة المعلقة ونكمل معالجته كنص عادي
    if raw_text.startswith("/"):
        _clear_awaiting_states(user.id)

    # اذا المستخدم (اي مستخدم، مو بس الأدمن) بانتظار يكتب نص شكوى بعد زر الإبلاغ.
    # استثناء: لو أرسل رابط منصة مدعوم بدل الشكوى، نعتبره غيّر رأيه ونكمل كتحميل عادي.
    if user.id in AWAITING_PROBLEM_REPORT and not _extract_all_links(raw_text):
        if await _handle_problem_report_text(update, context):
            return
    elif user.id in AWAITING_PROBLEM_REPORT:
        AWAITING_PROBLEM_REPORT.pop(user.id, None)

    # اذا الأدمن بحالة انتظار لوحة الأرصدة (آيدي مستخدم / كمية / باقات)
    elif _is_admin(user.id) and user.id in admin_wallet.AWAITING_WALLET:
        if await admin_wallet.handle_wallet_text(update, context):
            return

    # اذا الأدمن ينتظر منه نص تعديل رسالة
    elif _is_admin(user.id) and user.id in AWAITING_MESSAGE_EDIT:
        key, edit_lang = AWAITING_MESSAGE_EDIT.pop(user.id)
        new_text = update.message.text
        db.set_message(key, new_text, edit_lang)
        flag = "🇮🇶 عربي" if edit_lang == "ar" else "🇬🇧 English"
        await update.message.reply_text(f"✅ تحدثت رسالة: {EDITABLE_MESSAGES.get(key, key)} ({flag})")
        return

    # اذا الأدمن ينتظر منه رقم حد جديد (حجم ملف / حد الطابور / فاصل بينك)
    elif _is_admin(user.id) and user.id in AWAITING_LIMIT_EDIT:
        key = AWAITING_LIMIT_EDIT.pop(user.id)
        try:
            new_value = int((update.message.text or "").strip())
            if new_value <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("لازم ترسل رقم صحيح اكبر من صفر ❌")
            return
        db.set_setting(key, new_value)
        unit = LIMIT_UNITS.get(key, "")
        await update.message.reply_text(f"✅ صار {LIMIT_LABELS[key]}: {new_value} {unit}")
        return

    await _notify_admin_if_new(update, context)

    if db.is_banned(user.id):
        await update.message.reply_text(db.get_message("user_banned", _lang(user.id)))
        return

    if db.get_setting("maintenance_mode", False) and not _is_admin(user.id):
        await update.message.reply_text(db.get_message("maintenance_mode", _lang(user.id)))
        return

    text = update.message.text or ""
    is_group = update.effective_chat.type in ("group", "supergroup")

    # نستخرج كل الروابط المدعومة الموجودة بالرسالة (كل سطر/رابط منفصل)
    links = _extract_all_links(text)

    if not links:
        # بالمجاميع/القنوات نتجاهل الرسائل العادية بصمت حتى ما نزعج المحادثة
        if not is_group:
            await update.message.reply_text(db.get_message("unsupported_link", _lang(user.id)))
        return

    if is_group and db.get_setting("groups_enabled", True) is False:
        return

    if len(links) > 1:
        await update.message.reply_text(
            db.get_message("multi_links", _lang(user.id), count=len(links))
        )

    for platform, url in links:
        await _process_single_link(update, context, user, platform, url)


def _extract_all_links(text: str) -> list[tuple[str, str]]:
    """يستخرج كل الروابط المدعومة (X/دويين/ويشات) من نص قد يحتوي عدة روابط."""
    results = []
    for line in text.split():
        if wechat.detect(line):
            results.append(("wechat", wechat.extract_url(line)))
            continue
        platform = downloader.detect_platform(line)
        if platform:
            results.append((platform, downloader.extract_url(line, platform)))
    return results


async def _process_single_link(update, context, user, platform: str, url: str, fail_count: int = 0):
    lang = _lang(user.id)
    if platform == "wechat":
        if db.is_platform_disabled("wechat"):
            await update.message.reply_text(db.get_message("platform_disabled", lang))
            return
        if not wechat.is_configured() or not db.get_setting("wechat_fallback_enabled", True):
            await update.message.reply_text(db.get_message("wechat_disabled", lang))
            return
        # ويشات ما عندها yt-dlp: هي دائماً "المحاولة البديلة" (TikHub + فك التشفير)،
        # فنعرض تأكيد الخصم/الشراء مباشرة من أول رابط.
        await _show_fallback_prompt(context, update.effective_chat.id, user, url, "wechat")
        return

    if db.is_platform_disabled(platform):
        await update.message.reply_text(db.get_message("platform_disabled", lang))
        return

    if not _is_admin(user.id):
        db.log_link(user.id, user.username or "", platform, url)

    if _verify_link_enabled(user.id):
        check_msg = await update.message.reply_text(db.get_message("verifying_link", lang))
        ok = await downloader.verify_link(url, platform)
        await check_msg.delete()
        if not ok:
            text = db.get_message("link_verify_failed", lang, url=url)
            keyboard = _retry_keyboard(url, platform, fail_count + 1, lang)
            sent = await update.message.reply_text(text, reply_markup=keyboard)
            await _schedule_auto_delete(context, sent.chat_id, sent.message_id, "download_error")
            return

    if _preview_enabled(user.id):
        preview = await downloader.get_preview(url, platform)
        if preview and preview.get("thumbnail"):
            duration = preview.get("duration")
            duration_txt = f"⏱️ {int(duration // 60)}:{int(duration % 60):02d}" if duration else ""
            title = preview.get("title", "")
            if len(title) > 150:
                title = title[:150] + "..."
            caption = f"👁️ معاينة سريعة\n{title}\n{duration_txt}".strip()
            try:
                await context.bot.send_photo(update.effective_chat.id, preview["thumbnail"], caption=caption)
            except Exception:
                pass  # المعاينة اختيارية - ما نوقف التحميل لو فشلت

    if platform in downloader.QUALITY_CHOICE_PLATFORMS:
        await _handle_x(update, context, url, platform, fail_count)
    else:
        await _handle_auto_download(update, context, url, platform, fail_count)


# تخزين مؤقت: retry_id قصير -> (url, platform, fail_count) لزر "أعد المحاولة" / "محاولة بديلة"
RETRY_PENDING: dict[str, tuple[str, str, int]] = {}


FALLBACK_CAPABLE_PLATFORMS = {"douyin", "rednote", "wechat"}


def _retry_keyboard(url: str, platform: str, fail_count: int = 1, lang: str = "ar") -> InlineKeyboardMarkup:
    """يبني كيبورد الأزرار حسب عدد الفشل المتتالي لنفس الرابط:
    - دويين/RedNote/ويشات (منصات المحاولة البديلة المفعّلة):
        فشلة أولى:  🔀 محاولة بديلة + 🔄 أعد المحاولة + ❌ إلغاء
        فشلة ثانية فأكثر: 🔀 محاولة بديلة + ❌ إلغاء (يختفي زر الإعادة)
    - باقي المنصات (X، Bilibili): 🔄 أعد المحاولة + ❌ إلغاء كما كان.
    ملاحظة: yt-dlp يبقى أول محاولة دائماً، هذا التعديل بس للأزرار اللي تطلع بعد ما يفشل."""
    retry_id = uuid.uuid4().hex[:10]
    RETRY_PENDING[retry_id] = (url, platform, fail_count)

    show_fallback = (
        platform in FALLBACK_CAPABLE_PLATFORMS
        and tikhub.is_configured()
        and (platform != "wechat" or wechat.is_configured())
        and db.get_setting(f"{platform}_fallback_enabled", True)
    )
    # ويشات ما عندها yt-dlp اصلاً، فالإعادة عندها معناها نفس الطريقة (مفيدة لو Render كان نايم)
    show_retry = (fail_count <= 1) if show_fallback else True

    buttons = []
    if show_fallback:
        buttons.append([InlineKeyboardButton(db.get_message("btn_fallback", lang), callback_data=f"fallback:{retry_id}")])
    if show_retry:
        buttons.append([InlineKeyboardButton(db.get_message("btn_retry", lang), callback_data=f"retry:{retry_id}")])
    buttons.append([InlineKeyboardButton(db.get_message("btn_cancel_op", lang), callback_data=f"cancelop:{retry_id}")])
    return InlineKeyboardMarkup(buttons)


async def _schedule_auto_delete(context, chat_id: int, message_id: int, setting_key: str):
    """يجدول حذف رسالة تلقائياً بعد المدة المحددة بـ /admin (بالدقايق)، اذا الميزة مفعّلة."""
    enabled = db.get_setting(f"{setting_key}_autodelete_enabled", False)
    if not enabled:
        return
    minutes = int(db.get_setting(f"{setting_key}_autodelete_min", 5))

    async def _delete_later():
        await asyncio.sleep(max(minutes, 1) * 60)
        try:
            await context.bot.delete_message(chat_id, message_id)
        except Exception:
            pass  # ممكن الرسالة تكون انحذفت او تغيرت يدوياً - عادي نتجاهل

    asyncio.create_task(_delete_later())


async def _schedule_auto_delete_seconds(context, chat_id: int, message_id: int, seconds: int):
    """يجدول حذف رسالة بعد عدد ثواني محدد - يستخدم لرسالة فشل المحاولة البديلة."""
    async def _delete_later():
        await asyncio.sleep(max(seconds, 1))
        try:
            await context.bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    asyncio.create_task(_delete_later())


async def _send_error_with_retry(context, chat_id: int, msg, error: str, url: str, platform: str, fail_count: int = 1, lang: str = "ar", user=None):
    """يعرض رسالة الخطأ مع زر إعادة المحاولة + زر الإبلاغ. يبلغ المطور تلقائياً بالخطأ الحقيقي."""
    text = db.get_message("download_error", lang)
    report_id = await _report_error_to_dev(context, "تحميل فيديو", user, platform, url, error)
    keyboard = InlineKeyboardMarkup(
        list(_retry_keyboard(url, platform, fail_count, lang).inline_keyboard)
        + list(_report_button(report_id, lang).inline_keyboard)
    )
    try:
        await msg.edit_text(text, reply_markup=keyboard)
        sent = msg
    except Exception:
        sent = await context.bot.send_message(chat_id, text, reply_markup=keyboard)
    await _schedule_auto_delete(context, chat_id, sent.message_id, "download_error")


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, retry_id = query.data.split(":", 1)
    except ValueError:
        return

    pending = RETRY_PENDING.pop(retry_id, None)
    if not pending:
        await query.edit_message_text(db.get_message("expired_request", _lang(query.from_user.id)))
        return

    url, platform, fail_count = pending
    user = query.from_user
    chat_id = query.message.chat_id

    try:
        await query.message.delete()
    except Exception:
        pass

    # نرسل رسالة جديدة نستخدمها كـ update.message لباقي دوال المعالجة (اللي تعتمد عليها)
    placeholder = await context.bot.send_message(chat_id, db.get_message("retrying", _lang(update.effective_user.id)))
    fake_update = SimpleNamespace(
        message=placeholder,
        effective_user=user,
        effective_chat=query.message.chat,
        callback_query=None,
    )
    await _process_single_link(fake_update, context, user, platform, url, fail_count)


def _fallback_download(platform: str, url: str):
    """ينفذ التحميل البديل لمنصة معينة (دالة متزامنة، تشتغل بـ asyncio.to_thread).
    يرجع (path, meta). ويشات: TikHub + فك التشفير. دويين/RedNote: TikHub مباشرة."""
    if platform == "wechat":
        return wechat.download_and_decrypt(url)
    return getattr(tikhub, FALLBACK_DOWNLOAD_FUNCS[platform])(url)


FALLBACK_DOWNLOAD_FUNCS = {
    "douyin": "download_douyin_via_api",
    "rednote": "download_rednote_via_api",
}

# تخزين مؤقت: confirm_id قصير -> (url, platform) لتأكيد استخدام المحاولة البديلة
FALLBACK_CONFIRM_PENDING: dict[str, tuple[str, str]] = {}


def _fallback_supported(platform: str) -> bool:
    return platform in FALLBACK_DOWNLOAD_FUNCS or platform == "wechat"


async def _show_fallback_prompt(context, chat_id: int, user, url: str, platform: str, edit_message=None):
    """يعرض رسالة المحاولة البديلة حسب وضع المستخدم (3 حالات):
       1) الدفع موقف          -> السلوك القديم (مجاني متبقي فقط، بدون شراء)
       2) عنده رصيد/مجاني     -> تأكيد مع تفصيل الرصيدين (المجاني + المدفوع)
       3) ما عنده شي          -> زر شراء
    الأدمن مجاني دائماً: يتخطى كل هذا ويروح مباشرة لتأكيد بسيط."""
    lang = _lang(user.id)
    pname = payments.pname(platform, lang)
    is_admin = _is_admin(user.id)

    avail = wallet.availability(user.id, platform)
    pay_on = wallet.payments_enabled(platform)

    def _cancel_btn(cid):
        return InlineKeyboardButton(db.get_message("btn_cancel", lang), callback_data=f"fbcancel:{cid}")

    confirm_id = uuid.uuid4().hex[:10]
    use_label = db.get_message("btn_use_fallback", lang)

    if is_admin:
        FALLBACK_CONFIRM_PENDING[confirm_id] = (url, platform)
        text = db.get_message("fallback_admin", lang)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(use_label, callback_data=f"fbconfirm:{confirm_id}")],
                                       [_cancel_btn(confirm_id)]])

    elif avail["can_use"]:
        FALLBACK_CONFIRM_PENDING[confirm_id] = (url, platform)
        text = db.get_message(
            "fallback_confirm", lang,
            platform=pname, free_left=avail["free_left"], paid_balance=avail["paid"],
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(use_label, callback_data=f"fbconfirm:{confirm_id}")],
                                       [_cancel_btn(confirm_id)]])

    elif pay_on:
        # ما عنده شي: نعرض زر شراء، ونحفظ الرابط حتى نكمل تلقائياً بعد الدفع
        payments.RESUME_AFTER_PURCHASE[user.id] = (url, platform)
        text = db.get_message("fallback_no_credit", lang, platform=pname)
        buy_label = db.get_message("btn_buy_platform", lang, platform=pname)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(buy_label, callback_data=f"buy:plat:{platform}")],
            [_cancel_btn(confirm_id)],
        ])

    else:
        # الدفع موقف: نفس السلوك القديم - "وصلت للحد" بدون خيار شراء
        text = db.get_message("fallback_limit_reached", lang, limit=wallet.get_weekly_free_limit(platform))
        markup = None

    try:
        if edit_message is not None:
            await _safe_markdown(edit_message.edit_text, text, reply_markup=markup)
        else:
            await _safe_markdown(lambda t, **k: context.bot.send_message(chat_id, t, **k), text, reply_markup=markup)
    except Exception:
        await context.bot.send_message(chat_id, text.replace("*", "").replace("`", ""), reply_markup=markup)


async def handle_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج زر 'محاولة بديلة' - يعرض التأكيد (او الشراء لو ما عنده رصيد)."""
    query = update.callback_query
    await query.answer()
    lang = _lang(query.from_user.id)

    try:
        _, retry_id = query.data.split(":", 1)
    except ValueError:
        return

    pending = RETRY_PENDING.pop(retry_id, None)
    if not pending:
        await query.edit_message_text(db.get_message("expired_request", lang))
        return

    url, platform, _fail_count = pending
    user = query.from_user

    if not _fallback_supported(platform):
        return

    if not db.get_setting(f"{platform}_fallback_enabled", True):
        await query.answer(db.get_message("fallback_disabled", lang), show_alert=True)
        return

    await _show_fallback_prompt(context, query.message.chat_id, user, url, platform, edit_message=query.message)


async def handle_fallback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج زر 'إلغاء' برسالة تأكيد المحاولة البديلة."""
    query = update.callback_query
    await query.answer()
    try:
        _, confirm_id = query.data.split(":", 1)
    except ValueError:
        return
    pending = FALLBACK_CONFIRM_PENDING.pop(confirm_id, None)
    if pending:
        payments.RESUME_AFTER_PURCHASE.pop(query.from_user.id, None)
    else:
        payments.RESUME_AFTER_PURCHASE.pop(query.from_user.id, None)
    try:
        await query.message.delete()
    except Exception:
        pass


async def _run_fallback_download(update, context, user, chat_id: int, url: str, platform: str):
    """ينفذ المحاولة البديلة فعلياً. الخصم يصير بعد نجاح الإرسال (مو قبل)."""
    lang = _lang(user.id)
    status = await context.bot.send_message(chat_id, db.get_message("fallback_retrying", lang))
    files = []
    try:
        # نتحقق مرة ثانية قبل التنفيذ (الرصيد ممكن تغير بين التأكيد والضغط)
        if not _is_admin(user.id) and not wallet.availability(user.id, platform)["can_use"]:
            await status.delete()
            await _show_fallback_prompt(context, chat_id, user, url, platform)
            return

        path, meta = await asyncio.to_thread(_fallback_download, platform, url)
        files = [path]

        if not _check_size_ok(files):
            await status.edit_text(db.get_message("file_too_large", lang, max_size=_max_file_size_mb()))
            return
        if not _size_limit_allows(user.id, _total_size(files)):
            await status.edit_text(_size_limit_message(lang))
            return

        await status.delete()

        sticker_msg = await _show_upload_sticker(context, chat_id, platform)
        try:
            for f in files:
                await _send_file(update, context, f, chat_id=chat_id)
        except Exception:
            await _resolve_upload_sticker_error(context, chat_id, platform, sticker_msg)
            raise
        await _resolve_upload_sticker_success(sticker_msg)

        # الخصم بعد نجاح الإرسال الفعلي فقط. الأدمن ما ينخصم منه شي.
        if not _is_admin(user.id):
            wallet.consume(user.id, platform, url)
            db.log_link(user.id, user.username or "", platform, url)
        _maybe_trigger_size_limit(user.id, _total_size(files))
        _record_download_success(platform)

        try:
            await _send_post_info(context, chat_id, user.id, meta, len(files))
        except Exception:
            logger.exception("failed to send post info after successful fallback upload (ignored)")

        await _maybe_low_balance_hint(context, chat_id, user, platform)
        try:
            await _check_tikhub_balance(context)
        except Exception:
            logger.exception("tikhub balance check failed (ignored)")
    except Exception as e:
        logger.exception(f"{platform} fallback download failed")
        text = db.get_message("fallback_failed", lang)
        report_id = await _report_error_to_dev(context, "المحاولة البديلة", user, platform, url, str(e))
        markup = _report_button(report_id, lang)
        try:
            await status.edit_text(text, reply_markup=markup)
            fail_msg = status
        except Exception:
            fail_msg = await context.bot.send_message(chat_id, text, reply_markup=markup)
        seconds = _get_limit_value("fallback_failure_autodelete_sec")
        if db.get_setting("fallback_failure_autodelete_enabled", False):
            await _schedule_auto_delete_seconds(context, chat_id, fail_msg.message_id, seconds)
    finally:
        downloader.cleanup(files)


_LAST_TIKHUB_ALERT = {"ts": 0.0}


async def _check_tikhub_balance(context):
    """ينبه الأدمن (بالقناة) لو رصيد TikHub نزل تحت الحد. مرة كل 6 ساعات كحد أقصى حتى ما يزعج."""
    import time
    if not tikhub.is_configured():
        return
    if time.time() - _LAST_TIKHUB_ALERT["ts"] < 6 * 3600:
        return
    usage = await asyncio.to_thread(tikhub.get_usage)
    if not usage:
        return
    threshold = _get_limit_value("low_tikhub_balance_usd")
    bal = float(usage.get("balance") or 0) + float(usage.get("free_credit") or 0)
    if bal < threshold:
        _LAST_TIKHUB_ALERT["ts"] = time.time()
        await payments.notify(
            context,
            f"⚠️ *رصيد TikHub منخفض*\nالرصيد: ${bal:.3f} (الحد: ${threshold})\n"
            "اشحن رصيدك حتى ما تتوقف المحاولة البديلة عن المستخدمين.",
            markdown=True, kind="tikhub",
        )


async def _maybe_low_balance_hint(context, chat_id: int, user, platform: str):
    """تنبيه لطيف للمستخدم لما يبقى له تحميل وحد او خلص رصيده (يشجع الشراء بدون إزعاج)."""
    if _is_admin(user.id) or not wallet.payments_enabled(platform):
        return
    a = wallet.availability(user.id, platform)
    lang = _lang(user.id)
    total = a["free_left"] + a["paid"]
    if total > 1:
        return
    pname = payments.pname(platform, lang)
    text = db.get_message("low_balance_one" if total == 1 else "low_balance_zero", lang, platform=pname)
    try:
        await context.bot.send_message(chat_id, text)
    except Exception:
        pass


async def handle_fallback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج زر 'استخدم المحاولة' - ينفذ التحميل الفعلي عبر TikHub API."""
    query = update.callback_query
    await query.answer()

    try:
        _, confirm_id = query.data.split(":", 1)
    except ValueError:
        return

    lang = _lang(query.from_user.id)
    pending = FALLBACK_CONFIRM_PENDING.pop(confirm_id, None)
    if not pending:
        await query.edit_message_text(db.get_message("expired_request", lang))
        return

    url, platform = pending
    user = query.from_user
    chat_id = query.message.chat_id

    try:
        await query.message.delete()
    except Exception:
        pass

    await _run_fallback_download(update, context, user, chat_id, url, platform)


async def _resume_after_purchase(update, context, url: str, platform: str):
    """يُستدعى تلقائياً بعد نجاح الدفع: يكمل تحميل الرابط اللي كان ينتظر بدون ما المستخدم يعيد إرساله."""
    user = update.effective_user
    await _run_fallback_download(update, context, user, update.effective_chat.id, url, platform)


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_awaiting_states(update.effective_user.id)
    await payments.show_shop(update, context)


async def paysupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر مطلوب من تليگرام لأي بوت يبيع بالنجوم."""
    lang = _lang(update.effective_user.id)
    text = db.get_message("paysupport", lang)
    await _safe_markdown(update.message.reply_text, text)


async def handle_cancel_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج زر 'إلغاء العملية' - يمسح الطلب المعلق ويحذف رسالة الخطأ."""
    query = update.callback_query
    await query.answer()

    try:
        _, retry_id = query.data.split(":", 1)
    except ValueError:
        return

    RETRY_PENDING.pop(retry_id, None)
    try:
        await query.message.delete()
    except Exception:
        pass


def _escape_md(text: str) -> str:
    """يهرب الأحرف الخاصة بـ Markdown العادي حتى نص المستخدم/المنصة ما يكسر التنسيق."""
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_info_caption(meta: dict, count: int, lang: str = "ar") -> str:
    unknown = db.get_message("post_unknown", lang)
    no_handle = db.get_message("post_no_handle", lang)
    no_desc = db.get_message("post_no_desc", lang)

    uploader = _escape_md(meta.get("uploader") or unknown)
    uploader_id = meta.get("uploader_id")
    handle = f"@{_escape_md(uploader_id)}" if uploader_id else no_handle
    description = meta.get("description") or no_desc
    if len(description) > 400:
        description = description[:400] + "..."
    description = _escape_md(description)

    if count > 1:
        count_line = db.get_message("post_count_line", lang, count=count)
    else:
        count_line = ""

    return db.get_message(
        "post_info_template",
        lang,
        uploader=uploader,
        handle=handle,
        description=description,
        count=count,
        count_line=count_line,
    ).strip()


async def _send_post_info(context, chat_id: int, user_id: int, meta: dict, count: int, reply_markup=None):
    """يبني ويرسل رسالة معلومات المنشور، مع رسالة خطأ منفصلة وحذف تلقائي اذا فشل التنسيق."""
    lang = _lang(user_id)
    if not _post_info_enabled(user_id):
        if reply_markup:
            # لسا لازم نرسل الأزرار (مثل زر الصوت) حتى لو المعلومات موقفة
            done_text = db.get_message("btn_done", lang)
            sent = await context.bot.send_message(chat_id, done_text, reply_markup=reply_markup)
        return
    try:
        caption = _build_info_caption(meta, count, lang)
        sent = await context.bot.send_message(
            chat_id, caption, parse_mode="Markdown", reply_markup=reply_markup
        )
    except Exception as e:
        logger.exception("failed to send post info caption")
        text = db.get_message("post_info_error", lang)
        report_id = await _report_error_to_dev(context, "معلومات المنشور", user_id, meta.get("webpage_url", ""), meta.get("webpage_url", ""), str(e))
        combined_markup = reply_markup
        report_kb = _report_button(report_id, lang)
        if reply_markup:
            combined_markup = InlineKeyboardMarkup(list(reply_markup.inline_keyboard) + list(report_kb.inline_keyboard))
        else:
            combined_markup = report_kb
        sent = await context.bot.send_message(chat_id, text, reply_markup=combined_markup)
        await _schedule_auto_delete(context, chat_id, sent.message_id, "post_info_error")


# ---------- لمت حجم الملفات (تقليل البندويث بعد تحميل ملف كبير) ----------

def _size_limit_settings():
    return {
        "trigger_mb": _get_limit_value("size_limit_trigger_mb"),
        "duration_hours": _get_limit_value("size_limit_duration_hours"),
        "small_mb": _get_limit_value("size_limit_small_mb"),
    }


def _size_limit_active(user_id: int) -> bool:
    """هل المستخدم بفترة لمت فعالة هسه؟ (حمّل ملف كبير خلال آخر X ساعة ولم تنتهِ المدة)."""
    if not db.get_setting("size_limit_enabled", True):
        return False
    if _is_admin(user_id) or db.is_size_limit_bypassed(user_id):
        return False
    # المشتركين المدفوعين (عندهم رصيد بأي منصة) يتخطون اللمت تلقائياً
    if any(wallet.get_balance(user_id, p) > 0 for p in wallet.PAID_PLATFORMS):
        return False
    hit_at = db.get_size_limit_hit(user_id)
    if hit_at is None:
        return False
    duration = _size_limit_settings()["duration_hours"]
    from datetime import datetime, timezone, timedelta
    if hit_at.tzinfo is None:
        hit_at = hit_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - hit_at >= timedelta(hours=duration):
        db.clear_size_limit_hit(user_id)
        return False
    return True


def _size_limit_allows(user_id: int, size_bytes: int) -> bool:
    """هل حجم هذا الملف مسموح بيه للمستخدم بوضعه الحالي (عادي، او تحت لمت)؟"""
    if not _size_limit_active(user_id):
        return True
    small_bytes = _size_limit_settings()["small_mb"] * 1024 * 1024
    return size_bytes <= small_bytes


def _maybe_trigger_size_limit(user_id: int, size_bytes: int):
    """يسجل بداية فترة اللمت لو الملف اللي انحمّل توه وصل عتبة التفعيل.
    يُستدعى بعد نجاح التحميل الفعلي (مو قبله)، ومستثنى منها الأدمن والمعفيين والمشتركين المدفوعين."""
    if not db.get_setting("size_limit_enabled", True):
        return
    if _is_admin(user_id) or db.is_size_limit_bypassed(user_id):
        return
    if any(wallet.get_balance(user_id, p) > 0 for p in wallet.PAID_PLATFORMS):
        return
    trigger_bytes = _size_limit_settings()["trigger_mb"] * 1024 * 1024
    if size_bytes >= trigger_bytes:
        db.set_size_limit_hit(user_id)


def _size_limit_message(lang: str) -> str:
    s = _size_limit_settings()
    return db.get_message(
        "size_limit_active", lang,
        big_mb=s["trigger_mb"], hours=s["duration_hours"], small_mb=s["small_mb"],
    )



def _check_size_ok(files: list[str]) -> bool:
    import os
    total = sum(os.path.getsize(f) for f in files if os.path.exists(f))
    return total <= _max_file_size_bytes()


def _total_size(files: list[str]) -> int:
    import os
    return sum(os.path.getsize(f) for f in files if os.path.exists(f))


async def _show_upload_sticker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, platform: str):
    """يرسل ستيكر 'جاري الرفع' الخاص بالمنصة اذا محدد، يرجع رسالة الستيكر (لحذفها لاحقاً) او None."""
    file_id = db.get_sticker(f"upload_{platform}")
    if not file_id:
        return None
    try:
        return await context.bot.send_sticker(chat_id, file_id)
    except Exception:
        logger.exception("failed to send upload sticker")
        return None


async def _resolve_upload_sticker_success(sticker_msg):
    if sticker_msg:
        try:
            await sticker_msg.delete()
        except Exception:
            pass


async def _resolve_upload_sticker_error(context: ContextTypes.DEFAULT_TYPE, chat_id: int, platform: str, sticker_msg):
    if sticker_msg:
        try:
            await sticker_msg.delete()
        except Exception:
            pass
    error_sticker = db.get_sticker(f"error_{platform}")
    if error_sticker:
        try:
            await context.bot.send_sticker(chat_id, error_sticker)
        except Exception:
            logger.exception("failed to send error sticker")


async def _handle_x(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, platform: str = "x", fail_count: int = 0):
    lang = _lang(update.effective_user.id)
    msg = await update.message.reply_text(db.get_message("fetching_qualities", lang))
    try:
        meta, quality_options, count = await downloader.list_qualities(url, platform)
    except Exception as e:
        logger.exception(f"{platform} quality fetch failed")
        await msg.edit_text(db.get_message("quality_fetch_error", lang, error=str(e)))
        return

    req_id = uuid.uuid4().hex[:10]
    PENDING[req_id] = (url, platform)
    # نحفظ الحجم المتوقع لكل دقة (استخرجناه أصلاً من yt-dlp هنا) حتى ما نحتاج نطلبه مرة ثانية
    # لما المستخدم يضغط الجودة - يفيد بالتحقق قبل التحميل لعرض الرابط المباشر (منصة X).
    PENDING_QUALITY_SIZES[req_id] = dict(quality_options)

    buttons = []
    best_label = db.get_message("best_quality", lang)
    size_unit = db.get_message("size_unit", lang)
    for h, size_bytes in quality_options:
        label = f"{h}p" if h else best_label
        if size_bytes:
            size_mb = size_bytes / (1024 * 1024)
            label += f" - {size_mb:.1f} {size_unit}"
        buttons.append([InlineKeyboardButton(
            label, callback_data=f"dl:{req_id}:{h}"
        )])
    audio_label = db.get_message("btn_audio_only", lang)
    buttons.append([InlineKeyboardButton(audio_label, callback_data=f"dl:{req_id}:audio")])

    if count > 1:
        extra = db.get_message("post_multi_extra", lang, count=count)
    else:
        extra = ""
    await msg.edit_text(
        db.get_message("choose_quality", lang) + extra,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_douyin(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    await _handle_auto_download(update, context, url, "douyin")


async def _handle_auto_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, platform: str, fail_count: int = 0):
    """معالج عام للمنصات اللي تنزل تلقائياً بأعلى جودة بدون قائمة اختيار (دويين، RedNote، Bilibili).
    fail_count: عدد الفشل المتتالي السابق لنفس الرابط - يحدد شنو الأزرار تطلع بحالة الفشل."""
    chat_id = update.effective_chat.id
    lang = _lang(update.effective_user.id)
    status_key = "downloading_douyin" if platform == "douyin" else "downloading"
    msg = await update.message.reply_text(db.get_message(status_key, lang))
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

    files = []
    took_heavy_slot = False
    try:
        files, meta = await downloader.download_video(url, platform, 0)
        if not files:
            no_files_msg = db.get_message("no_files", lang)
            await _send_error_with_retry(
                context, chat_id, msg, no_files_msg, url, platform, fail_count + 1, lang, update.effective_user
            )
            return

        if not _check_size_ok(files):
            await msg.edit_text(db.get_message("file_too_large", lang, max_size=_max_file_size_mb()))
            return
        if not _size_limit_allows(update.effective_user.id, _total_size(files)):
            await msg.edit_text(_size_limit_message(lang))
            return

        if _total_size(files) >= _heavy_threshold_bytes():
            await _acquire_heavy_slot(update, context, chat_id)
            took_heavy_slot = True

        await msg.delete()

        sticker_msg = await _show_upload_sticker(context, chat_id, platform)
        try:
            for path in files:
                await _send_file(update, context, path)
        except Exception:
            await _resolve_upload_sticker_error(context, chat_id, platform, sticker_msg)
            raise
        await _resolve_upload_sticker_success(sticker_msg)

        req_id = uuid.uuid4().hex[:10]
        PENDING[f"audio_{platform}_{req_id}"] = url
        audio_btn = InlineKeyboardMarkup([[InlineKeyboardButton(
            db.get_message("btn_audio", lang), callback_data=f"aud:{platform}:{req_id}"
        )]])
        _maybe_trigger_size_limit(update.effective_user.id, _total_size(files))
        _record_download_success(platform)
        try:
            await _send_post_info(context, chat_id, update.effective_user.id, meta, len(files), reply_markup=audio_btn)
        except Exception:
            logger.exception("failed to send post info after successful auto-download (ignored)")
    except Exception as e:
        logger.exception(f"{platform} download failed")
        await _record_download_failure(context, platform, str(e))
        await _send_error_with_retry(context, chat_id, msg, str(e), url, platform, fail_count + 1, lang, update.effective_user)
    finally:
        downloader.cleanup(files)
        if took_heavy_slot:
            _release_heavy_slot()


async def _offer_direct_link(query, lang: str, direct_url: str):
    """يعرض للمستخدم زر يفتح رابط الفيديو المباشر بمتصفحه، بدل تحميله على سيرفرنا ورفعه
    لتليگرام. يستخدم حالياً لمنصة X فقط لما الحجم يتجاوز حد الرابط المباشر (وليس بالضرورة
    حد تليگرام العام - الاثنين مستقلان وقد يختلف الرقم بينهما)."""
    text = db.get_message("direct_link_prompt", lang, max_size=_get_limit_value("direct_link_threshold_mb"))
    button = InlineKeyboardMarkup([[InlineKeyboardButton(
        db.get_message("btn_direct_download", lang), url=direct_url
    )]])
    await query.edit_message_text(text, reply_markup=button)



async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = _lang(query.from_user.id)

    try:
        _, req_id, choice = query.data.split(":", 2)
        is_audio = (choice == "audio")
        height = 0 if is_audio else int(choice)
    except ValueError:
        await query.edit_message_text(db.get_message("invalid_request", lang))
        return

    pending = PENDING.pop(req_id, None)
    quality_sizes = PENDING_QUALITY_SIZES.pop(req_id, {})
    if not pending:
        await query.edit_message_text(db.get_message("expired_request", lang))
        return
    url, platform = pending

    await query.edit_message_text(db.get_message("downloading", lang))
    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)

    chat_id = query.message.chat_id
    files = []
    took_heavy_slot = False
    try:
        # X فقط: نتحقق من حجم الفيديو المتوقع *قبل* التحميل (yt-dlp عادة تعرف الحجم من
        # قائمة الجودات أصلاً). لو الحجم معروف ومتوقع أكبر من حد تليگرام، نتخطى التحميل
        # والرفع بالكامل ونعرض رابط مباشر بدل هذا - يوفر بندويث سيرفرنا من الجهتين.
        if not is_audio and platform == "x" and db.get_setting("direct_link_enabled", True):
            expected_size = quality_sizes.get(height)
            threshold_bytes = _get_limit_value("direct_link_threshold_mb") * 1024 * 1024
            if expected_size and expected_size > threshold_bytes:
                direct_url = await downloader.get_direct_url(url, platform, height)
                if direct_url:
                    await _offer_direct_link(query, lang, direct_url)
                    return
                # ما لقينا رابط مباشر (نادر) - نكمل بالتحميل العادي كخطة احتياط

        if is_audio:
            files, meta = await downloader.download_audio(url, platform)
        else:
            files, meta = await downloader.download_video(url, platform, height)

        # X فقط: نتحقق من الحجم الفعلي بعد التحميل مقابل حد الرابط المباشر تحديداً
        # (مو حد تليگرام العام) - يغطي الحالة اللي yt-dlp ما وفرت حجم دقيق مسبقاً،
        # وهذا شائع جداً بفيديوهات X. هذا الفحص لازم يصير *قبل* _check_size_ok،
        # لأن الملف ممكن يكون أصغر من حد تليگرام (مقبول للرفع) لكن أكبر من حد
        # الرابط المباشر اللي حدده الأدمن - وبهالحالة لازم نعرض الرابط المباشر برضو.
        if not is_audio and platform == "x" and db.get_setting("direct_link_enabled", True):
            threshold_bytes = _get_limit_value("direct_link_threshold_mb") * 1024 * 1024
            if _total_size(files) > threshold_bytes:
                direct_url = await downloader.get_direct_url(url, platform, height)
                if direct_url:
                    await _offer_direct_link(query, lang, direct_url)
                    return
                # ما لقينا رابط مباشر (نادر) - نكمل بالفحص العادي تحت كخطة احتياط

        if not _check_size_ok(files):
            await query.edit_message_text(db.get_message("file_too_large", lang, max_size=_max_file_size_mb()))
            return
        if not _size_limit_allows(query.from_user.id, _total_size(files)):
            await query.edit_message_text(_size_limit_message(lang))
            return

        if _total_size(files) >= _heavy_threshold_bytes():
            await _acquire_heavy_slot(update, context, chat_id)
            took_heavy_slot = True

        await query.message.delete()

        sticker_msg = await _show_upload_sticker(context, chat_id, platform)
        display_name = meta.get("audio_display_name") if is_audio else None
        try:
            for path in files:
                await _send_file(update, context, path, chat_id=chat_id, display_name=display_name)
        except Exception:
            await _resolve_upload_sticker_error(context, chat_id, platform, sticker_msg)
            raise
        await _resolve_upload_sticker_success(sticker_msg)
        _maybe_trigger_size_limit(query.from_user.id, _total_size(files))
        _record_download_success(platform)

        try:
            await _send_post_info(context, chat_id, update.effective_user.id, meta, len(files))
        except Exception:
            logger.exception("failed to send post info after successful upload (ignored)")
    except Exception as e:
        logger.exception(f"{platform} download failed")
        await _record_download_failure(context, platform, str(e))
        await _send_error_with_retry(context, chat_id, query.message, str(e), url, platform, lang=lang, user=query.from_user)
    finally:
        downloader.cleanup(files)
        if took_heavy_slot:
            _release_heavy_slot()


async def _send_file(update, context, path: str, chat_id=None, display_name: str = None):
    chat_id = chat_id or update.effective_chat.id
    lower = path.lower()
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        with open(path, "rb") as f:
            await context.bot.send_photo(chat_id, f)
    elif lower.endswith((".mp3", ".m4a", ".ogg")):
        filename = f"{display_name}.mp3" if display_name else None
        with open(path, "rb") as f:
            await context.bot.send_audio(chat_id, f, filename=filename)
    else:
        with open(path, "rb") as f:
            await context.bot.send_video(chat_id, f, supports_streaming=True)


async def handle_audio_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج زر 'حمل الصوت بس' اللي يطلع بعد تحميل فيديو دويين."""
    query = update.callback_query
    await query.answer()
    lang = _lang(query.from_user.id)

    try:
        _, platform, req_id = query.data.split(":", 2)
    except ValueError:
        return

    url = PENDING.pop(f"audio_{platform}_{req_id}", None)
    if not url:
        await context.bot.send_message(query.message.chat_id, db.get_message("expired_request", lang))
        return

    chat_id = query.message.chat_id
    status = await context.bot.send_message(chat_id, db.get_message("downloading_audio", lang))

    files = []
    try:
        files, meta = await downloader.download_audio(url, platform)
        if not files or not _check_size_ok(files):
            await status.edit_text(db.get_message("file_too_large", lang, max_size=_max_file_size_mb()))
            return
        if not _size_limit_allows(query.from_user.id, _total_size(files)):
            await status.edit_text(_size_limit_message(lang))
            return
        await status.delete()
        display_name = meta.get("audio_display_name")
        for path in files:
            await _send_file(update, context, path, chat_id=chat_id, display_name=display_name)
        _maybe_trigger_size_limit(query.from_user.id, _total_size(files))
    except Exception as e:
        logger.exception("audio download failed")
        report_id = await _report_error_to_dev(context, "تحميل صوت (MP3)", update.effective_user, platform, url, str(e))
        await status.edit_text(db.get_message("download_error", lang), reply_markup=_report_button(report_id, lang))
    finally:
        downloader.cleanup(files)


def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .base_url(config.BASE_URL)
        .base_file_url(config.BASE_FILE_URL)
        .local_mode(False)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", my_stats))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("deeplink", deeplink_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("sizebypass", sizebypass_command))
    app.add_handler(CommandHandler("unsizebypass", unsizebypass_command))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("paysupport", paysupport_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(handle_audio_request, pattern=r"^aud:"))
    app.add_handler(CallbackQueryHandler(handle_pref_toggle, pattern=r"^pref:"))
    app.add_handler(CallbackQueryHandler(handle_language_choice, pattern=r"^setlang:"))
    app.add_handler(CallbackQueryHandler(handle_report_button, pattern=r"^report:"))
    app.add_handler(CallbackQueryHandler(handle_report_seen, pattern=r"^reportseen:"))
    app.add_handler(CallbackQueryHandler(payments.handle_buy_callback, pattern=r"^buy:"))
    app.add_handler(PreCheckoutQueryHandler(payments.handle_pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payments.handle_successful_payment))
    app.add_handler(CallbackQueryHandler(handle_retry, pattern=r"^retry:"))
    app.add_handler(CallbackQueryHandler(handle_fallback, pattern=r"^fallback:"))
    app.add_handler(CallbackQueryHandler(handle_fallback_confirm, pattern=r"^fbconfirm:"))
    app.add_handler(CallbackQueryHandler(handle_fallback_cancel, pattern=r"^fbcancel:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_op, pattern=r"^cancelop:"))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_admin_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    payments.set_resume_callback(_resume_after_purchase)
    return app
