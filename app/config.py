import os

# --- توكن البوت من BotFather ---
BOT_TOKEN = os.environ["BOT_TOKEN"]

# --- سيرفر Local Bot API (شغال محلياً جوه نفس الحاوية) ---
LOCAL_API_HOST = os.environ.get("LOCAL_API_HOST", "127.0.0.1")
LOCAL_API_PORT = os.environ.get("LOCAL_API_PORT", "8081")
LOCAL_API_URL = f"http://{LOCAL_API_HOST}:{LOCAL_API_PORT}"
BASE_URL = f"{LOCAL_API_URL}/bot"
BASE_FILE_URL = f"{LOCAL_API_URL}/file/bot"

# --- منفذ الويب المطلوب من Render (متغير PORT يجيهه رندر تلقائياً) ---
PORT = int(os.environ.get("PORT", "10000"))

# --- رابط الخدمة العلني على Render (لعمل بينك ذاتي يمنع السيرفر يوكف) ---
# مثال: https://your-service-name.onrender.com
EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

# --- فترة البينك الذاتي بالثواني (افتراضي كل 10 دقايق) ---
PING_INTERVAL = int(os.environ.get("PING_INTERVAL", "840"))

# --- مجلد التحميل المؤقت ---
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- أقصى عدد خيارات جودة نعرضها لروابط اكس ---
MAX_QUALITY_OPTIONS = 5

# --- أقصى دقة (بكسل ارتفاع) نسمح بتحميلها/دمجها - سقف حماية ذاكرة ضد فيديوهات ضخمة (4K وأعلى) ---
MAX_QUALITY_HEIGHT = int(os.environ.get("MAX_QUALITY_HEIGHT", "1080"))

# --- آيدي التليگرام الرقمي حقك، يوصلك عليه إشعار كل مستخدم جديد يستخدم البوت ---
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

# --- رابط اتصال MongoDB (يحفظ فقط بيانات نصية: مستخدمين، رسائل، روابط، حظر - ابداً ملفات) ---
MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "x_douyin_bot")

# --- أقصى حجم ملف مسموح تحميله (ميكابايت) ---
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "250"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# --- الحجم اللي فوقه يعتبر تحميل "ثقيل" ويفعل نظام الطابور (ميكابايت) ---
HEAVY_FILE_THRESHOLD_MB = int(os.environ.get("HEAVY_FILE_THRESHOLD_MB", "150"))
HEAVY_FILE_THRESHOLD_BYTES = HEAVY_FILE_THRESHOLD_MB * 1024 * 1024

# --- مفتاح TikHub API (اختياري - يفعل تحميل ويشات وعرض استهلاك الطلبات بالإحصائيات) ---
TIKHUB_API_KEY = os.environ.get("TIKHUB_API_KEY", "")

# --- رابط خدمة فك تشفير فيديوهات ويشات (خدمة Render ثانية منفصلة، Docker image جاهز) ---
WECHAT_DECRYPT_API_URL = os.environ.get("WECHAT_DECRYPT_API_URL", "")

# --- رابط لوحة Render (لعرضه بالإحصائيات، لأن استهلاك RAM/المساحة ما يوصل له البوت برمجياً) ---
RENDER_DASHBOARD_URL = os.environ.get("RENDER_DASHBOARD_URL", "")

# --- كوكيز دويين مشفرة base64 (اختياري - تحل مشكلة "Fresh cookies are needed") ---
DOUYIN_COOKIES_DATA = os.environ.get("DOUYIN_COOKIES_DATA", "")
DOUYIN_COOKIES_FILE = "/tmp/douyin_cookies.txt"

if DOUYIN_COOKIES_DATA:
    import base64
    try:
        with open(DOUYIN_COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(DOUYIN_COOKIES_DATA))
    except Exception as e:
        print(f"⚠️ فشل فك تشفير كوكيز دويين: {e}")
        DOUYIN_COOKIES_FILE = None
else:
    DOUYIN_COOKIES_FILE = None

# --- كوكيز X مشفرة base64 (اختياري - تحل مشاكل "Video is unavailable" مع حسابات حقيقية) ---
X_COOKIES_DATA = os.environ.get("X_COOKIES_DATA", "")
X_COOKIES_FILE = "/tmp/x_cookies.txt"

if X_COOKIES_DATA:
    import base64
    try:
        with open(X_COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(X_COOKIES_DATA))
    except Exception as e:
        print(f"⚠️ فشل فك تشفير كوكيز X: {e}")
        X_COOKIES_FILE = None
else:
    X_COOKIES_FILE = None

# --- كوكيز RedNote/Xiaohongshu مشفرة base64 (اختياري - قد تحتاجها بعض الروابط) ---
REDNOTE_COOKIES_DATA = os.environ.get("REDNOTE_COOKIES_DATA", "")
REDNOTE_COOKIES_FILE = "/tmp/rednote_cookies.txt"

if REDNOTE_COOKIES_DATA:
    import base64
    try:
        with open(REDNOTE_COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(REDNOTE_COOKIES_DATA))
    except Exception as e:
        print(f"⚠️ فشل فك تشفير كوكيز RedNote: {e}")
        REDNOTE_COOKIES_FILE = None
else:
    REDNOTE_COOKIES_FILE = None
