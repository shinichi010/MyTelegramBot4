"""
لوحة الأدمن للأرصدة والدفع: عرض مستخدم، إهداء/سحب، Refund، تشغيل/إيقاف الدفع (عام + لكل منصة)،
إعدادات الباقات، قناة الإشعارات مع رسالة تجريبية، إحصائيات الإيرادات.

callbacks كلها تبدأ بـ "adm:w:" وتُعالج هنا. حالات الانتظار (إرسال آيدي/رقم) بـ AWAITING_WALLET.
"""
import logging

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.ext import ContextTypes

from . import config, db, wallet, payments, tikhub

logger = logging.getLogger("admin_wallet")

PLATFORM_LABELS = {"douyin": "دويين", "rednote": "RedNote", "wechat": "ويشات"}

# admin_id -> dict(state=..., ...) حالة انتظار نص من الأدمن
AWAITING_WALLET: dict[int, dict] = {}


def _back(cb="adm:w:menu"):
    return [Btn("⬅️ رجوع", callback_data=cb)]


def _fmt_tx(t: dict) -> str:
    ts = t["created_at"].strftime("%m-%d %H:%M")
    kind = {
        "purchase": "🛒 شراء", "gift": "🎁 هدية", "adjust": "➖ تصحيح",
        "consume_free": "🎁 استهلاك مجاني", "consume_paid": "💎 استهلاك",
        "refund_credit": "↩️ إرجاع رصيد", "refund_stars": "💸 استرجاع نجوم",
    }.get(t["type"], t["type"])
    plat = PLATFORM_LABELS.get(t.get("platform"), t.get("platform", ""))
    extra = ""
    if t.get("stars"):
        extra = f" ({t['stars']}⭐)"
    return f"{ts} {kind} {plat} {t.get('credits', 0):+d}{extra}"


def _notify_text() -> str:
    ch = config.NOTIFY_CHANNEL_ID
    lines = [
        "📢 *وجهة الإشعارات*",
        f"القناة: `{ch}`" if ch else "القناة: ❌ غير مضبوطة (ضيف `NOTIFY_CHANNEL_ID` بـ Render)",
        f"الخاص: `{config.ADMIN_CHAT_ID or '—'}`",
        "",
        "اضغط على أي نوع حتى تبدل وجهته بين 📢 القناة و 👤 الخاص:",
    ]
    return "\n".join(lines)


def _notify_markup() -> Markup:
    rows = []
    for kind, label in payments.NOTIFY_KINDS.items():
        dest = "📢 القناة" if payments.get_route(kind) == "channel" else "👤 الخاص"
        rows.append([Btn(f"{label}: {dest}", callback_data=f"adm:w:route:{kind}")])
    rows.append([Btn("📨 إرسال رسالة تجريبية", callback_data="adm:w:notify_test")])
    rows.append(_back())
    return Markup(rows)


# ==================== القائمة الرئيسية ====================

def main_menu() -> Markup:
    on = db.get_setting("payments_enabled", True)
    rows = [
        [Btn("🔍 عرض مستخدم / رصيده", callback_data="adm:w:find")],
        [Btn("🎁 إهداء تحميلات", callback_data="adm:w:gift")],
        [Btn("➖ سحب/تصحيح رصيد", callback_data="adm:w:take")],
        [Btn("💸 استرجاع عملية (Refund)", callback_data="adm:w:refund")],
        [Btn(f"💳 الدفع (عام): {'🟢 مفعّل' if on else '🔴 موقف'}", callback_data="adm:w:pay_toggle")],
        [Btn("🌐 الدفع لكل منصة", callback_data="adm:w:pay_platforms")],
        [Btn("📦 الباقات والأسعار", callback_data="adm:w:pkgs")],
        [Btn("📢 قناة الإشعارات + تجربة", callback_data="adm:w:notify")],
        [Btn("💰 إيرادات وإحصائيات", callback_data="adm:w:stats")],
        _back("adm:back"),
    ]
    return Markup(rows)


async def handle_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, is_admin) -> bool:
    """يرجع True لو تعامل مع الـ callback. is_admin: دالة من bot.py."""
    query = update.callback_query
    data = query.data
    if not data.startswith("adm:w:"):
        return False
    if not is_admin(query.from_user.id):
        await query.answer("هذا القسم للأدمن بس 🚫", show_alert=True)
        return True
    await query.answer()
    admin_id = query.from_user.id
    AWAITING_WALLET.pop(admin_id, None)
    action = data[len("adm:w:"):]

    if action == "menu":
        await query.edit_message_text("💰 الأرصدة والدفع", reply_markup=main_menu())

    # ----- عرض مستخدم -----
    elif action == "find":
        AWAITING_WALLET[admin_id] = {"state": "find"}
        await query.edit_message_text(
            "🔍 ارسل آيدي المستخدم (رقم) او @يوزره:", reply_markup=Markup([_back()]))

    # ----- إهداء -----
    elif action == "gift":
        AWAITING_WALLET[admin_id] = {"state": "gift_user"}
        await query.edit_message_text(
            "🎁 ارسل آيدي المستخدم او @يوزره اللي تريد تهديه:", reply_markup=Markup([_back()]))

    elif action.startswith("gift_plat:") or action.startswith("take_plat:"):
        kind, _, rest = action.partition("_plat:")
        target = int(rest.split(":")[0]); platform = rest.split(":")[1]
        AWAITING_WALLET[admin_id] = {"state": f"{kind}_amount", "target": target, "platform": platform}
        verb = "تهديه" if kind == "gift" else "تسحبه"
        await query.edit_message_text(
            f"كم تحميل تريد {verb} ({PLATFORM_LABELS[platform]})؟ ارسل رقم صحيح.",
            reply_markup=Markup([_back()]))

    # ----- سحب/تصحيح -----
    elif action == "take":
        AWAITING_WALLET[admin_id] = {"state": "take_user"}
        await query.edit_message_text(
            "➖ ارسل آيدي المستخدم او @يوزره اللي تريد تسحب من رصيده:", reply_markup=Markup([_back()]))

    # ----- Refund -----
    elif action == "refund":
        AWAITING_WALLET[admin_id] = {"state": "refund_user"}
        await query.edit_message_text(
            "💸 ارسل آيدي المستخدم او @يوزره لأعرض آخر مشترياته وتختار منها:",
            reply_markup=Markup([_back()]))

    elif action.startswith("refund_pick:"):
        charge_id = action.split(":", 1)[1]
        tx = wallet.get_purchase(charge_id)
        if not tx:
            await query.edit_message_text("ما لقيت العملية.", reply_markup=Markup([_back()]))
            return True
        await query.edit_message_text(
            f"تأكيد استرجاع؟\n\n👤 {tx['user_id']}\n📦 {tx['credits']} تحميل "
            f"({PLATFORM_LABELS.get(tx['platform'])})\n⭐ {tx['stars']}\n\n"
            "راح ترجع النجوم للمستخدم ويُسحب الرصيد الممنوح.",
            reply_markup=Markup([
                [Btn("✅ نعم، استرجع", callback_data=f"adm:w:refund_do:{charge_id}")],
                _back(),
            ]))

    elif action.startswith("refund_do:"):
        charge_id = action.split(":", 1)[1]
        ok, msg = await payments.do_refund(context, charge_id)
        await query.edit_message_text(("✅ " if ok else "❌ ") + msg, reply_markup=Markup([_back()]))
        if ok:
            tx = wallet.get_purchase(charge_id)
            try:
                await context.bot.send_message(tx["user_id"], "💸 تم استرجاع نجومك لعملية شراء. لأي استفسار: /paysupport")
            except Exception:
                pass

    # ----- تشغيل/إيقاف الدفع -----
    elif action == "pay_toggle":
        db.set_setting("payments_enabled", not db.get_setting("payments_enabled", True))
        await query.edit_message_text("💰 الأرصدة والدفع", reply_markup=main_menu())

    elif action == "pay_platforms":
        rows = []
        for p, label in PLATFORM_LABELS.items():
            on = db.get_setting(f"payments_enabled_{p}", True)
            rows.append([Btn(f"{label}: {'🟢 مفعّل' if on else '🔴 موقف'}", callback_data=f"adm:w:pay_plat:{p}")])
        rows.append(_back())
        await query.edit_message_text("الدفع لكل منصة 👇", reply_markup=Markup(rows))

    elif action.startswith("pay_plat:"):
        p = action.split(":", 1)[1]
        db.set_setting(f"payments_enabled_{p}", not db.get_setting(f"payments_enabled_{p}", True))
        rows = []
        for pp, label in PLATFORM_LABELS.items():
            on = db.get_setting(f"payments_enabled_{pp}", True)
            rows.append([Btn(f"{label}: {'🟢 مفعّل' if on else '🔴 موقف'}", callback_data=f"adm:w:pay_plat:{pp}")])
        rows.append(_back())
        await query.edit_message_text("الدفع لكل منصة 👇", reply_markup=Markup(rows))

    # ----- الباقات -----
    elif action == "pkgs":
        rows = [[Btn(label, callback_data=f"adm:w:pkgs_plat:{p}")] for p, label in PLATFORM_LABELS.items()]
        rows.append(_back())
        await query.edit_message_text("اختار المنصة لتعديل باقاتها 👇", reply_markup=Markup(rows))

    elif action.startswith("pkgs_plat:"):
        p = action.split(":", 1)[1]
        pk = wallet.get_packages(p)
        lines = "\n".join(f"{i+1}) {x['credits']} تحميل = {x['stars']} ⭐" for i, x in enumerate(pk))
        AWAITING_WALLET[admin_id] = {"state": "pkgs_edit", "platform": p}
        await query.edit_message_text(
            f"📦 باقات {PLATFORM_LABELS[p]} الحالية:\n{lines}\n\n"
            "ارسل الباقات الجديدة، كل باقة بسطر بهالصيغة: `العدد النجوم`\n"
            "مثال:\n`10 15`\n`30 40`\n`100 120`",
            parse_mode="Markdown", reply_markup=Markup([_back("adm:w:pkgs")]))

    # ----- قناة الإشعارات + وجهة كل نوع -----
    elif action == "notify":
        await query.edit_message_text(_notify_text(), parse_mode="Markdown", reply_markup=_notify_markup())

    elif action.startswith("route:"):
        kind = action.split(":", 1)[1]
        if kind in payments.NOTIFY_KINDS:
            current = payments.get_route(kind)
            if current == "private" and not config.NOTIFY_CHANNEL_ID:
                await query.answer("ضيف NOTIFY_CHANNEL_ID بـ Render أولاً حتى تكدر تختار القناة ⚠️", show_alert=True)
            else:
                payments.set_route(kind, "private" if current == "channel" else "channel")
        await query.edit_message_text(_notify_text(), parse_mode="Markdown", reply_markup=_notify_markup())

    elif action == "notify_test":
        lines = []
        seen = {}
        for kind, label in payments.NOTIFY_KINDS.items():
            tgt = payments.target_for(kind)
            if tgt in seen:
                lines.append(f"• {label}: {seen[tgt]}")
                continue
            ok = await payments.notify(context, f"✅ رسالة تجريبية من البوت ({label}) — الإشعارات تشتغل هنا.", kind=kind)
            seen[tgt] = "✅ وصلت" if ok else "❌ فشل"
            lines.append(f"• {label} ← `{tgt}`: {seen[tgt]}")
        msg = "📨 نتيجة التجربة:\n\n" + "\n".join(lines)
        if any("فشل" in v for v in seen.values()):
            msg += ("\n\nإذا فشلت القناة تأكد من:\n"
                    "• البوت مضاف للقناة كـ *أدمن* وعنده صلاحية نشر الرسائل\n"
                    "• آيدي القناة صحيح (يبدأ بـ -100)\n"
                    "• متغير `NOTIFY_CHANNEL_ID` مضاف بـ Render وسويت Deploy")
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=Markup([_back("adm:w:notify")]))

    # ----- إحصائيات -----
    elif action == "stats":
        r = wallet.revenue_stats()
        out = r["outstanding"]
        text = (
            "💰 *إيرادات وإحصائيات*\n\n"
            f"🛒 عمليات الشراء: {r['purchases']}\n"
            f"⭐ مجموع النجوم: {r['stars']} (≈ ${r['stars'] * 0.013:.2f})\n"
            f"💸 نجوم مسترجعة: {r['refunded_stars']}\n\n"
            "📊 *أرصدة موزعة على المستخدمين (التزاماتك):*\n"
            f"• دويين: {out['douyin']}\n• RedNote: {out['rednote']}\n• ويشات: {out['wechat']}\n"
        )
        top = wallet.top_spenders(5)
        if top:
            text += "\n🏆 *أكثر المنفقين:*\n" + "\n".join(
                f"• `{t['user_id']}`: {t['stars']}⭐ ({t['n']} عملية)" for t in top)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=Markup([_back()]))

    return True


# ==================== استقبال نصوص الأدمن (آيدي/أرقام) ====================

async def handle_wallet_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يرجع True لو النص كان جواب لحالة انتظار محفظة (وتم التعامل معه)."""
    admin_id = update.effective_user.id
    st = AWAITING_WALLET.get(admin_id)
    if not st:
        return False
    text = (update.message.text or "").strip()
    state = st["state"]

    # ---- الخطوة: تحديد مستخدم ----
    if state in ("find", "gift_user", "take_user", "refund_user"):
        uid = wallet.find_user_id(text)
        if uid is None:
            await update.message.reply_text("ما لقيت هذا المستخدم ❌ (لازم يكون استخدم البوت قبل، او ارسل آيدي رقمي)")
            return True
        AWAITING_WALLET.pop(admin_id, None)

        if state == "find":
            await update.message.reply_text(_user_report(uid), parse_mode="Markdown")
        elif state in ("gift_user", "take_user"):
            kind = "gift" if state == "gift_user" else "take"
            rows = [[Btn(label, callback_data=f"adm:w:{kind}_plat:{uid}:{p}")] for p, label in PLATFORM_LABELS.items()]
            rows.append(_back())
            await update.message.reply_text(
                f"المستخدم `{uid}`\n" + _balances_lines(uid) + "\n\nاختار المنصة:",
                parse_mode="Markdown", reply_markup=Markup(rows))
        else:  # refund_user
            purchases = wallet.recent_purchases(uid, 8)
            if not purchases:
                await update.message.reply_text("ما عنده مشتريات.")
                return True
            rows = []
            for t in purchases:
                tag = "✅مسترجع " if t.get("refunded") else ""
                label = f"{tag}{t['credits']} {PLATFORM_LABELS.get(t['platform'])} — {t['stars']}⭐ ({t['created_at']:%m-%d})"
                if not t.get("refunded"):
                    rows.append([Btn(label, callback_data=f"adm:w:refund_pick:{t['charge_id']}")])
                else:
                    rows.append([Btn(label, callback_data="adm:w:menu")])
            rows.append(_back())
            await update.message.reply_text("اختار العملية اللي تريد تسترجعها:", reply_markup=Markup(rows))
        return True

    # ---- الخطوة: كمية الإهداء/السحب ----
    if state in ("gift_amount", "take_amount"):
        try:
            n = int(text)
            if n <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("لازم رقم صحيح اكبر من صفر ❌")
            return True
        AWAITING_WALLET.pop(admin_id, None)
        uid, p = st["target"], st["platform"]
        if state == "gift_amount":
            bal = wallet.gift(uid, p, n, admin_id, note="admin gift")
            await update.message.reply_text(f"✅ انهدى {n} تحميل ({PLATFORM_LABELS[p]}) للمستخدم `{uid}`\nرصيده الآن: {bal}",
                                            parse_mode="Markdown")
            try:
                await context.bot.send_message(uid, f"🎁 وصلتك هدية: {n} تحميل بالمحاولة البديلة ({PLATFORM_LABELS[p]})!")
            except Exception:
                await update.message.reply_text("(ما كدرت أبلغ المستخدم - ممكن حاظر البوت)")
        else:
            bal = wallet.gift(uid, p, -n, admin_id, note="admin take")
            await update.message.reply_text(f"✅ تم السحب. رصيده الآن ({PLATFORM_LABELS[p]}): {bal}")
        return True

    # ---- الخطوة: تعديل الباقات ----
    if state == "pkgs_edit":
        pk = []
        try:
            for line in text.splitlines():
                if not line.strip():
                    continue
                c, s = line.split()
                c, s = int(c), int(s)
                if c <= 0 or s <= 0:
                    raise ValueError
                pk.append({"credits": c, "stars": s})
            if not pk or len(pk) > 6:
                raise ValueError
        except ValueError:
            await update.message.reply_text("صيغة غلط ❌ لازم كل سطر: `العدد النجوم` (من 1 إلى 6 باقات)", parse_mode="Markdown")
            return True
        AWAITING_WALLET.pop(admin_id, None)
        wallet.set_packages(st["platform"], pk)
        await update.message.reply_text(f"✅ تحدثت باقات {PLATFORM_LABELS[st['platform']]} ({len(pk)} باقة).")
        return True

    return False


def _balances_lines(uid: int) -> str:
    lines = []
    for p, label in PLATFORM_LABELS.items():
        a = wallet.availability(uid, p)
        lines.append(f"• {label}: 🎁 {a['free_left']}/{wallet.get_weekly_free_limit(p)} مجاني | 💎 {a['paid']} مدفوع")
    return "\n".join(lines)


def _user_report(uid: int) -> str:
    info = db.get_user_info(uid) or {}
    stats = db.get_user_link_stats(uid)
    name = info.get("full_name") or "—"
    uname = f"@{info['username']}" if info.get("username") else "—"
    joined = info["joined_at"].strftime("%Y-%m-%d") if info.get("joined_at") else "—"
    banned = "⛔ محظور" if db.is_banned(uid) else "🟢 نشط"
    lines = [
        f"👤 *{name}* ({uname})", f"🆔 `{uid}`", f"📅 عضو منذ: {joined} | {banned}",
        f"🔗 مجموع التحميلات: {stats['total']}", "", "💼 *الأرصدة:*", _balances_lines(uid),
    ]
    hist = wallet.user_history(uid, 8)
    if hist:
        lines += ["", "🧾 *آخر الحركات:*"] + [_fmt_tx(t) for t in hist]
    return "\n".join(lines)
