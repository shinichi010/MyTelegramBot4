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
PING_INTERVAL = int(os.environ.get("PING_INTERVAL", "600"))

# --- مجلد التحميل المؤقت ---
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- أقصى عدد خيارات جودة نعرضها لروابط اكس ---
MAX_QUALITY_OPTIONS = 5

# --- كوكيز دويين مشفرة base64 (اختياري - تحل مشكلة "Fresh cookies are needed") ---
DOUYIN_COOKIES_DATA = os.environ.get("DOUYIN_COOKIES_DATA", "")
DOUYIN_COOKIES_FILE = "/tmp/douyin_cookies.txt"

if DOUYIN_COOKIES_DATA:
    import base64
    try:
        with open(DOUYIN_COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(DOUYIN_COOKIES_DATA))
    except Exception as e:
        print(f"⚠️ فشل دويين: {e}")
        DOUYIN_COOKIES_FILE = None
else:
    DOUYIN_COOKIES_FILE = None
