from .keepalive import start_keepalive
from .bot import build_application
from . import db


def main():
    start_keepalive()

    try:
        db.init()
    except Exception as e:
        print(f"⚠️ فشل الاتصال بقاعدة البيانات: {e}")
        print("البوت راح يشتغل بالرسائل الافتراضية بدون حفظ إحصائيات/روابط.")

    app = build_application()
    print("🤖 البوت شغال...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
