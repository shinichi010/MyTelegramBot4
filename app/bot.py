import os
import json
import logging
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

from . import config, downloader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# تخزين مؤقت بالذاكرة: id قصير -> الرابط (لأن callback_data محدود بـ 64 بايت)
PENDING: dict[str, str] = {}

KNOWN_USERS_FILE = "/tmp/known_users.json"

WELCOME = (
    "هلا بيك 👋\n\n"
    "ارسلي رابط فيديو من *X (تويتر)* او *دويين* وراح أنزلّك المحتوى.\n\n"
    "▪️ روابط X: راح تطلع الك خيارات جودة تختار منها.\n"
    "▪️ روابط دويين: يتنزل تلقائياً بأعلى جودة متوفرة (فيديو او صور)."
)


def _load_known_users() -> set:
    try:
        with open(KNOWN_USERS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_known_users(ids: set):
    try:
        with open(KNOWN_USERS_FILE, "w") as f:
            json.dump(list(ids), f)
    except Exception:
        pass


async def _notify_admin_if_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config.ADMIN_CHAT_ID:
        return

    user = update.effective_user
    if not user:
        return

    known = _load_known_users()
    if user.id in known:
        return

    known.add(user.id)
    _save_known_users(known)

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
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _notify_admin_if_new(update, context)

    text = update.message.text or ""
    platform = downloader.detect_platform(text)

    if not platform:
        await update.message.reply_text(
            "بس روابط X (تويتر) او دويين مدعومة حالياً 🙏"
        )
        return

    url = downloader.extract_url(text, platform)

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


async def _handle_x(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("🔍 اجيب خيارات الجودة...")
    try:
        meta, heights, count = await downloader.list_x_qualities(url)
    except Exception as e:
        logger.exception("x quality fetch failed")
        await msg.edit_text(f"ما گدرت اجيب معلومات الرابط ❌\n{e}")
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
        f"اختار الجودة اللي تريدها 👇{extra}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_douyin(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("⬇️ جاري التحميل بأعلى جودة...")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)

    files = []
    try:
        files, meta = await downloader.download_douyin(url)
        if not files:
            await msg.edit_text("ما گدرت انزل هذا المنشور ❌")
            return

        await msg.delete()
        for path in files:
            await _send_file(update, context, path)

        await update.message.reply_text(
            _build_info_caption(meta, len(files)), parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("douyin download failed")
        await msg.edit_text(f"صار خطأ بالتحميل ❌\n{e}")
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
        await query.edit_message_text("انتهت صلاحية هذا الطلب، ارسل الرابط مرة اخرى 🔄")
        return

    await query.edit_message_text("⬇️ جاري التحميل...")
    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)

    files = []
    try:
        files, meta = await downloader.download_x(url, height)
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
        await query.edit_message_text(f"صار خطأ بالتحميل ❌\n{e}")
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
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
