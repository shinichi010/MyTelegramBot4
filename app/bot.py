import logging
import uuid
from types import SimpleNamespace

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

from . import config, downloader, db, tikhub, wechat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# تخزين مؤقت بالذاكرة: id قصير -> الرابط (لأن callback_data محدود بـ 64 بايت)
PENDING: dict[str, str] = {}

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
    wait_msg = await context.bot.send_message(
        chat_id, db.get_message("queue_wait", position=position)
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
    "welcome": "رسالة البداية (/start)",
    "unsupported_link": "رابط غير مدعوم",
    "fetching_qualities": "جلب خيارات الجودة (X)",
    "choose_quality": "اختيار الجودة (X)",
    "downloading": "جاري التحميل (X)",
    "downloading_douyin": "جاري التحميل (دويين)",
    "download_error": "خطأ بالتحميل",
    "post_info_error": "خطأ بمعلومات المنشور",
    "post_info_template": "قالب معلومات المنشور",
    "quality_fetch_error": "خطأ بجلب الجودة",
    "expired_request": "انتهاء صلاحية الطلب",
    "platform_disabled": "منصة موقوفة",
    "user_banned": "مستخدم محظور",
    "maintenance_mode": "وضع الصيانة",
    "file_too_large": "الملف كبير جداً",
    "queue_wait": "انتظار بالطابور",
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
}

# محادثة تعديل رسالة (أدمن فقط): user_id -> key الرسالة اللي ينتظر نصها الجديد
AWAITING_MESSAGE_EDIT: dict[int, str] = {}

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
}
LIMIT_UNITS = {
    "max_file_size_mb": "ميكا",
    "heavy_file_threshold_mb": "ميكا",
    "main_ping_interval_min": "دقايق",
    "wechat_ping_interval_min": "دقايق",
    "download_error_autodelete_min": "دقايق",
    "post_info_error_autodelete_min": "دقايق",
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
    }
    return int(db.get_setting(key, defaults.get(key, 0)))


FAILURE_ALERT_THRESHOLD = 5  # كم فشل متتالي لنفس المنصة قبل ما ننبه الأدمن


async def _record_download_failure(context: ContextTypes.DEFAULT_TYPE, platform: str, error: str):
    count = db.record_failure(platform)
    if count == FAILURE_ALERT_THRESHOLD and config.ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                config.ADMIN_CHAT_ID,
                f"⚠️ تنبيه: صار {count} حالات فشل متتالية بمنصة *{platform}*.\n"
                f"آخر خطأ: {error[:300]}\n\n"
                "ممكن الروابط تحتاج تحديث كوكيز، او فيه مشكلة بالمنصة نفسها.",
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("failed to send failure alert to admin")


def _record_download_success(platform: str):
    db.reset_failures(platform)


async def _notify_admin_if_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config.ADMIN_CHAT_ID:
        return

    user = update.effective_user
    if not user:
        return

    is_new = db.upsert_user(user.id, user.username or "", user.full_name or "")
    if not is_new:
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
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            await context.bot.send_photo(config.ADMIN_CHAT_ID, file_id, caption=caption)
        else:
            await context.bot.send_message(config.ADMIN_CHAT_ID, caption)
    except Exception:
        logger.exception("failed to notify admin about new user")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_awaiting_states(update.effective_user.id)
    await _notify_admin_if_new(update, context)

    # دعم Deep Link: t.me/البوت?start=رابط_مشفر_base64 يبدأ التحميل تلقائياً
    if context.args:
        url = _decode_deep_link(context.args[0])
        if url:
            platform = downloader.detect_platform(url) or ("wechat" if wechat.detect(url) else None)
            if platform:
                user = update.effective_user
                await _process_single_link(update, context, user, platform, url)
                return
        await update.message.reply_text("رابط الـ Deep Link غير صالح، جرب ترسل الرابط مباشرة ❌")
        return

    await update.message.reply_text(db.get_message("welcome"), parse_mode="Markdown")


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
    text = (
        "📖 *الأوامر المتوفرة*\n\n"
        "/start — رسالة الترحيب وشرح المنصات المدعومة\n"
        "/stats — إحصائياتك الشخصية + إعدادات (معلومات المنشور، التحقق من الرابط)\n"
        "/help — هذي الرسالة\n\n"
        "📎 *شلون تستخدم البوت*\n"
        "بس ارسل رابط من X، دويين، ويشات، RedNote، او Bilibili — تقدر ترسل عدة "
        "روابط بنفس الرسالة وراح انزلهن وحدة وحدة بالترتيب.\n\n"
        "▪️ روابط X، RedNote، وBilibili: تطلع الك خيارات جودة مع الحجم تختار منها.\n"
        "▪️ روابط دويين وويشات: تتنزل تلقائياً بأعلى جودة متوفرة.\n"
        "▪️ اي فيديو تكدر تحمل الصوت بس منه (MP3) بزر منفصل."
    )
    if _is_admin(update.effective_user.id):
        text += (
            "\n\n🛠️ انت أدمن - استخدم /admin لفتح لوحة التحكم.\n"
            "لو صارت عالق بمنتصف تعديل رسالة/ستيكر/حد رقمي وتريد تلغيه، استخدم /cancel."
        )
    await update.message.reply_text(text, parse_mode="Markdown")


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
        AWAITING_MESSAGE_EDIT.pop(user_id, None) is not None
        or AWAITING_STICKER_EDIT.pop(user_id, None) is not None
        or AWAITING_LIMIT_EDIT.pop(user_id, None) is not None
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يلغي اي محادثة تعديل معلقة (رسالة/ستيكر/حد رقمي) عالقة بانتظار نص من الأدمن."""
    was_waiting = _clear_awaiting_states(update.effective_user.id)
    if was_waiting:
        await update.message.reply_text("✅ تم إلغاء العملية المعلقة.")
    else:
        await update.message.reply_text("ماكو عملية معلقة حالياً.")


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إحصائيات شخصية للمستخدم نفسه - كم رابط حمّل ومن اي منصة + إعداداته الشخصية."""
    user = update.effective_user
    _clear_awaiting_states(user.id)
    text, markup = _build_stats_view(user.id)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


def _build_stats_view(user_id: int):
    info = db.get_user_info(user_id)
    stats = db.get_user_link_stats(user_id)

    platform_names = {"x": "X (تويتر)", "douyin": "دويين", "wechat": "ويشات"}
    lines = ["📊 *إحصائياتك بالبوت*\n"]

    if info and info.get("joined_at"):
        lines.append(f"📅 عضو منذ: {info['joined_at'].strftime('%Y-%m-%d')}")

    lines.append(f"🔗 مجموع التحميلات: {stats['total']}")
    for platform, count in stats["by_platform"].items():
        name = platform_names.get(platform, platform)
        lines.append(f"  • {name}: {count}")

    if stats["total"] == 0:
        lines.append("\nما عندك تحميلات مسجلة لحد هسه 📭")

    text = "\n".join(lines)

    post_info_state = "🟢 مفعّلة" if _post_info_enabled(user_id) else "🔴 موقفة"
    verify_state = "🟢 مفعّل" if _verify_link_enabled(user_id) else "🔴 موقف"
    preview_state = "🟢 مفعّلة" if _preview_enabled(user_id) else "🔴 موقفة"

    buttons = [
        [InlineKeyboardButton(f"ℹ️ معلومات المنشور: {post_info_state}", callback_data="pref:toggle_post_info")],
        [InlineKeyboardButton(f"🔎 التحقق من الرابط: {verify_state}", callback_data="pref:toggle_verify_link")],
        [InlineKeyboardButton(f"👁️ معاينة سريعة قبل التحميل: {preview_state}", callback_data="pref:toggle_preview")],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def handle_pref_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if query.data == "pref:toggle_post_info":
        if not db.get_setting("post_info_global_enabled", True):
            await query.answer("معلومات المنشور موقفة عام من الأدمن حالياً 🚫", show_alert=True)
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
            await query.answer("المعاينة السريعة موقفة عام من الأدمن حالياً 🚫", show_alert=True)
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

    if data == "adm:msgs":
        buttons = [
            [InlineKeyboardButton(label, callback_data=f"adm:msg:{key}")]
            for key, label in EDITABLE_MESSAGES.items()
        ]
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")])
        await query.edit_message_text(
            "اختار الرسالة اللي تريد تعدلها 👇", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("adm:msg:"):
        key = data.split(":", 2)[2]
        current = db.get_message(key)
        AWAITING_MESSAGE_EDIT[query.from_user.id] = key
        var_hint = MESSAGE_VARIABLE_HINTS.get(
            key,
            "تكدر تستخدم `{error}` او `{max_size}` او `{position}` حسب نوع الرسالة، "
            "خلهم كما هم لو ما تعرف وين تنحط."
        )
        await query.edit_message_text(
            f"📝 النص الحالي لـ *{EDITABLE_MESSAGES.get(key, key)}*:\n\n"
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
            [InlineKeyboardButton("⬅️ رجوع", callback_data="adm:back")],
        ]
        await query.edit_message_text(
            "اضغط على الحد اللي تريد تغيره 👇", reply_markup=InlineKeyboardMarkup(buttons)
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

    # اذا الأدمن ينتظر منه نص تعديل رسالة
    elif _is_admin(user.id) and user.id in AWAITING_MESSAGE_EDIT:
        key = AWAITING_MESSAGE_EDIT.pop(user.id)
        new_text = update.message.text
        db.set_message(key, new_text)
        await update.message.reply_text(f"✅ تحدثت رسالة: {EDITABLE_MESSAGES.get(key, key)}")
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
        await update.message.reply_text(db.get_message("user_banned"))
        return

    if db.get_setting("maintenance_mode", False) and not _is_admin(user.id):
        await update.message.reply_text(db.get_message("maintenance_mode"))
        return

    text = update.message.text or ""
    is_group = update.effective_chat.type in ("group", "supergroup")

    # نستخرج كل الروابط المدعومة الموجودة بالرسالة (كل سطر/رابط منفصل)
    links = _extract_all_links(text)

    if not links:
        # بالمجاميع/القنوات نتجاهل الرسائل العادية بصمت حتى ما نزعج المحادثة
        if not is_group:
            await update.message.reply_text(db.get_message("unsupported_link"))
        return

    if is_group and db.get_setting("groups_enabled", True) is False:
        return

    if len(links) > 1:
        await update.message.reply_text(
            f"📋 لقيت {len(links)} روابط بالرسالة، راح انزلهن وحدة وحدة بالترتيب."
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


async def _process_single_link(update, context, user, platform: str, url: str):
    if platform == "wechat":
        if db.is_platform_disabled("wechat"):
            await update.message.reply_text(db.get_message("platform_disabled"))
            return
        if not wechat.is_configured():
            await update.message.reply_text("تحميل ويشات مو مفعّل حالياً 🙏")
            return
        if not _is_admin(user.id):
            db.log_link(user.id, user.username or "", "wechat", url)
        await _handle_wechat(update, context, url)
        return

    if db.is_platform_disabled(platform):
        await update.message.reply_text(db.get_message("platform_disabled"))
        return

    if not _is_admin(user.id):
        db.log_link(user.id, user.username or "", platform, url)

    if _verify_link_enabled(user.id):
        check_msg = await update.message.reply_text("🔎 جاري التحقق من الرابط...")
        ok = await downloader.verify_link(url, platform)
        await check_msg.delete()
        if not ok:
            await update.message.reply_text(f"هذا الرابط ما يشتغل او غير متاح ❌\n{url}")
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
        await _handle_x(update, context, url, platform)
    else:
        await _handle_auto_download(update, context, url, platform)


# تخزين مؤقت: retry_id قصير -> (url, platform) لزر "أعد المحاولة"
RETRY_PENDING: dict[str, tuple[str, str]] = {}


def _retry_keyboard(url: str, platform: str) -> InlineKeyboardMarkup:
    retry_id = uuid.uuid4().hex[:10]
    RETRY_PENDING[retry_id] = (url, platform)
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 أعد المحاولة", callback_data=f"retry:{retry_id}")]])


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


async def _send_error_with_retry(context, chat_id: int, msg, error: str, url: str, platform: str):
    """يعرض رسالة الخطأ مع زر إعادة المحاولة. يحاول يعدل رسالة موجودة، وإلا يرسل وحدة جديدة."""
    text = db.get_message("download_error", error=error)
    keyboard = _retry_keyboard(url, platform)
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
        await query.edit_message_text(db.get_message("expired_request"))
        return

    url, platform = pending
    user = query.from_user
    chat_id = query.message.chat_id

    try:
        await query.message.delete()
    except Exception:
        pass

    # نرسل رسالة جديدة نستخدمها كـ update.message لباقي دوال المعالجة (اللي تعتمد عليها)
    placeholder = await context.bot.send_message(chat_id, "🔄 جاري إعادة المحاولة...")
    fake_update = SimpleNamespace(
        message=placeholder,
        effective_user=user,
        effective_chat=query.message.chat,
        callback_query=None,
    )
    await _process_single_link(fake_update, context, user, platform, url)


def _escape_md(text: str) -> str:
    """يهرب الأحرف الخاصة بـ Markdown العادي حتى نص المستخدم/المنصة ما يكسر التنسيق."""
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_info_caption(meta: dict, count: int) -> str:
    uploader = _escape_md(meta.get("uploader") or "غير معروف")
    uploader_id = meta.get("uploader_id")
    handle = f"@{_escape_md(uploader_id)}" if uploader_id else "بدون يوزر"
    description = meta.get("description") or "بدون وصف"
    if len(description) > 400:
        description = description[:400] + "..."
    description = _escape_md(description)
    count_line = f"🎞️ عدد المقاطع/الصور: {count}" if count > 1 else ""

    return db.get_message(
        "post_info_template",
        uploader=uploader,
        handle=handle,
        description=description,
        count=count,
        count_line=count_line,
    ).strip()


async def _send_post_info(context, chat_id: int, user_id: int, meta: dict, count: int, reply_markup=None):
    """يبني ويرسل رسالة معلومات المنشور، مع رسالة خطأ منفصلة وحذف تلقائي اذا فشل التنسيق."""
    if not _post_info_enabled(user_id):
        if reply_markup:
            # لسا لازم نرسل الأزرار (مثل زر الصوت) حتى لو المعلومات موقفة
            sent = await context.bot.send_message(chat_id, "✅ تم", reply_markup=reply_markup)
        return
    try:
        caption = _build_info_caption(meta, count)
        sent = await context.bot.send_message(
            chat_id, caption, parse_mode="Markdown", reply_markup=reply_markup
        )
    except Exception as e:
        logger.exception("failed to send post info caption")
        text = db.get_message("post_info_error", error=str(e))
        sent = await context.bot.send_message(chat_id, text, reply_markup=reply_markup)
        await _schedule_auto_delete(context, chat_id, sent.message_id, "post_info_error")


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


async def _handle_x(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, platform: str = "x"):
    msg = await update.message.reply_text(db.get_message("fetching_qualities"))
    try:
        meta, quality_options, count = await downloader.list_qualities(url, platform)
    except Exception as e:
        logger.exception(f"{platform} quality fetch failed")
        await msg.edit_text(db.get_message("quality_fetch_error", error=str(e)))
        return

    req_id = uuid.uuid4().hex[:10]
    PENDING[req_id] = (url, platform)

    buttons = []
    for h, size_bytes in quality_options:
        label = f"{h}p" if h else "أفضل جودة متوفرة"
        if size_bytes:
            size_mb = size_bytes / (1024 * 1024)
            label += f" - {size_mb:.1f} ميكا"
        buttons.append([InlineKeyboardButton(
            label, callback_data=f"dl:{req_id}:{h}"
        )])
    buttons.append([InlineKeyboardButton("🎵 صوت فقط (MP3)", callback_data=f"dl:{req_id}:audio")])

    extra = f" (المنشور فيه {count} مقاطع/صور، راح تنزل كلهن)" if count > 1 else ""
    await msg.edit_text(
        db.get_message("choose_quality") + extra,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_douyin(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    await _handle_auto_download(update, context, url, "douyin")


async def _handle_auto_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, platform: str):
    """معالج عام للمنصات اللي تنزل تلقائياً بأعلى جودة بدون قائمة اختيار (دويين، RedNote، Bilibili)."""
    chat_id = update.effective_chat.id
    status_key = "downloading_douyin" if platform == "douyin" else "downloading"
    msg = await update.message.reply_text(db.get_message(status_key))
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

    files = []
    took_heavy_slot = False
    try:
        files, meta = await downloader.download_video(url, platform, 0)
        if not files:
            await _send_error_with_retry(context, chat_id, msg, "ما گدرت انزل هذا المنشور", url, platform)
            return

        if not _check_size_ok(files):
            await msg.edit_text(db.get_message("file_too_large", max_size=_max_file_size_mb()))
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
            "🎵 حمل الصوت بس (MP3)", callback_data=f"aud:{platform}:{req_id}"
        )]])
        await _send_post_info(context, chat_id, update.effective_user.id, meta, len(files), reply_markup=audio_btn)
        _record_download_success(platform)
    except Exception as e:
        logger.exception(f"{platform} download failed")
        await _record_download_failure(context, platform, str(e))
        await _send_error_with_retry(context, chat_id, msg, str(e), url, platform)
    finally:
        downloader.cleanup(files)
        if took_heavy_slot:
            _release_heavy_slot()


async def _handle_wechat(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("⬇️ جاري التحميل من ويشات...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

    files = []
    took_heavy_slot = False
    try:
        path, meta = await asyncio.to_thread(wechat.download_and_decrypt, url)
        files = [path]

        if not _check_size_ok(files):
            await msg.edit_text(db.get_message("file_too_large", max_size=_max_file_size_mb()))
            return

        if _total_size(files) >= _heavy_threshold_bytes():
            await _acquire_heavy_slot(update, context, chat_id)
            took_heavy_slot = True

        await msg.delete()

        sticker_msg = await _show_upload_sticker(context, chat_id, "wechat")
        try:
            for path in files:
                await _send_file(update, context, path)
        except Exception:
            await _resolve_upload_sticker_error(context, chat_id, "wechat", sticker_msg)
            raise
        await _resolve_upload_sticker_success(sticker_msg)

        await _send_post_info(context, chat_id, update.effective_user.id, meta, len(files))
        _record_download_success("wechat")
    except Exception as e:
        logger.exception("wechat download failed")
        await _record_download_failure(context, "wechat", str(e))
        await _send_error_with_retry(context, chat_id, msg, str(e), url, "wechat")
    finally:
        downloader.cleanup(files)
        if took_heavy_slot:
            _release_heavy_slot()


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, req_id, choice = query.data.split(":", 2)
        is_audio = (choice == "audio")
        height = 0 if is_audio else int(choice)
    except ValueError:
        await query.edit_message_text("طلب غير صالح ❌")
        return

    pending = PENDING.pop(req_id, None)
    if not pending:
        await query.edit_message_text(db.get_message("expired_request"))
        return
    url, platform = pending

    await query.edit_message_text(db.get_message("downloading"))
    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)

    chat_id = query.message.chat_id
    files = []
    took_heavy_slot = False
    try:
        if is_audio:
            files, meta = await downloader.download_audio(url, platform)
        else:
            files, meta = await downloader.download_video(url, platform, height)

        if not _check_size_ok(files):
            await query.edit_message_text(db.get_message("file_too_large", max_size=_max_file_size_mb()))
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

        await _send_post_info(context, chat_id, update.effective_user.id, meta, len(files))
        _record_download_success(platform)
    except Exception as e:
        logger.exception(f"{platform} download failed")
        await _record_download_failure(context, platform, str(e))
        await _send_error_with_retry(context, chat_id, query.message, str(e), url, platform)
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

    try:
        _, platform, req_id = query.data.split(":", 2)
    except ValueError:
        return

    url = PENDING.pop(f"audio_{platform}_{req_id}", None)
    if not url:
        await context.bot.send_message(query.message.chat_id, db.get_message("expired_request"))
        return

    chat_id = query.message.chat_id
    status = await context.bot.send_message(chat_id, "🎵 جاري تحميل الصوت...")

    files = []
    try:
        files, meta = await downloader.download_audio(url, platform)
        if not files or not _check_size_ok(files):
            await status.edit_text(db.get_message("file_too_large", max_size=_max_file_size_mb()))
            return
        await status.delete()
        display_name = meta.get("audio_display_name")
        for path in files:
            await _send_file(update, context, path, chat_id=chat_id, display_name=display_name)
    except Exception as e:
        logger.exception("audio download failed")
        await status.edit_text(db.get_message("download_error", error=str(e)))
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
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(handle_audio_request, pattern=r"^aud:"))
    app.add_handler(CallbackQueryHandler(handle_pref_toggle, pattern=r"^pref:"))
    app.add_handler(CallbackQueryHandler(handle_retry, pattern=r"^retry:"))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_admin_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
