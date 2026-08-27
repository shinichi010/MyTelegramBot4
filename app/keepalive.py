import time
import logging
import threading

import requests
from flask import Flask

from . import config

logger = logging.getLogger("keepalive")

flask_app = Flask(__name__)


@flask_app.get("/")
def health():
    return "bot is alive", 200


def _run_flask():
    # host 0.0.0.0 مطلوب حتى رندر يوصل للمنفذ
    flask_app.run(host="0.0.0.0", port=config.PORT)


def _ping_loop():
    if not config.EXTERNAL_URL:
        logger.warning(
            "RENDER_EXTERNAL_URL مو محدد - البينك الذاتي متوقف. "
            "أضف هذا المتغير او استخدم UptimeRobot يدوياً."
        )
        return

    time.sleep(30)  # ننطر السيرفر يشتغل اول شي
    while True:
        try:
            requests.get(config.EXTERNAL_URL, timeout=15)
            logger.info("keep-alive ping sent")
        except Exception as e:
            logger.warning(f"keep-alive ping failed: {e}")

        # نصحّي بعد خدمة فك تشفير ويشات (اذا مفعّلة) بنفس الدورة
        if config.WECHAT_DECRYPT_API_URL:
            try:
                requests.get(config.WECHAT_DECRYPT_API_URL, timeout=15)
                logger.info("wechat-decrypt-api keep-alive ping sent")
            except Exception as e:
                logger.warning(f"wechat-decrypt-api keep-alive ping failed: {e}")

        time.sleep(config.PING_INTERVAL)


def start_keepalive():
    threading.Thread(target=_run_flask, daemon=True).start()
    threading.Thread(target=_ping_loop, daemon=True).start()
