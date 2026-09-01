import time
import logging
import threading

import requests
from flask import Flask

from . import config, db

logger = logging.getLogger("keepalive")

flask_app = Flask(__name__)


@flask_app.get("/")
def health():
    return "bot is alive", 200


def _run_flask():
    # host 0.0.0.0 مطلوب حتى رندر يوصل للمنفذ
    flask_app.run(host="0.0.0.0", port=config.PORT)


def _main_ping_loop():
    """بينك ذاتي للبوت الرئيسي - الفاصل الزمني قابل للتعديل من /admin."""
    if not config.EXTERNAL_URL:
        logger.warning(
            "RENDER_EXTERNAL_URL مو محدد - البينك الذاتي للبوت الرئيسي متوقف. "
            "أضف هذا المتغير او استخدم UptimeRobot يدوياً."
        )
        return

    time.sleep(30)  # ننطر السيرفر يشتغل اول شي
    while True:
        try:
            requests.get(config.EXTERNAL_URL, timeout=15)
            logger.info("main bot keep-alive ping sent")
        except Exception as e:
            logger.warning(f"main bot keep-alive ping failed: {e}")

        interval_min = db.get_setting("main_ping_interval_min", config.PING_INTERVAL // 60)
        time.sleep(max(int(interval_min), 1) * 60)


def _wechat_ping_loop():
    """بينك ذاتي منفصل لخدمة فك تشفير ويشات - فاصل زمني وتفعيل/إيقاف مستقلين تماماً."""
    if not config.WECHAT_DECRYPT_API_URL:
        return

    time.sleep(45)  # نبدأ بعد البوت الرئيسي بشوي حتى ما يتزاحمون بأول تشغيل
    while True:
        enabled = db.get_setting("wechat_ping_enabled", True)
        if enabled:
            try:
                requests.get(config.WECHAT_DECRYPT_API_URL, timeout=15)
                logger.info("wechat-decrypt-api keep-alive ping sent")
            except Exception as e:
                logger.warning(f"wechat-decrypt-api keep-alive ping failed: {e}")
        else:
            logger.info("wechat-decrypt-api keep-alive موقف من /admin - تخطينا هذي الدورة")

        interval_min = db.get_setting("wechat_ping_interval_min", 10)
        time.sleep(max(int(interval_min), 1) * 60)


def start_keepalive():
    threading.Thread(target=_run_flask, daemon=True).start()
    threading.Thread(target=_main_ping_loop, daemon=True).start()
    threading.Thread(target=_wechat_ping_loop, daemon=True).start()
