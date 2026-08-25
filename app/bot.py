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

WELCOME = (
    "هلا بيك 👋\n\n"
    "ارسلي رابط فيديو من *X (تويتر)* او *دويين* وراح أنزلّك المحتوى.\n\n"
    "▪️ روابط X: راح تطلع الك خيارات جودة تختار منها.\n"
    "▪️ روابط دويين: يتنزل تلقائياً بأعلى جودة متوفرة (فيديو او صور)."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def _handle_x(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("🔍 اجيب خيارات الجودة...")
    try:
        info, options = await downloader.list_x_qualities(url)
    except Exception as e:
        logger.exception("x quality fetch failed")
        await msg.edit_text(f"ما گدرت اجيب معلومات الرابط ❌\n{e}")
        return

    if not options:
        await msg.edit_text("ما لكيت جودات فيديو بهذا الرابط ❌")
        return

    req_id = uuid.uuid4().hex[:10]
    PENDING[req_id] = url

    buttons = []
    for f in options:
        height = f.get("height")
        ext = f.get("ext", "mp4")
        size = f.get("filesize") or f.get("filesize_approx")
        size_txt = f" - {size / 1024 / 1024:.1f}MB" if size else ""
        label = f"{height}p ({ext}){size_txt}"
        buttons.append([InlineKeyboardButton(
            label, callback_data=f"dl:{req_id}:{f['format_id']}"
        )])

    await msg.edit_text(
        "اختار الجودة اللي تريدها 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_douyin(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("⬇️ جاري التحميل بأعلى جودة...")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)

    files = []
    try:
        files = await downloader.download_douyin(url)
        if not files:
            await msg.edit_text("ما گدرت انزل هذا المنشور ❌")
            return

        await msg.delete()
        for path in files:
            await _send_file(update, context, path)
    except Exception as e:
        logger.exception("douyin download failed")
        await msg.edit_text(f"صار خطأ بالتحميل ❌\n{e}")
    finally:
        downloader.cleanup(files)


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, req_id, format_id = query.data.split(":", 2)
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
        files = await downloader.download_x(url, format_id)
        await query.message.delete()
        for path in files:
            await _send_file(update, context, path, chat_id=query.message.chat_id)
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
