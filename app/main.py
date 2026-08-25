from .keepalive import start_keepalive
from .bot import build_application


def main():
    start_keepalive()
    app = build_application()
    print("🤖 البوت شغال...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
