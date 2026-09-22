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
# كل مفتاح فيه نسختين: ar (عربي) و en (إنكليزي)
DEFAULT_MESSAGES = {
    "welcome": {
        "ar": (
            "هلا والف هلا بيك 👋\n\n"
            "ارسلي رابط فيديو من X ( تويتر )، دويين ( التيك توك الصيني )، ويشات ( WeChat )، "
            "RedNote ( شياوخونغشو )، او Bilibili وراح أنزلك المحتوى.\n\n"
            "💠 روابط X، RedNote، وBilibili: راح تطلع الك خيارات جودة (مع الحجم) تختار منها.\n"
            "💠 روابط دويين وويشات: يتنزل تلقائياً بأعلى جودة متوفرة (فيديو او صور).\n\n"
            "📊 ارسل /stats حتى تشوف إحصائياتك وتتحكم بإعداداتك الشخصية "
            "(معلومات المنشور، التحقق من الرابط، المعاينة السريعة)."
        ),
        "en": (
            "Hey there 👋\n\n"
            "Send me a video link from X (Twitter), Douyin (Chinese TikTok), WeChat Channels, "
            "RedNote (Xiaohongshu), or Bilibili and I'll download it for you.\n\n"
            "💠 X, RedNote, and Bilibili links: you'll get quality options (with size) to choose from.\n"
            "💠 Douyin and WeChat links: downloaded automatically at the best available quality (video or photos).\n\n"
            "📊 Send /stats to see your stats and control your personal settings "
            "(post info, link verification, quick preview)."
        ),
    },
    "unsupported_link": {
        "ar": "بس روابط X، دويين، ويشات، RedNote، او Bilibili مدعومة حالياً 🙏",
        "en": "Only X, Douyin, WeChat, RedNote, or Bilibili links are supported right now 🙏",
    },
    "fetching_qualities": {
        "ar": "🔍 اجيب خيارات الجودة...",
        "en": "🔍 Fetching quality options...",
    },
    "choose_quality": {
        "ar": "اختار الجودة اللي تريدها 👇",
        "en": "Choose the quality you want 👇",
    },
    "downloading": {
        "ar": "⬇️ جاري التحميل...",
        "en": "⬇️ Downloading...",
    },
    "downloading_douyin": {
        "ar": "⬇️ جاري التحميل بأعلى جودة...",
        "en": "⬇️ Downloading at the best quality...",
    },
    "download_error": {
        "ar": "صار خطأ بالتحميل ❌\nجرب مرة ثانية، وإذا استمرت المشكلة اضغط الزر تحت للإبلاغ.",
        "en": "A download error occurred ❌\nTry again, and if it keeps happening tap the button below to report it.",
    },
    "post_info_error": {
        "ar": "صار خطأ بجلب معلومات المنشور ❌",
        "en": "Failed to fetch post info ❌",
    },
    "post_info_template": {
        "ar": (
            "ℹ️ *معلومات المنشور*\n"
            "👤 الاسم: {uploader}\n"
            "🔗 اليوزر: {handle}\n"
            "📝 الوصف: {description}\n"
            "{count_line}"
        ),
        "en": (
            "ℹ️ *Post Info*\n"
            "👤 Name: {uploader}\n"
            "🔗 Handle: {handle}\n"
            "📝 Description: {description}\n"
            "{count_line}"
        ),
    },
    "quality_fetch_error": {
        "ar": "ما گدرت اجيب معلومات الرابط ❌\nجرب مرة ثانية، وإذا استمرت المشكلة اضغط الزر تحت للإبلاغ.",
        "en": "Couldn't fetch link info ❌\nTry again, and if it keeps happening tap the button below to report it.",
    },
    "expired_request": {
        "ar": "انتهت صلاحية هذا الطلب، ارسل الرابط مرة اخرى 🔄",
        "en": "This request has expired, please send the link again 🔄",
    },
    "platform_disabled": {
        "ar": "التحميل من هذه المنصة متوقف حالياً 🚫",
        "en": "Downloads from this platform are currently disabled 🚫",
    },
    "user_banned": {
        "ar": "ما تكدر تستخدم البوت حالياً 🚫",
        "en": "You can't use this bot right now 🚫",
    },
    "maintenance_mode": {
        "ar": "🛠️ البوت تحت الصيانة حالياً، رجاءً حاول بعد شوي.",
        "en": "🛠️ The bot is under maintenance right now, please try again shortly.",
    },
    "file_too_large": {
        "ar": "الملف حجمه اكبر من {max_size} ميكا، ما يگدر البوت يرسله ❌",
        "en": "The file is larger than {max_size} MB, the bot can't send it ❌",
    },
    "queue_wait": {
        "ar": (
            "⏳ حالياً اكو تحميل ثقيل شغال. انت رقمك {position} بالطابور.\n"
            "راح يبدأ تحميلك تلقائياً بعد ما يخلص اللي گبلك."
        ),
        "en": (
            "⏳ A heavy download is currently running. You're number {position} in the queue.\n"
            "Your download will start automatically once the one before you finishes."
        ),
    },
    "fallback_retrying": {
        "ar": "🔄 جاري إعادة المحاولة بطريقة بديلة...",
        "en": "🔄 Retrying with an alternative method...",
    },
    "fallback_failed": {
        "ar": "❌ فشلت المحاولة البديلة بعد.\nما انخصم شي من رصيدك ✅\nجرب مرة ثانية، وإذا استمرت المشكلة اضغط الزر تحت للإبلاغ.",
        "en": "❌ The alternative method also failed.\nNothing was deducted from your balance ✅\nTry again, and if it keeps happening tap the button below to report it.",
    },
    "fallback_limit_reached": {
        "ar": (
            "وصلت لحد المحاولات البديلة المسموحة هذا الأسبوع ({limit}). "
            "حاول مرة اخرى الأسبوع الجاي 🔁"
        ),
        "en": (
            "You've reached this week's alternative-attempt limit ({limit}). "
            "Try again next week 🔁"
        ),
    },
    "link_verify_failed": {
        "ar": "هذا الرابط ما يشتغل او غير متاح ❌\n{url}",
        "en": "This link doesn't work or isn't available ❌\n{url}",
    },
    "fallback_confirm": {
        "ar": (
            "🔀 *المحاولة البديلة* ({platform})\n\n"
            "هذي الطريقة تشتغل 100% وتنزل الفيديو من مصدر بديل.\n\n"
            "🎁 مجانية متبقية هذا الأسبوع: {free_left}\n"
            "💎 رصيدك المدفوع: {paid_balance}\n\n"
            "راح تُستهلك من المجانية أولاً، وبعدها من رصيدك المدفوع.\n"
            "ما ينخصم شي إلا بعد ما يوصلك الفيديو ✅"
        ),
        "en": (
            "🔀 *Alternative method* ({platform})\n\n"
            "This method works 100% and downloads the video from a backup source.\n\n"
            "🎁 Free attempts left this week: {free_left}\n"
            "💎 Your paid balance: {paid_balance}\n\n"
            "Free attempts are used first, then your paid balance.\n"
            "Nothing is deducted until the video is delivered ✅"
        ),
    },
    "fallback_no_credit": {
        "ar": (
            "🔀 *المحاولة البديلة* ({platform})\n\n"
            "خلصت محاولاتك المجانية لهذا الأسبوع وما عندك رصيد بـ{platform}.\n"
            "تقدر تشتري تحميلات بالنجوم ⭐ وتكمل، وراح ينزل هذا الفيديو تلقائياً بعد الدفع:"
        ),
        "en": (
            "🔀 *Alternative method* ({platform})\n\n"
            "You've used your free attempts this week and have no {platform} balance.\n"
            "You can buy downloads with Stars ⭐ - this video will download automatically after payment:"
        ),
    },
    "paysupport": {
        "ar": (
            "🛟 *دعم المدفوعات*\n\n"
            "لو صارت مشكلة بأي عملية دفع (نجوم انخصمت وما وصلك رصيد، او تريد استرجاع)، "
            "تواصل وياي مباشرة: @snh_1\n\n"
            "ابعث آيدي حسابك وصورة الفاتورة إن أمكن."
        ),
        "en": (
            "🛟 *Payment support*\n\n"
            "If anything goes wrong with a payment (Stars charged but no credit, or you want a refund), "
            "contact me directly: @snh_1\n\n"
            "Please include your account ID and a screenshot of the invoice if possible."
        ),
    },
    "wechat_disabled": {
        "ar": "تحميل ويشات مو مفعّل حالياً 🙏",
        "en": "WeChat downloads aren't enabled right now 🙏",
    },
    # ============ المرحلة الثانية: نصوص كانت مدفونة بالكود ============
    "help": {
        "ar": (
            "📖 *الأوامر المتوفرة*\n\n"
            "/start — رسالة الترحيب وشرح المنصات المدعومة\n"
            "/stats — إحصائياتك الشخصية + رصيدك + إعداداتك\n"
            "/buy — شراء تحميلات بالنجوم ⭐\n"
            "/help — هذي الرسالة\n\n"
            "📎 *شلون تستخدم البوت*\n"
            "بس ارسل رابط من X، دويين، ويشات، RedNote، او Bilibili — تقدر ترسل عدة "
            "روابط بنفس الرسالة وراح انزلهن وحدة وحدة بالترتيب.\n\n"
            "▪️ روابط X، RedNote، وBilibili: تطلع الك خيارات جودة مع الحجم تختار منها.\n"
            "▪️ روابط دويين وويشات: تتنزل تلقائياً بأعلى جودة متوفرة.\n"
            "▪️ اي فيديو تكدر تحمل الصوت بس منه (MP3) بزر منفصل."
        ),
        "en": (
            "📖 *Available commands*\n\n"
            "/start — Welcome message and supported platforms\n"
            "/stats — Your personal stats + balance + settings\n"
            "/buy — Buy downloads with Stars ⭐\n"
            "/help — This message\n\n"
            "📎 *How to use the bot*\n"
            "Just send a link from X, Douyin, WeChat, RedNote, or Bilibili — you can send several "
            "links in one message and I'll download them one by one in order.\n\n"
            "▪️ X, RedNote and Bilibili links: you get quality options with sizes to choose from.\n"
            "▪️ Douyin and WeChat links: downloaded automatically in the best available quality.\n"
            "▪️ For any video you can download the audio only (MP3) with a separate button."
        ),
    },
    "help_admin": {
        "ar": "\n\n🛠️ انت أدمن - استخدم /admin لفتح لوحة التحكم.\nلو صارت عالق بمنتصف تعديل رسالة/ستيكر/حد رقمي وتريد تلغيه، استخدم /cancel.",
        "en": "\n\n🛠️ You're an admin - use /admin to open the control panel.\nIf you get stuck in the middle of editing a message/sticker/limit, use /cancel.",
    },
    "deeplink_invalid": {
        "ar": "رابط الـ Deep Link غير صالح، جرب ترسل الرابط مباشرة ❌",
        "en": "The deep link is invalid, try sending the link directly ❌",
    },
    "cancel_done": {
        "ar": "✅ تم إلغاء العملية المعلقة.",
        "en": "✅ Pending operation cancelled.",
    },
    "cancel_none": {
        "ar": "ماكو عملية معلقة حالياً.",
        "en": "There's no pending operation.",
    },
    "multi_links": {
        "ar": "📋 لقيت {count} روابط بالرسالة، راح انزلهن وحدة وحدة بالترتيب.",
        "en": "📋 Found {count} links in the message, I'll download them one by one in order.",
    },
    "verifying_link": {
        "ar": "🔎 جاري التحقق من الرابط...",
        "en": "🔎 Verifying the link...",
    },
    "retrying": {
        "ar": "🔄 جاري إعادة المحاولة...",
        "en": "🔄 Retrying...",
    },
    "downloading_audio": {
        "ar": "🎵 جاري تحميل الصوت...",
        "en": "🎵 Downloading audio...",
    },
    "invalid_request": {
        "ar": "طلب غير صالح ❌",
        "en": "Invalid request ❌",
    },
    "no_files": {
        "ar": "ما گدرت انزل هذا المنشور",
        "en": "Couldn't download this post",
    },
    "post_info_disabled": {
        "ar": "معلومات المنشور موقفة عام من الأدمن حالياً 🚫",
        "en": "Post info is disabled globally by the admin right now 🚫",
    },
    "preview_disabled": {
        "ar": "المعاينة السريعة موقفة عام من الأدمن حالياً 🚫",
        "en": "Quick preview is disabled globally by the admin right now 🚫",
    },
    "fallback_disabled": {
        "ar": "المحاولة البديلة موقفة حالياً 🚫",
        "en": "The alternative method is disabled right now 🚫",
    },
    "fallback_admin": {
        "ar": "🔀 *المحاولة البديلة*\n\nأنت أدمن: مجاني وما ينخصم منك شي ✅",
        "en": "🔀 *Alternative method*\n\nYou're an admin: free, nothing is deducted ✅",
    },
    "low_balance_one": {
        "ar": "ℹ️ باقي لك تحميل واحد بالمحاولة البديلة ({platform}).",
        "en": "ℹ️ You have 1 alternative-method download left ({platform}).",
    },
    "low_balance_zero": {
        "ar": "ℹ️ خلص رصيدك بالمحاولة البديلة ({platform}). تكدر تشتري تحميلات بـ /buy ⭐",
        "en": "ℹ️ You're out of alternative-method downloads ({platform}). Use /buy to top up ⭐",
    },
    "stats_title": {
        "ar": "📊 *إحصائياتك بالبوت*\n",
        "en": "📊 *Your stats*\n",
    },
    "stats_no_downloads": {
        "ar": "\nما عندك تحميلات مسجلة لحد هسه 📭",
        "en": "\nNo downloads recorded yet 📭",
    },
    "stats_balance_title": {
        "ar": "\n💼 *رصيد المحاولة البديلة*",
        "en": "\n💼 *Alternative-method balance*",
    },
    "stats_balance_line": {
        "ar": "  • {platform}: 🎁 {free} مجانية | 💎 {paid} مدفوعة",
        "en": "  • {platform}: 🎁 {free} free | 💎 {paid} paid",
    },
    "shop_title": {
        "ar": "🛒 *شراء تحميلات بالنجوم ⭐*",
        "en": "🛒 *Buy downloads with Stars ⭐*",
    },
    "shop_pick_platform": {
        "ar": "اختار المنصة 👇",
        "en": "Choose a platform 👇",
    },
    "shop_platform_title": {
        "ar": "📦 *باقات {platform}*",
        "en": "📦 *{platform} packages*",
    },
    "shop_pick_package": {
        "ar": "اختار الباقة 👇",
        "en": "Choose a package 👇",
    },
    "shop_disabled": {
        "ar": "🚫 الشراء متوقف حالياً.",
        "en": "🚫 Purchases are currently disabled.",
    },
    "shop_invoice_error": {
        "ar": "⚠️ تعذر إنشاء الفاتورة",
        "en": "⚠️ Could not create invoice",
    },
    "invoice_title": {
        "ar": "{credits} تحميل - {platform}",
        "en": "{credits} downloads - {platform}",
    },
    "invoice_desc": {
        "ar": "رصيد دائم: {credits} تحميل بالمحاولة البديلة لمنصة {platform}.",
        "en": "Permanent credit: {credits} alternative-method downloads for {platform}.",
    },
    "pay_invalid_invoice": {
        "ar": "فاتورة غير صالحة.",
        "en": "Invalid invoice.",
    },
    "pay_platform_disabled": {
        "ar": "الشراء متوقف حالياً لهذه المنصة.",
        "en": "Purchases are disabled for this platform.",
    },
    "pay_price_changed": {
        "ar": "تغير السعر، افتح المتجر من جديد.",
        "en": "The price changed, please reopen the shop.",
    },
    "pay_unavailable": {
        "ar": "الخدمة غير متاحة حالياً، حاول بعد شوي.",
        "en": "Service unavailable, try again shortly.",
    },
    "pay_error": {
        "ar": "صار خطأ، حاول مرة ثانية.",
        "en": "Something went wrong, please try again.",
    },
    "pay_success": {
        "ar": "✅ تم الدفع!\nأُضيف {credits} تحميل لرصيد {platform}.\n💎 رصيدك الحالي: {balance}",
        "en": "✅ Payment successful!\n{credits} downloads added to your {platform} balance.\n💎 Current balance: {balance}",
    },
    "pay_unknown_payload": {
        "ar": "تم استلام دفعتك، وراح نتواصل وياك لإضافة الرصيد. 🙏",
        "en": "Payment received, we'll contact you to add your credit. 🙏",
    },
    "refund_user_notice": {
        "ar": "💸 تم استرجاع نجومك لعملية شراء. لأي استفسار: /paysupport",
        "en": "💸 Your Stars for a purchase were refunded. Questions: /paysupport",
    },
    "gift_user_notice": {
        "ar": "🎁 وصلتك هدية: {credits} تحميل بالمحاولة البديلة ({platform})!",
        "en": "🎁 You received a gift: {credits} alternative-method downloads ({platform})!",
    },
    # أزرار (تنعدل من الأدمن مثل باقي الرسائل)
    "btn_close": {"ar": "❌ إغلاق", "en": "❌ Close"},
    "btn_back": {"ar": "⬅️ رجوع", "en": "⬅️ Back"},
    "btn_cancel": {"ar": "❌ إلغاء", "en": "❌ Cancel"},
    "btn_cancel_op": {"ar": "❌ إلغاء العملية", "en": "❌ Cancel operation"},
    "btn_retry": {"ar": "🔄 أعد المحاولة", "en": "🔄 Retry"},
    "btn_fallback": {"ar": "🔀 محاولة بديلة", "en": "🔀 Alternative method"},
    "btn_use_fallback": {"ar": "✅ استخدم المحاولة", "en": "✅ Use this method"},
    "btn_buy_platform": {"ar": "🛒 اشتري تحميلات {platform}", "en": "🛒 Buy {platform} downloads"},
    "btn_buy": {"ar": "🛒 اشتري تحميلات ⭐", "en": "🛒 Buy downloads ⭐"},
    "btn_package": {"ar": "{credits} تحميل — {stars} ⭐", "en": "{credits} downloads — {stars} ⭐"},
    "btn_audio": {"ar": "🎵 حمل الصوت بس (MP3)", "en": "🎵 Audio only (MP3)"},
    "btn_audio_only": {"ar": "🎵 صوت فقط (MP3)", "en": "🎵 Audio only (MP3)"},
    "btn_done": {"ar": "✅ تم", "en": "✅ Done"},
    "btn_post_info": {"ar": "ℹ️ معلومات المنشور", "en": "ℹ️ Post info"},
    "btn_verify_link": {"ar": "🔎 التحقق من الرابط", "en": "🔎 Link verification"},
    "btn_preview": {"ar": "👁️ معاينة سريعة قبل التحميل", "en": "👁️ Quick preview"},
    "state_on_f": {"ar": "🟢 مفعّلة", "en": "🟢 On"},
    "state_off_f": {"ar": "🔴 موقفة", "en": "🔴 Off"},
    "state_on_m": {"ar": "🟢 مفعّل", "en": "🟢 On"},
    "state_off_m": {"ar": "🔴 موقف", "en": "🔴 Off"},
    "best_quality": {"ar": "أفضل جودة متوفرة", "en": "Best available quality"},
    "size_unit": {"ar": "ميكا", "en": "MB"},
    "post_multi_extra": {"ar": " (المنشور فيه {count} مقاطع/صور، راح تنزل كلهن)", "en": " (this post has {count} items, all will be downloaded)"},
    "member_since": {"ar": "📅 عضو منذ", "en": "📅 Member since"},
    "total_downloads": {"ar": "🔗 مجموع التحميلات", "en": "🔗 Total downloads"},
    "platform_x": {"ar": "X (تويتر)", "en": "X (Twitter)"},
    "platform_douyin": {"ar": "دويين", "en": "Douyin"},
    "platform_wechat": {"ar": "ويشات", "en": "WeChat"},
    "platform_rednote": {"ar": "RedNote", "en": "RedNote"},
    "platform_bilibili": {"ar": "Bilibili", "en": "Bilibili"},
    "post_unknown": {"ar": "غير معروف", "en": "Unknown"},
    "post_no_handle": {"ar": "بدون يوزر", "en": "no handle"},
    "post_no_desc": {"ar": "بدون وصف", "en": "no description"},
    "post_count_line": {"ar": "🎞️ عدد المقاطع/الصور: {count}", "en": "🎞️ Number of items: {count}"},
    "dev_error_report": {
        "ar": (
            "🚨 *خطأ - {kind}*\n\n"
            "👤 المستخدم: `{user_id}` {username}\n"
            "🌐 المنصة: {platform}\n"
            "🔗 الرابط: {url}\n\n"
            "❗️ *الخطأ:*\n`{error}`"
        ),
        "en": (
            "🚨 *Error - {kind}*\n\n"
            "👤 User: `{user_id}` {username}\n"
            "🌐 Platform: {platform}\n"
            "🔗 URL: {url}\n\n"
            "❗️ *Error:*\n`{error}`"
        ),
    },
    "btn_report_problem": {"ar": "🚨 أبلغ عن المشكلة", "en": "🚨 Report the problem"},
    "btn_seen": {"ar": "✅ شفتها", "en": "✅ Seen"},
    "report_ask": {
        "ar": "✍️ اكتب المشكلة اللي صارت وياك هسه، وراح ارسلها للفريق مباشرة.",
        "en": "✍️ Write the problem you're facing now, and I'll send it straight to the team.",
    },
    "report_sent": {
        "ar": "✅ توصلت شكواك، شكراً الك. راح نراجعها بأقرب وقت 🙏",
        "en": "✅ Your report was received, thank you. We'll review it soon 🙏",
    },
    "user_report": {
        "ar": (
            "🚨 *بلاغ مشكلة من مستخدم*\n\n"
            "👤 المستخدم: `{user_id}` {username}\n"
            "🌐 السياق: {platform} — {url}\n"
            "❗️ الخطأ الأصلي: `{error}`\n\n"
            "💬 *رسالة المستخدم:*\n{message}"
        ),
        "en": (
            "🚨 *User problem report*\n\n"
            "👤 User: `{user_id}` {username}\n"
            "🌐 Context: {platform} — {url}\n"
            "❗️ Original error: `{error}`\n\n"
            "💬 *User's message:*\n{message}"
        ),
    },
}


# نسخ قديمة معروفة لرسائل تغير نصها بتحديث. اذا نص الرسالة المخزن بالقاعدة يطابق حرفياً واحدة منها
# (يعني الأدمن ما عدلها) نستبدله تلقائياً بالجديد عند التشغيل. اذا الأدمن عدلها بيده ما نلمسها أبداً.
LEGACY_MESSAGES = {
    "fallback_confirm": {
        "ar": [
            "🔀 *المحاولة البديلة*\n\nهذي الطريقة تشتغل 100%، بس راح تصير مدفوعة مستقبلاً.\nعندك حالياً {remaining} من {limit} محاولة مجانية متبقية هذا الأسبوع.\n\nتريد تستخدمها؟",
        ],
        "en": [
            "🔀 *Alternative Method*\n\nThis method works 100%, but it will become paid in the future.\nYou currently have {remaining} of {limit} free attempts left this week.\n\nDo you want to use it?",
        ],
    },
    "download_error": {
        "ar": ["صار خطأ بالتحميل ❌\n{error}"],
        "en": ["A download error occurred ❌\n{error}"],
    },
    "post_info_error": {
        "ar": ["صار خطأ بجلب معلومات المنشور ❌\n{error}"],
        "en": ["Failed to fetch post info ❌\n{error}"],
    },
    "quality_fetch_error": {
        "ar": ["ما گدرت اجيب معلومات الرابط ❌\n{error}"],
        "en": ["Couldn't fetch link info ❌\n{error}"],
    },
    "fallback_failed": {
        "ar": ["❌ فشلت المحاولة البديلة بعد.\n{error}"],
        "en": ["❌ The alternative method also failed.\n{error}"],
    },
}


def _migrate_legacy_messages() -> int:
    """يستبدل النصوص القديمة غير المعدلة بالجديدة. يرجع عدد الرسائل اللي تحدثت."""
    updated = 0
    for key, langs in LEGACY_MESSAGES.items():
        for lang, old_texts in langs.items():
            new_text = DEFAULT_MESSAGES.get(key, {}).get(lang)
            if not new_text:
                continue
            res = _db.messages.update_one(
                {"key": f"{key}_{lang}", "text": {"$in": old_texts}},
                {"$set": {"text": new_text}},
            )
            updated += res.modified_count
    return updated


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

    # نزرع الرسائل الافتراضية اذا مو موجودة (نسخة لكل لغة تحت مفتاح key_lang)
    for key, versions in DEFAULT_MESSAGES.items():
        for lang, text in versions.items():
            doc_key = f"{key}_{lang}"
            _db.messages.update_one(
                {"key": doc_key}, {"$setOnInsert": {"key": doc_key, "text": text}}, upsert=True
            )

    try:
        n = _migrate_legacy_messages()
        if n:
            logger.info("🔄 تم ترحيل %s رسالة قديمة للنسخة الجديدة", n)
    except Exception:
        logger.exception("legacy message migration failed")

    try:
        from . import wallet
        wallet.init_indexes()
    except Exception:
        logger.exception("failed to init wallet indexes")

    logger.info("✅ اتصال MongoDB جاهز")


def is_connected() -> bool:
    return _db is not None


# ---------- الرسائل ----------

def get_message(key: str, lang: str = "ar", **kwargs) -> str:
    """يجيب رسالة من القاعدة بلغة معينة (او الافتراضية اذا القاعدة غير متصلة) ويعبي المتغيرات.
    lang: 'ar' او 'en'. اذا اللغة غير مدعومة لهذا المفتاح، يرجع بالعربي كافتراضي."""
    if lang not in ("ar", "en"):
        lang = "ar"

    versions = DEFAULT_MESSAGES.get(key, {})
    text = versions.get(lang) or versions.get("ar", "")

    if is_connected():
        doc = _db.messages.find_one({"key": f"{key}_{lang}"})
        if doc and doc.get("text"):
            text = doc["text"]
        elif lang != "ar":
            # اذا مو موجودة نسخة بهذي اللغة بالقاعدة، نرجع للعربي كافتراضي
            doc_ar = _db.messages.find_one({"key": f"{key}_ar"})
            if doc_ar and doc_ar.get("text"):
                text = doc_ar["text"]

    try:
        return text.format(**kwargs) if kwargs else text
    except (KeyError, IndexError):
        return text


def set_message(key: str, text: str, lang: str = "ar") -> bool:
    if not is_connected():
        return False
    if lang not in ("ar", "en"):
        lang = "ar"
    _db.messages.update_one({"key": f"{key}_{lang}"}, {"$set": {"text": text}}, upsert=True)
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


def remove_sticker(key: str) -> bool:
    if not is_connected():
        return False
    _db.stickers.delete_one({"key": key})
    return True


# ---------- إعدادات عامة (مفاتيح/قيم بسيطة) ----------

def get_setting(key: str, default=False):
    if not is_connected():
        return default
    doc = _db.settings.find_one({"key": key})
    return doc.get("value", default) if doc else default


def set_setting(key: str, value):
    if not is_connected():
        return False
    _db.settings.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)
    return True


# ---------- تتبع الفشل المتكرر (لتنبيه الأدمن) ----------

def record_failure(platform: str) -> int:
    """يسجل فشل جديد لمنصة، يرجع عدد الفشل المتتالي الحالي."""
    if not is_connected():
        return 0
    doc = _db.failure_tracking.find_one_and_update(
        {"platform": platform},
        {"$inc": {"consecutive_failures": 1}},
        upsert=True,
        return_document=True,
    )
    return doc.get("consecutive_failures", 1) if doc else 1


def reset_failures(platform: str):
    if not is_connected():
        return
    _db.failure_tracking.update_one(
        {"platform": platform}, {"$set": {"consecutive_failures": 0}}, upsert=True
    )


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


def get_user_info(user_id: int) -> dict | None:
    if not is_connected():
        return None
    return _db.users.find_one({"user_id": user_id})


def get_user_link_stats(user_id: int) -> dict:
    """يرجع عدد الروابط اللي أرسلها مستخدم معين، مقسّمة حسب المنصة."""
    if not is_connected():
        return {"total": 0, "by_platform": {}}
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
    ]
    by_platform = {doc["_id"]: doc["count"] for doc in _db.links.aggregate(pipeline)}
    return {"total": sum(by_platform.values()), "by_platform": by_platform}


def get_top_users(limit: int = 10) -> list[dict]:
    """يرجع أكثر المستخدمين نشاطاً (أعلى عدد روابط)، كل وحدة فيها user_id, username, count."""
    if not is_connected():
        return []
    pipeline = [
        {"$group": {"_id": {"user_id": "$user_id", "username": "$username"}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    results = []
    for doc in _db.links.aggregate(pipeline):
        results.append({
            "user_id": doc["_id"]["user_id"],
            "username": doc["_id"].get("username") or "",
            "count": doc["count"],
        })
    return results


# ---------- تفضيلات شخصية للمستخدم ----------

def get_user_pref(user_id: int, key: str, default=None):
    """يرجع تفضيل شخصي للمستخدم (مثل show_post_info, verify_link). None يعني ما حدده - يستخدم الافتراضي العام."""
    if not is_connected():
        return default
    info = _db.users.find_one({"user_id": user_id}, {f"prefs.{key}": 1})
    if not info or "prefs" not in info:
        return default
    return info["prefs"].get(key, default)


def set_user_pref(user_id: int, key: str, value):
    if not is_connected():
        return False
    _db.users.update_one({"user_id": user_id}, {"$set": {f"prefs.{key}": value}}, upsert=True)
    return True


def get_user_language(user_id: int) -> str | None:
    """يرجع لغة المستخدم المحفوظة ('ar' او 'en')، او None اذا ما اختار لغة بعد."""
    return get_user_pref(user_id, "language", None)


def set_user_language(user_id: int, lang: str):
    if lang not in ("ar", "en"):
        lang = "ar"
    set_user_pref(user_id, "language", lang)


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


# ---------- استهلاك المحاولة البديلة الأسبوعي (عبر TikHub) ----------

def _current_week_key() -> str:
    """مفتاح الأسبوع الحالي بصيغة ISO (سنة-رقم أسبوع)، يستخدم لتصفير العداد تلقائياً كل أسبوع."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def get_fallback_usage(user_id: int, platform: str = "douyin") -> int:
    """يرجع عدد مرات استخدام المحاولة البديلة لهذا المستخدم بالأسبوع الحالي، لمنصة معينة."""
    if not is_connected():
        return 0
    week_key = _current_week_key()
    doc = _db.fallback_usage.find_one({"user_id": user_id, "platform": platform, "week": week_key})
    return doc.get("count", 0) if doc else 0


def increment_fallback_usage(user_id: int, platform: str = "douyin") -> int:
    """يزيد عداد استخدام المحاولة البديلة لهذا المستخدم بالأسبوع الحالي لمنصة معينة، يرجع العدد الجديد."""
    if not is_connected():
        return 0
    week_key = _current_week_key()
    doc = _db.fallback_usage.find_one_and_update(
        {"user_id": user_id, "platform": platform, "week": week_key},
        {"$inc": {"count": 1}},
        upsert=True,
        return_document=True,
    )
    return doc.get("count", 1) if doc else 1


# أسماء قديمة متوافقة - تبقى تشتغل بدون تغيير باقي الكود (تستخدم دويين تلقائياً)
def get_douyin_fallback_usage(user_id: int) -> int:
    return get_fallback_usage(user_id, "douyin")


def increment_douyin_fallback_usage(user_id: int) -> int:
    return increment_fallback_usage(user_id, "douyin")


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


def get_storage_stats() -> dict | None:
    """يجيب حجم استهلاك MongoDB الفعلي من القاعدة نفسها (بالميكابايت)، من اصل 512 ميكا بالخطة المجانية."""
    if not is_connected():
        return None
    try:
        stats = _db.command("dbStats")
        used_mb = stats.get("dataSize", 0) / (1024 * 1024)
        storage_mb = stats.get("storageSize", 0) / (1024 * 1024)
        index_mb = stats.get("indexSize", 0) / (1024 * 1024)
        total_mb = storage_mb + index_mb
        return {
            "data_mb": round(used_mb, 2),
            "total_mb": round(total_mb, 2),
            "free_tier_limit_mb": 512,
            "percent_used": round((total_mb / 512) * 100, 1),
        }
    except Exception:
        logger.exception("failed to fetch MongoDB storage stats")
        return None
