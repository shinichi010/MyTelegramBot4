# بوت تحميل X (تويتر) ودويين

بوت تليگرام يحمل فيديوهات من X ودويين، مبني على `python-telegram-bot` + `yt-dlp`، وشغال مع
**Local Bot API Server** حتى يرفع حد رفع الملفات من 50 ميكا الافتراضي الى 2000 ميكا.

## المميزات
- روابط **X**: تعرض خيارات جودة (عدة دقات) يختار المستخدم منها.
- روابط **دويين**: تحميل تلقائي بأعلى جودة متوفرة (فيديو او صور سلايدشو).
- رفع ملفات لين 2000 ميكا عبر سيرفر Bot API محلي (بدل حد 50 ميكا الافتراضي).
- سيرفر ويب صغير + بينك ذاتي حتى ميوكف على خطة Render المجانية.

## 1) قبل ما تنشر: جيب المتطلبات
1. **BOT_TOKEN**: سوي بوت جديد عن طريق [@BotFather](https://t.me/BotFather).
2. **TG_API_ID و TG_API_HASH**: سجل دخول بـ [my.telegram.org](https://my.telegram.org) →
   API Development Tools → أنشئ تطبيق وخذ القيمتين.

## 2) النشر على Render (Docker)
1. ارفع هذا المجلد كامل الى مستودع GitHub.
2. بـ Render: **New → Web Service** → اختار المستودع.
3. Render راح يتعرف على `Dockerfile` تلقائياً (Environment: **Docker**).
4. أضف متغيرات البيئة التالية من تبويب Environment:
   - `BOT_TOKEN`
   - `TG_API_ID`
   - `TG_API_HASH`
   - `RENDER_EXTERNAL_URL` = رابط الخدمة نفسها (تحصل عليه بعد أول Deploy، شكله
     `https://اسم-الخدمة.onrender.com` - أضفه وسوي Deploy مرة ثانية).
5. اختار الخطة **Free**، واعمل Deploy.

> ⚠️ خطة Render المجانية عندها RAM محدودة (512MB). سيرفر Local Bot API + بايثون + yt-dlp
> خفاف بالخمول، بس فيديوهات كبيرة جداً ممكن تاخذ وقت اطول بالتحميل والرفع.

## 3) منع السيرفر من الوكوف (Sleep)
خطة Render المجانية توكف الخدمة بعد ~15 دقيقة بدون طلبات HTTP. البوت فيه حل مدمج:
- سيرفر Flask صغير يرد على أي طلب GET بمنفذ `$PORT` (Render نفسه يحتاجه لأي Web Service).
- خيط (thread) خلفي يسوي بينك ذاتي لـ `RENDER_EXTERNAL_URL` كل 10 دقايق (قابل للتعديل
  عبر `PING_INTERVAL`).

**كخط دفاع ثاني موصى فيه**: سوي حساب مجاني بـ [UptimeRobot](https://uptimerobot.com) وضيف
مونيتور HTTP يضرب رابط خدمتك كل 5 دقايق. هذا يضمن انو حتى لو البينك الذاتي فشل لأي سبب
(مثلاً السيرفر اعاد التشغيل)، اكو مصدر خارجي يحافظ عليه صاحي.

## البنية
```
x_douyin_bot/
├── Dockerfile          # يجهز ffmpeg + ثنائي telegram-bot-api + بايثون
├── entrypoint.sh        # يشغل سيرفر Bot API المحلي ثم البوت
├── requirements.txt
├── .env.example
└── app/
    ├── config.py        # قراءة متغيرات البيئة
    ├── downloader.py     # منطق yt-dlp لـ X ودويين
    ├── bot.py            # handlers البوت
    ├── keepalive.py      # سيرفر Flask + بينك ذاتي
    └── main.py           # نقطة التشغيل
```

## ملاحظات
- يدعم فقط روابط `twitter.com` / `x.com` و `douyin.com` / `iesdouyin.com` / `v.douyin.com`
  — أي رابط ثاني يتم رفضه برسالة واضحة.
- منشورات X اللي فيها اكثر من فيديو (thread/gallery) حالياً ياخذ اول فيديو بس؛ اذا تحتاج
  دعم لكل الفيديوهات بمنشور واحد، اخبرني اضيفها.
- الملفات المحملة تنحذف من السيرفر مباشرة بعد الإرسال (تنظيف تلقائي بـ `downloader.cleanup`).
