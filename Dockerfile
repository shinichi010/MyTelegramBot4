FROM python:3.11-slim

# ffmpeg لازم يدمج الفيديو والصوت (yt-dlp)، curl لتنزيل ثنائي telegram-bot-api
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# تنزيل ثنائي Local Bot API Server الجاهز (linux x86_64)
RUN curl -L -o /usr/local/bin/telegram-bot-api \
    https://github.com/jakbin/telegram-bot-api-binary/releases/download/latest/telegram-bot-api \
    && chmod +x /usr/local/bin/telegram-bot-api

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 10000

ENTRYPOINT ["./entrypoint.sh"]
