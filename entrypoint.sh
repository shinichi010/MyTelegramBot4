#!/bin/bash
set -e

if [ -z "$TG_API_ID" ] || [ -z "$TG_API_HASH" ]; then
    echo "❌ لازم تحدد TG_API_ID و TG_API_HASH (احصل عليهم من my.telegram.org)"
    exit 1
fi

echo "🚀 تشغيل Local Bot API Server..."
telegram-bot-api \
    --api-id="$TG_API_ID" \
    --api-hash="$TG_API_HASH" \
    --http-port="${LOCAL_API_PORT:-8081}" \
    --local \
    --dir=/srv/tg-api-data \
    --log=/srv/tg-api.log &

# ننطر شوي حتى سيرفر البوت إي بي يصير جاهز يستقبل طلبات
sleep 5

echo "🚀 تشغيل بوت تليگرام..."
exec python -m app.main
