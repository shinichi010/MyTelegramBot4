import os
import re
import uuid
import asyncio
import yt_dlp

from . import config

X_PATTERN = re.compile(r"(https?://)?(www\.)?(twitter\.com|x\.com)/\S+", re.IGNORECASE)
DOUYIN_PATTERN = re.compile(r"(https?://)?(www\.|v\.)?(douyin\.com|iesdouyin\.com)/\S+", re.IGNORECASE)
REDNOTE_PATTERN = re.compile(
    r"(https?://)?(www\.)?(xiaohongshu\.com|rednote\.com|xhslink\.com)/\S+", re.IGNORECASE
)
BILIBILI_PATTERN = re.compile(
    r"(https?://)?(www\.)?(bilibili\.com|b23\.tv)/\S+", re.IGNORECASE
)

_PATTERNS = {
    "x": X_PATTERN,
    "douyin": DOUYIN_PATTERN,
    "rednote": REDNOTE_PATTERN,
    "bilibili": BILIBILI_PATTERN,
}

# المنصات اللي تعرض قائمة جودات للاختيار قبل التحميل. الباقي (دويين، ويشات) يتنزل
# تلقائياً بأعلى جودة متوفرة بدون قائمة اختيار.
QUALITY_CHOICE_PLATFORMS = {"x", "rednote", "bilibili"}


def detect_platform(text: str):
    """يرجع اسم المنصة او None حسب الرابط الموجود بالنص."""
    for platform, pattern in _PATTERNS.items():
        if pattern.search(text):
            return platform
    return None


def extract_url(text: str, platform: str) -> str:
    pattern = _PATTERNS.get(platform, X_PATTERN)
    match = pattern.search(text)
    return match.group(0) if match else text.strip()


def _base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
    }


def _cookie_file_for(platform: str):
    if platform == "x":
        return config.X_COOKIES_FILE
    if platform == "douyin":
        return config.DOUYIN_COOKIES_FILE
    if platform == "rednote":
        return config.REDNOTE_COOKIES_FILE
    return None


def _platform_opts(platform: str) -> dict:
    """خيارات إضافية خاصة بمنصة معينة (كوكيز، هيدرز خاصة تتطلبها بعض المواقع)."""
    opts = {}
    cookie_file = _cookie_file_for(platform)
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if platform == "bilibili":
        # Bilibili أحياناً يرفض الطلب بدون هذول (خطأ 412 Precondition Failed)
        opts["http_headers"] = {
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
    return opts


def _entries_of(info: dict) -> list[dict]:
    """يرجع كل الفيديوهات/العناصر بمنشور واحد (thread/gallery) كلستة."""
    if info.get("entries"):
        return [e for e in info["entries"] if e]
    return [info]


def extract_meta(info: dict) -> dict:
    """يستخرج معلومات صاحب المنشور والوصف من كائن معلومات yt-dlp."""
    return {
        "uploader": info.get("uploader") or info.get("channel") or "",
        "uploader_id": info.get("uploader_id") or info.get("channel_id") or "",
        "description": (info.get("description") or info.get("title") or "").strip(),
        "webpage_url": info.get("webpage_url") or "",
    }


async def list_qualities(url: str, platform: str = "x"):
    """يستخرج خيارات جودة عامة (بالدقة + الحجم التقريبي) ومعلومات صاحب المنشور،
    ويدعم اكثر من فيديو بنفس الرابط. مستخدمة حالياً لـ X بس (باقي المنصات تنزل تلقائياً)."""

    def _extract():
        opts = _base_opts()
        opts.update(_platform_opts(platform))
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.to_thread(_extract)
    entries = _entries_of(info)
    meta = extract_meta(entries[0])

    # نجمع كل الدقات المتوفرة، ونحسب أفضل تقدير حجم لكل دقة (فيديو + افضل صوت متوفر)
    formats = entries[0].get("formats") or []
    best_audio_size = 0
    for f in formats:
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none"):
            size = f.get("filesize") or f.get("filesize_approx") or 0
            if size > best_audio_size:
                best_audio_size = size

    by_height = {}  # height -> (video_size, has_own_audio)
    for f in formats:
        if f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        h = f["height"]
        if h > config.MAX_QUALITY_HEIGHT:
            continue  # سقف أقصى للجودة يمنع دمج فيديوهات ضخمة (4K وأعلى) تستهلك ذاكرة زايدة
        v_size = f.get("filesize") or f.get("filesize_approx") or 0
        has_audio = f.get("acodec") not in (None, "none")
        prev = by_height.get(h)
        if prev is None or v_size > prev[0]:
            by_height[h] = (v_size, has_audio)

    quality_options = []  # [(height, total_size_bytes_or_None)]
    for h, (v_size, has_audio) in sorted(by_height.items(), key=lambda x: -x[0]):
        if v_size == 0:
            total = None  # ما نعرف الحجم
        elif has_audio:
            total = v_size
        else:
            total = v_size + best_audio_size
        quality_options.append((h, total))

    quality_options = quality_options[: config.MAX_QUALITY_OPTIONS]
    if not quality_options:
        quality_options = [(0, None)]  # يعني "أفضل جودة متوفرة" بدون تحديد دقة

    return meta, quality_options, len(entries)


# اسم قديم متوافق - يبقى يشتغل بدون تغيير باقي الكود
async def list_x_qualities(url: str):
    return await list_qualities(url, "x")


async def download_video(url: str, platform: str, height: int = 0) -> tuple[list[str], dict]:
    """يحمل كل فيديوهات/صور المنشور (وحدة او اكثر) بأقرب دقة ممكنة للدقة المختارة
    (height=0 يعني أفضل جودة متوفرة تلقائياً - مستخدم لكل المنصات غير X)."""
    prefix = str(uuid.uuid4())
    out_template = os.path.join(config.DOWNLOAD_DIR, f"{prefix}_%(playlist_index)s.%(ext)s")

    # نطبق سقف الدقة القصوى حتى بمسار "أفضل جودة متوفرة" - يمنع دمج فيديوهات 4K وأعلى
    effective_height = height if (height and height > 0) else config.MAX_QUALITY_HEIGHT
    fmt = f"bv*[height<={effective_height}]+ba/b[height<={effective_height}]/best"

    def _download():
        opts = _base_opts()
        opts.update({
            "format": fmt,
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "writethumbnail": False,
        })
        opts.update(_platform_opts(platform))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = _entries_of(info)
            meta = extract_meta(entries[0])
            files = [ydl.prepare_filename(e) for e in entries]
            return files, meta

    try:
        files, meta = await asyncio.to_thread(_download)
    except Exception:
        cleanup_by_prefix(prefix)  # ننظف اي ملفات جزئية/مؤقتة تركها الفشل (خصوصاً اثناء الدمج)
        raise
    return _fix_extensions(files), meta


# أسماء قديمة متوافقة - تبقى تشتغل بدون تغيير باقي الكود
async def download_x(url: str, height: int) -> tuple[list[str], dict]:
    return await download_video(url, "x", height)


async def download_douyin(url: str) -> tuple[list[str], dict]:
    return await download_video(url, "douyin", 0)


async def download_audio(url: str, platform: str) -> tuple[list[str], dict]:
    """يحمل الصوت بس (MP3) من اي منصة مدعومة، لكل الفيديوهات بالمنشور اذا اكثر من وحدة."""
    prefix = str(uuid.uuid4())
    out_template = os.path.join(config.DOWNLOAD_DIR, f"{prefix}_%(playlist_index)s.%(ext)s")

    def _download():
        opts = _base_opts()
        opts.update({
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
        opts.update(_platform_opts(platform))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = _entries_of(info)
            meta = extract_meta(entries[0])
            # بعد التحويل لـ mp3 الامتداد يتغير، نبني الاسم المتوقع يدوياً
            files = []
            for e in entries:
                raw_name = ydl.prepare_filename(e)
                base, _ = os.path.splitext(raw_name)
                files.append(base + ".mp3")
            return files, meta

    try:
        files, meta = await asyncio.to_thread(_download)
    except Exception:
        cleanup_by_prefix(prefix)
        raise
    files = [f for f in files if os.path.exists(f)]

    # اسم عرض للملف مبني على اسم صاحب الحساب (نظافة الاسم من رموز ممنوعة بأسماء الملفات)
    uploader_name = meta.get("uploader") or meta.get("uploader_id") or "audio"
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", uploader_name).strip() or "audio"
    meta["audio_display_name"] = safe_name

    return files, meta


async def verify_link(url: str, platform: str) -> bool:
    """يتحقق ان الرابط شغال وقابل للوصول قبل لا نبدأ تحميل فعلي (بدون تحميل فعلي للملف)."""

    def _check():
        opts = _base_opts()
        opts.update(_platform_opts(platform))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=False)
            return True
        except Exception:
            return False

    return await asyncio.to_thread(_check)


async def get_preview(url: str, platform: str) -> dict | None:
    """يجيب صورة مصغرة (thumbnail) ومدة الفيديو بدون تحميل فعلي، لعرض معاينة سريعة."""

    def _fetch():
        opts = _base_opts()
        opts.update(_platform_opts(platform))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            return None
        entries = _entries_of(info)
        entry = entries[0]
        return {
            "thumbnail": entry.get("thumbnail"),
            "duration": entry.get("duration"),  # بالثواني
            "title": entry.get("title") or "",
        }

    return await asyncio.to_thread(_fetch)


def _fix_extensions(files: list[str]) -> list[str]:
    fixed = []
    for f in files:
        if os.path.exists(f):
            fixed.append(f)
        else:
            base, _ = os.path.splitext(f)
            for ext in (".mp4", ".jpg", ".jpeg", ".png", ".webp"):
                if os.path.exists(base + ext):
                    fixed.append(base + ext)
                    break
    return fixed


def cleanup(paths: list[str]):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def cleanup_by_prefix(prefix: str):
    """ينظف أي ملفات مؤقتة (كاملة او جزئية - .part, .ytdl, إلخ) تركها تحميل فاشل،
    بالاعتماد على بادئة uuid الفريدة لهذا التحميل."""
    try:
        for fname in os.listdir(config.DOWNLOAD_DIR):
            if fname.startswith(prefix):
                try:
                    os.remove(os.path.join(config.DOWNLOAD_DIR, fname))
                except OSError:
                    pass
    except OSError:
        pass
