import logging
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

from . import config, downloader, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# تخزين مؤقت بالذاكرة: id قصير -> الرابط (لأن callback_data محدود بـ 64 بايت)
PENDING: dict[str, str] = {}

# مفاتيح رسائل قابلة للتعديل، لعرضها بلوحة تحكم الأدمن مع أسماء مفهومة
EDITABLE_MESSAGES = {
    "welcome": "رسالة البداية (/start)",
    "unsupported_link": "رابط غير مدعوم",
    "fetching_qualities": "جلب خيارات الجودة (X)",
    "choose_quality": "اختيار الجودة (X)",
    "downloading": "جاري التحميل (X)",
    "downloading_douyin": "جاري التحميل (دويين)",
    "download_error": "خطأ بالتحميل",
    "quality_fetch_error": "خطأ بجلب الجودة",
    "expired_request": "انتهاء صلاحية الطلب",
    "platform_disabled": "منصة موقوفة",
    "user_banned": "مستخدم محظور",
    "file_too_large": "الملف كبير جداً",
    "queue_wait": "انتظار بالطابور",
}

# محادثة تعديل رسالة (أدمن فقط): user_id -> key الرسالة اللي ينتظر نصها الجديد
AWAITING_MESSAGE_EDIT: dict[int, str] = {}


def _is_admin(user_id: int) -> bool:
    return bool(config.ADMIN_CHAT_ID) and str(user_id) == str(config.ADMIN_CHAT_ID)


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
    await _notify_admin_if_new(update, context)
    await update.message.reply_text(db.get_message("welcome"), parse_mode="Markdown")


# ==================== لوحة تحكم الأدمن ====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return

    buttons = [
        [InlineKeyboardButton("✏️ تعديل الرسائل", callback_data="adm:msgs")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="adm:stats")],
        [InlineKeyboardButton("📄 سجل الروابط", callback_data="adm:links")],
        [InlineKeyboardButton("🚫 توقيف/تفعيل منصة", callback_data="adm:platforms")],
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
        await query.edit_message_text(
            f"📝 النص الحالي لـ *{EDITABLE_MESSAGES.get(key, key)}*:\n\n"
            f"`{current}`\n\n"
            "ارسل النص الجديد هسه كرسالة عادية (تكدر تستخدم `{error}` او `{max_size}` "
            "او `{position}` حسب نوع الرسالة، خلهم كما هم لو ما تعرف وين تنحط).",
            parse_mode="Markdown",
        )

    elif data == "adm:stats":
        stats = db.get_stats()
        text = (
            "📊 *إحصائيات البوت*\n\n"
            f"👥 عدد المستخدمين: {stats['users']}\n"
            f"🔗 عدد الروابط المسجلة: {stats['links']}\n"
            f"⛔ عدد المحظورين: {stats['banned']}"
        )
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
        for platform, label in (("x", "X (تويتر)"), ("douyin", "دويين")):
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
            [InlineKeyboardButton("📊 إحصائيات", callback_data="adm:stats")],
            [InlineKeyboardButton("📄 سجل الروابط", callback_data="adm:links")],
            [InlineKeyboardButton("🚫 توقيف/تفعيل منصة", callback_data="adm:platforms")],
            [InlineKeyboardButton("⛔ حظر مستخدم", callback_data="adm:ban_help")],
        ]
        await query.edit_message_text("🛠️ لوحة تحكم الأدمن", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_callback_refresh_platforms(query):
    buttons = []
    for platform, label in (("x", "X (تويتر)"), ("douyin", "دويين")):
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


# ==================== الرسائل العادية ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # اذا الأدمن ينتظر منه نص تعديل رسالة
    if _is_admin(user.id) and user.id in AWAITING_MESSAGE_EDIT:
        key = AWAITING_MESSAGE_EDIT.pop(user.id)
        new_text = update.message.text
        db.set_message(key, new_text)
        await update.message.reply_text(f"✅ تحدثت رسالة: {EDITABLE_MESSAGES.get(key, key)}")
        return

    await _notify_admin_if_new(update, context)

    if db.is_banned(user.id):
        await update.message.reply_text(db.get_message("user_banned"))
        return

    text = update.message.text or ""
    platform = downloader.detect_platform(text)

    if not platform:
        await update.message.reply_text(db.get_message("unsupported_link"))
        return

    if db.is_platform_disabled(platform):
        await update.message.reply_text(db.get_message("platform_disabled"))
        return

    url = downloader.extract_url(text, platform)

    # تسجيل الرابط بالسجل، إلا إذا كان مرسل من الأدمن نفسه
    if not _is_admin(user.id):
        db.log_link(user.id, user.username or "", platform, url)

    if platform == "douyin":
        await _handle_douyin(update, context, url)
    else:
        await _handle_x(update, context, url)


def _build_info_caption(meta: dict, count: int) -> str:
    uploader = meta.get("uploader") or "غير معروف"
    uploader_id = meta.get("uploader_id")
    handle = f"@{uploader_id}" if uploader_id else ""
    description = meta.get("description") or "بدون وصف"
    if len(description) > 400:
        description = description[:400] + "..."

    lines = ["ℹ️ *معلومات المنشور*", f"👤 الاسم: {uploader}"]
    if handle:
        lines.append(f"🔗 اليوزر: {handle}")
    lines.append(f"📝 الوصف: {description}")
    if count > 1:
        lines.append(f"🎞️ عدد المقاطع/الصور: {count}")
    return "\n".join(lines)


def _check_size_ok(files: list[str]) -> bool:
    import os
    total = sum(os.path.getsize(f) for f in files if os.path.exists(f))
    return total <= config.MAX_FILE_SIZE_BYTES


async def _handle_x(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text(db.get_message("fetching_qualities"))
    try:
        meta, heights, count = await downloader.list_x_qualities(url)
    except Exception as e:
        logger.exception("x quality fetch failed")
        await msg.edit_text(db.get_message("quality_fetch_error", error=str(e)))
        return

    req_id = uuid.uuid4().hex[:10]
    PENDING[req_id] = url

    buttons = []
    for h in heights:
        label = f"{h}p" if h else "أفضل جودة متوفرة"
        buttons.append([InlineKeyboardButton(
            label, callback_data=f"dl:{req_id}:{h}"
        )])

    extra = f" (المنشور فيه {count} مقاطع/صور، راح تنزل كلهن)" if count > 1 else ""
    await msg.edit_text(
        db.get_message("choose_quality") + extra,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_douyin(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text(db.get_message("downloading_douyin"))
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)

    files = []
    try:
        files, meta = await downloader.download_douyin(url)
        if not files:
            await msg.edit_text(db.get_message("download_error", error="ما گدرت انزل هذا المنشور"))
            return

        if not _check_size_ok(files):
            await msg.edit_text(db.get_message("file_too_large", max_size=config.MAX_FILE_SIZE_MB))
            return

        await msg.delete()
        for path in files:
            await _send_file(update, context, path)

        await update.message.reply_text(
            _build_info_caption(meta, len(files)), parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("douyin download failed")
        await msg.edit_text(db.get_message("download_error", error=str(e)))
    finally:
        downloader.cleanup(files)


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, req_id, height_str = query.data.split(":", 2)
        height = int(height_str)
    except ValueError:
        await query.edit_message_text("طلب غير صالح ❌")
        return

    url = PENDING.pop(req_id, None)
    if not url:
        await query.edit_message_text(db.get_message("expired_request"))
        return

    await query.edit_message_text(db.get_message("downloading"))
    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)

    files = []
    try:
        files, meta = await downloader.download_x(url, height)

        if not _check_size_ok(files):
            await query.edit_message_text(db.get_message("file_too_large", max_size=config.MAX_FILE_SIZE_MB))
            return

        await query.message.delete()
        for path in files:
            await _send_file(update, context, path, chat_id=query.message.chat_id)

        await context.bot.send_message(
            query.message.chat_id,
            _build_info_caption(meta, len(files)),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("x download failed")
        await query.edit_message_text(db.get_message("download_error", error=str(e)))
    finally:
        downloader.cleanup(files)


async def _send_file(update, context, path: str, chat_id=None):
    chat_id = chat_id or update.effective_chat.id
    lower = path.lower()
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        with open(path, "rb") as f:
            await context.bot.send_photo(chat_id, f)
    else:
        with open(path, "rb") as f:
            await context.bot.send_video(chat_id, f, supports_streaming=True)


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
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
