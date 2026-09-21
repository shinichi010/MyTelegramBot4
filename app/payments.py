"""
الدفع بنجوم تليگرام (XTR): عرض الباقات، الفواتير، pre_checkout، successful_payment، الاسترجاع.

الفكرة:
- المستخدم يضغط "🛒 اشتري تحميلات" ← يختار المنصة ← يختار الباقة ← نرسل فاتورة نجوم.
- payload الفاتورة = "buy:<platform>:<index>:<user_id>" (قصير وما يتحمل تلاعب لأننا نتحقق منه مرة ثانية).
- بعد الدفع نضيف الرصيد، وندز إشعار للقناة، وإذا كان المستخدم جاي من محاولة بديلة فاشلة
  نكمل التحميل تلقائياً بدون ما يعيد إرسال الرابط (RESUME_AFTER_PURCHASE).
"""
import logging
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ContextTypes

from . import config, db, wallet

logger = logging.getLogger("payments")

PLATFORM_NAMES = {
    "ar": {"douyin": "دويين", "rednote": "RedNote", "wechat": "ويشات"},
    "en": {"douyin": "Douyin", "rednote": "RedNote", "wechat": "WeChat"},
}

# user_id -> (url, platform): رابط ينتظر المستخدم يشتري رصيد حتى يكمل تحميله تلقائياً
RESUME_AFTER_PURCHASE: dict[int, tuple[str, str]] = {}

# يُحقن من bot.py حتى نتجنب استيراد دائري (دالة تنفذ المحاولة البديلة لرابط معين)
_resume_callback = None


def set_resume_callback(fn):
    global _resume_callback
    _resume_callback = fn


async def _send_md(send_fn, *args, **kwargs):
    """يرسل بـ Markdown، ولو الأدمن كسر الرموز بتعديل رسالة (نجمة غير مغلقة) يرسل نص عادي بدل ما ينهار."""
    from telegram.error import BadRequest
    try:
        return await send_fn(*args, parse_mode="Markdown", **kwargs)
    except BadRequest as e:
        if "parse entities" in str(e).lower():
            args = tuple(a.replace("*", "").replace("`", "") if isinstance(a, str) else a for a in args)
            return await send_fn(*args, **kwargs)
        raise


def pname(platform: str, lang: str) -> str:
    return PLATFORM_NAMES.get(lang, PLATFORM_NAMES["ar"]).get(platform, platform)


def _lang(user_id: int) -> str:
    return db.get_user_language(user_id) or "ar"


# ---------- عرض الباقات ----------

def platforms_menu(lang: str) -> InlineKeyboardMarkup:
    rows = []
    for p in wallet.PAID_PLATFORMS:
        if wallet.payments_enabled(p):
            rows.append([InlineKeyboardButton(pname(p, lang), callback_data=f"buy:plat:{p}")])
    rows.append([InlineKeyboardButton(db.get_message("btn_close", lang), callback_data="buy:close")])
    return InlineKeyboardMarkup(rows)


def packages_menu(platform: str, lang: str) -> InlineKeyboardMarkup:
    rows = []
    for i, pkg in enumerate(wallet.get_packages(platform)):
        label = db.get_message("btn_package", lang, credits=pkg["credits"], stars=pkg["stars"])
        rows.append([InlineKeyboardButton(label, callback_data=f"buy:pkg:{platform}:{i}")])
    rows.append([InlineKeyboardButton(db.get_message("btn_back", lang), callback_data="buy:menu")])
    return InlineKeyboardMarkup(rows)


def _balances_text(user_id: int, lang: str) -> str:
    lines = []
    for p in wallet.PAID_PLATFORMS:
        a = wallet.availability(user_id, p)
        lines.append(db.get_message("stats_balance_line", lang, platform=pname(p, lang),
                                    free=a["free_left"], paid=a["paid"]).strip("\n"))
    return "\n".join(lines)


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE, resume: tuple[str, str] | None = None):
    """يعرض قائمة الشراء. resume=(url, platform) لو المستخدم جاي من محاولة بديلة."""
    user = update.effective_user
    lang = _lang(user.id)
    chat_id = update.effective_chat.id

    if not wallet.payments_enabled():
        await context.bot.send_message(chat_id, db.get_message("shop_disabled", lang))
        return

    if resume:
        RESUME_AFTER_PURCHASE[user.id] = resume

    head = db.get_message("shop_title", lang)
    pick = db.get_message("shop_pick_platform", lang)
    text = f"{head}\n\n{_balances_text(user.id, lang)}\n\n{pick}"
    await _send_md(context.bot.send_message, chat_id, text, reply_markup=platforms_menu(lang))


# ---------- معالجة أزرار المتجر ----------

async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    lang = _lang(user.id)
    data = query.data
    await query.answer()

    if data == "buy:close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if not wallet.payments_enabled():
        await query.answer(db.get_message("shop_disabled", lang), show_alert=True)
        return

    if data == "buy:menu":
        head = db.get_message("shop_title", lang)
        pick = db.get_message("shop_pick_platform", lang)
        await _send_md(query.edit_message_text, f"{head}\n\n{_balances_text(user.id, lang)}\n\n{pick}",
                       reply_markup=platforms_menu(lang))
        return

    if data.startswith("buy:plat:"):
        platform = data.split(":", 2)[2]
        if platform not in wallet.PAID_PLATFORMS or not wallet.payments_enabled(platform):
            await query.answer("🚫", show_alert=True)
            return
        title = db.get_message("shop_platform_title", lang, platform=pname(platform, lang))
        pick = db.get_message("shop_pick_package", lang)
        await _send_md(query.edit_message_text, f"{title}\n\n{pick}", reply_markup=packages_menu(platform, lang))
        return

    if data.startswith("buy:pkg:"):
        _, _, platform, idx = data.split(":", 3)
        packages = wallet.get_packages(platform)
        try:
            pkg = packages[int(idx)]
        except (ValueError, IndexError):
            await query.answer("⚠️", show_alert=True)
            return
        if not wallet.payments_enabled(platform):
            await query.answer("🚫", show_alert=True)
            return

        name = pname(platform, lang)
        title = db.get_message("invoice_title", lang, credits=pkg["credits"], platform=name)
        desc = db.get_message("invoice_desc", lang, credits=pkg["credits"], platform=name)

        # payload قصير ≤128 بايت. نتحقق منه مرة ثانية عند الدفع.
        payload = f"buy:{platform}:{idx}:{user.id}:{uuid.uuid4().hex[:6]}"
        try:
            await context.bot.send_invoice(
                chat_id=query.message.chat_id,
                title=title, description=desc, payload=payload,
                provider_token="",            # فاضي = نجوم تليگرام
                currency="XTR",
                prices=[LabeledPrice(label=title, amount=int(pkg["stars"]))],
            )
        except Exception:
            logger.exception("failed to send invoice")
            await query.answer(db.get_message("shop_invoice_error", lang), show_alert=True)


# ---------- pre_checkout و successful_payment ----------

async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لازم نجاوب خلال 10 ثواني. نتحقق أن الباقة والسعر ما تغيروا وان الدفع لسه مفعّل."""
    q = update.pre_checkout_query
    lang = _lang(q.from_user.id)

    async def reject(key):
        await q.answer(ok=False, error_message=db.get_message(key, lang))

    try:
        parts = q.invoice_payload.split(":")
        if len(parts) < 4 or parts[0] != "buy":
            await reject("pay_invalid_invoice")
            return
        _, platform, idx, uid = parts[:4]
        if int(uid) != q.from_user.id or q.currency != "XTR":
            await reject("pay_invalid_invoice")
            return
        if platform not in wallet.PAID_PLATFORMS or not wallet.payments_enabled(platform):
            await reject("pay_platform_disabled")
            return
        pkg = wallet.get_packages(platform)[int(idx)]
        if int(pkg["stars"]) != q.total_amount:
            await reject("pay_price_changed")
            return
        if not db.is_connected():
            await reject("pay_unavailable")
            return
    except Exception:
        logger.exception("pre_checkout validation failed")
        await reject("pay_error")
        return

    await q.answer(ok=True)


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    pay = msg.successful_payment
    lang = _lang(user.id)

    try:
        _, platform, idx, uid = pay.invoice_payload.split(":")[:4]
        pkg = wallet.get_packages(platform)[int(idx)]
        credits = int(pkg["credits"])
    except Exception:
        # دفع تم لكن payload مش مفهوم: لازم نبلغ الأدمن حتى ما تضيع فلوس المستخدم
        logger.exception("successful_payment with bad payload: %s", pay.invoice_payload)
        await _notify(context, f"🚨 دفعة بـ payload غير مفهوم!\nالمستخدم: {user.id}\n"
                               f"النجوم: {pay.total_amount}\ncharge_id: `{pay.telegram_payment_charge_id}`")
        await msg.reply_text(db.get_message("pay_unknown_payload", lang))
        return

    ok, balance = wallet.add_purchase(
        user.id, platform, credits, pay.total_amount, pay.telegram_payment_charge_id
    )
    if not ok:
        # دفعة مكررة (تليگرام أعاد الإرسال): ما نضيف مرة ثانية
        return

    name = pname(platform, lang)
    text = db.get_message("pay_success", lang, credits=credits, platform=name, balance=balance)
    await msg.reply_text(text)

    uname = f"@{user.username}" if user.username else "—"
    await _notify(
        context,
        f"💰 *عملية شراء جديدة*\n"
        f"👤 {user.full_name} ({uname})\n🆔 `{user.id}`\n"
        f"📦 {credits} تحميل — {pname(platform, 'ar')}\n"
        f"⭐ {pay.total_amount} نجمة\n"
        f"🧾 `{pay.telegram_payment_charge_id}`",
        markdown=True,
    )

    # لو المستخدم جاي من محاولة بديلة فاشلة: نكمل التحميل تلقائياً
    resume = RESUME_AFTER_PURCHASE.pop(user.id, None)
    if resume and resume[1] == platform and _resume_callback:
        try:
            await _resume_callback(update, context, resume[0], platform)
        except Exception:
            logger.exception("auto-resume after purchase failed")


# ---------- الإشعارات (لكل نوع وجهته: قناة او خاص) ----------

# أنواع الإشعارات. كل نوع يتحدد له من /admin: "channel" او "private".
NOTIFY_KINDS = {
    "new_user": "🆕 مستخدم جديد",
    "purchase": "💰 عمليات الشراء والاسترجاع",
    "tikhub": "⚠️ رصيد TikHub المنخفض",
    "failure": "🚨 تنبيه فشل متتالي بمنصة",
}
# الافتراضي: القناة لو مضبوطة، وإلا الخاص (حتى ما تضيع إشعارات لو ما ضبطت القناة)
_DEFAULT_ROUTE = "channel"


def get_route(kind: str) -> str:
    """'channel' او 'private' لهذا النوع (مع رجوع للخاص لو القناة غير مضبوطة)."""
    route = db.get_setting(f"notify_route_{kind}", _DEFAULT_ROUTE)
    if route == "channel" and not config.NOTIFY_CHANNEL_ID:
        return "private"
    return "private" if route not in ("channel", "private") else route


def set_route(kind: str, route: str):
    if kind in NOTIFY_KINDS and route in ("channel", "private"):
        db.set_setting(f"notify_route_{kind}", route)


def target_for(kind: str):
    """الآيدي اللي تنرسل له إشعارات هذا النوع."""
    return config.NOTIFY_CHANNEL_ID if get_route(kind) == "channel" else config.ADMIN_CHAT_ID


def notify_target():
    """للتوافق: وجهة إشعارات الشراء."""
    return target_for("purchase")


async def notify(context, text: str, markdown: bool = False, kind: str = "purchase") -> bool:
    target = target_for(kind)
    if not target:
        return False
    try:
        await context.bot.send_message(target, text, parse_mode="Markdown" if markdown else None)
        return True
    except Exception:
        logger.exception("failed to send %s notification to %s", kind, target)
        return False


async def notify_photo(context, kind: str, photo, caption: str) -> bool:
    """يرسل صورة+كابشن (لإشعار المستخدم الجديد) للوجهة المختارة لهذا النوع."""
    target = target_for(kind)
    if not target:
        return False
    try:
        await context.bot.send_photo(target, photo, caption=caption)
        return True
    except Exception:
        logger.exception("failed to send %s photo notification to %s", kind, target)
        return False


async def _notify(context, text: str, markdown: bool = False) -> bool:
    return await notify(context, text, markdown, kind="purchase")


# ---------- الاسترجاع (أدمن) ----------

async def do_refund(context, charge_id: str) -> tuple[bool, str]:
    """يسترجع النجوم للمستخدم عبر تليگرام، ويسحب الرصيد الممنوح (بقدر ما ينسحب)."""
    tx = wallet.get_purchase(charge_id)
    if not tx:
        return False, "ما لقيت هذي العملية."
    if tx.get("refunded"):
        return False, "هذي العملية مسترجعة من قبل."
    try:
        await context.bot.refund_star_payment(user_id=tx["user_id"], telegram_payment_charge_id=charge_id)
    except Exception as e:
        logger.exception("refund_star_payment failed")
        return False, f"رفض تليگرام الاسترجاع: {e}"
    ok, clawed = wallet.mark_refunded(charge_id)
    return True, f"تم استرجاع {tx.get('stars', '?')} ⭐ وسُحب {clawed} تحميل من رصيده."
