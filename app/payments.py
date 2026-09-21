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
    rows.append([InlineKeyboardButton("❌ إغلاق" if lang == "ar" else "❌ Close", callback_data="buy:close")])
    return InlineKeyboardMarkup(rows)


def packages_menu(platform: str, lang: str) -> InlineKeyboardMarkup:
    rows = []
    for i, pkg in enumerate(wallet.get_packages(platform)):
        if lang == "ar":
            label = f"{pkg['credits']} تحميل — {pkg['stars']} ⭐"
        else:
            label = f"{pkg['credits']} downloads — {pkg['stars']} ⭐"
        rows.append([InlineKeyboardButton(label, callback_data=f"buy:pkg:{platform}:{i}")])
    rows.append([InlineKeyboardButton("⬅️ رجوع" if lang == "ar" else "⬅️ Back", callback_data="buy:menu")])
    return InlineKeyboardMarkup(rows)


def _balances_text(user_id: int, lang: str) -> str:
    lines = []
    for p in wallet.PAID_PLATFORMS:
        a = wallet.availability(user_id, p)
        if lang == "ar":
            lines.append(f"• {pname(p, lang)}: 🎁 {a['free_left']} مجانية | 💎 {a['paid']} مدفوعة")
        else:
            lines.append(f"• {pname(p, lang)}: 🎁 {a['free_left']} free | 💎 {a['paid']} paid")
    return "\n".join(lines)


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE, resume: tuple[str, str] | None = None):
    """يعرض قائمة الشراء. resume=(url, platform) لو المستخدم جاي من محاولة بديلة."""
    user = update.effective_user
    lang = _lang(user.id)
    chat_id = update.effective_chat.id

    if not wallet.payments_enabled():
        msg = "🚫 الشراء متوقف حالياً." if lang == "ar" else "🚫 Purchases are currently disabled."
        await context.bot.send_message(chat_id, msg)
        return

    if resume:
        RESUME_AFTER_PURCHASE[user.id] = resume

    head = "🛒 *شراء تحميلات بالنجوم ⭐*" if lang == "ar" else "🛒 *Buy downloads with Stars ⭐*"
    pick = "اختار المنصة 👇" if lang == "ar" else "Choose a platform 👇"
    text = f"{head}\n\n{_balances_text(user.id, lang)}\n\n{pick}"
    await context.bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=platforms_menu(lang))


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
        await query.answer("🚫 الشراء متوقف حالياً" if lang == "ar" else "🚫 Purchases are disabled", show_alert=True)
        return

    if data == "buy:menu":
        head = "🛒 *شراء تحميلات بالنجوم ⭐*" if lang == "ar" else "🛒 *Buy downloads with Stars ⭐*"
        pick = "اختار المنصة 👇" if lang == "ar" else "Choose a platform 👇"
        await query.edit_message_text(
            f"{head}\n\n{_balances_text(user.id, lang)}\n\n{pick}",
            parse_mode="Markdown", reply_markup=platforms_menu(lang),
        )
        return

    if data.startswith("buy:plat:"):
        platform = data.split(":", 2)[2]
        if platform not in wallet.PAID_PLATFORMS or not wallet.payments_enabled(platform):
            await query.answer("🚫", show_alert=True)
            return
        title = (f"📦 *باقات {pname(platform, lang)}*" if lang == "ar"
                 else f"📦 *{pname(platform, lang)} packages*")
        pick = "اختار الباقة 👇" if lang == "ar" else "Choose a package 👇"
        await query.edit_message_text(f"{title}\n\n{pick}", parse_mode="Markdown",
                                      reply_markup=packages_menu(platform, lang))
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
        if lang == "ar":
            title = f"{pkg['credits']} تحميل - {name}"
            desc = f"رصيد دائم: {pkg['credits']} تحميل بالمحاولة البديلة لمنصة {name}."
        else:
            title = f"{pkg['credits']} downloads - {name}"
            desc = f"Permanent credit: {pkg['credits']} alternative-method downloads for {name}."

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
            await query.answer("⚠️ تعذر إنشاء الفاتورة" if lang == "ar" else "⚠️ Could not create invoice", show_alert=True)


# ---------- pre_checkout و successful_payment ----------

async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لازم نجاوب خلال 10 ثواني. نتحقق أن الباقة والسعر ما تغيروا وان الدفع لسه مفعّل."""
    q = update.pre_checkout_query
    lang = _lang(q.from_user.id)

    async def reject(msg_ar, msg_en):
        await q.answer(ok=False, error_message=msg_ar if lang == "ar" else msg_en)

    try:
        parts = q.invoice_payload.split(":")
        if len(parts) < 4 or parts[0] != "buy":
            await reject("فاتورة غير صالحة.", "Invalid invoice.")
            return
        _, platform, idx, uid = parts[:4]
        if int(uid) != q.from_user.id or q.currency != "XTR":
            await reject("فاتورة غير صالحة.", "Invalid invoice.")
            return
        if platform not in wallet.PAID_PLATFORMS or not wallet.payments_enabled(platform):
            await reject("الشراء متوقف حالياً لهذه المنصة.", "Purchases are disabled for this platform.")
            return
        pkg = wallet.get_packages(platform)[int(idx)]
        if int(pkg["stars"]) != q.total_amount:
            await reject("تغير السعر، افتح المتجر من جديد.", "The price changed, please reopen the shop.")
            return
        if not db.is_connected():
            await reject("الخدمة غير متاحة حالياً، حاول بعد شوي.", "Service unavailable, try again shortly.")
            return
    except Exception:
        logger.exception("pre_checkout validation failed")
        await reject("صار خطأ، حاول مرة ثانية.", "Something went wrong, please try again.")
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
        await msg.reply_text("تم استلام دفعتك، وراح نتواصل وياك لإضافة الرصيد. 🙏" if lang == "ar"
                             else "Payment received, we'll contact you to add your credit. 🙏")
        return

    ok, balance = wallet.add_purchase(
        user.id, platform, credits, pay.total_amount, pay.telegram_payment_charge_id
    )
    if not ok:
        # دفعة مكررة (تليگرام أعاد الإرسال): ما نضيف مرة ثانية
        return

    name = pname(platform, lang)
    if lang == "ar":
        text = f"✅ تم الدفع!\nأُضيف {credits} تحميل لرصيد {name}.\n💎 رصيدك الحالي: {balance}"
    else:
        text = f"✅ Payment successful!\n{credits} downloads added to your {name} balance.\n💎 Current balance: {balance}"
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


# ---------- الإشعارات (قناة الأدمن) ----------

def notify_target():
    """القناة إن وُجدت، وإلا الأدمن بالخاص."""
    return config.NOTIFY_CHANNEL_ID or config.ADMIN_CHAT_ID


async def _notify(context, text: str, markdown: bool = False) -> bool:
    target = notify_target()
    if not target:
        return False
    try:
        await context.bot.send_message(target, text, parse_mode="Markdown" if markdown else None)
        return True
    except Exception:
        logger.exception("failed to send notification to %s", target)
        return False


async def notify(context, text: str, markdown: bool = False) -> bool:
    return await _notify(context, text, markdown)


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
