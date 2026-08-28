import os
import re
import uuid
import asyncio
import yt_dlp

from . import config

X_PATTERN = re.compile(r"(https?://)?(www\.)?(twitter\.com|x\.com)/\S+", re.IGNORECASE)
DOUYIN_PATTERN = re.compile(r"(https?://)?(www\.|v\.)?(douyin\.com|iesdouyin\.com)/\S+", re.IGNORECASE)


def detect_platform(text: str):
    """يرجع 'x' او 'douyin' او None حسب الرابط الموجود بالنص."""
    if X_PATTERN.search(text):
        return "x"
    if DOUYIN_PATTERN.search(text):
        return "douyin"
    return None


def extract_url(text: str, platform: str) -> str:
    pattern = X_PATTERN if platform == "x" else DOUYIN_PATTERN
    match = pattern.search(text)
    return match.group(0) if match else text.strip()


def _base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }


def _cookie_file_for(platform: str):
    if platform == "x":
        return config.X_COOKIES_FILE
    if platform == "douyin":
        return config.DOUYIN_COOKIES_FILE
    return None


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


async def list_x_qualities(url: str):
    """يستخرج خيارات جودة عامة (بالدقة) ومعلومات صاحب المنشور، ويدعم اكثر من فيديو بنفس الرابط."""

    def _extract():
        opts = _base_opts()
        if config.X_COOKIES_FILE:
            opts["cookiefile"] = config.X_COOKIES_FILE
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.to_thread(_extract)
    entries = _entries_of(info)
    meta = extract_meta(entries[0])

    # نجمع كل الدقات المتوفرة عبر كل الفيديوهات بالمنشور (اتحاد الدقات)
    heights = set()
    for entry in entries:
        for f in (entry.get("formats") or []):
            if f.get("vcodec") not in (None, "none") and f.get("height"):
                heights.add(f["height"])

    sorted_heights = sorted(heights, reverse=True)[: config.MAX_QUALITY_OPTIONS]
    if not sorted_heights:
        sorted_heights = [0]  # يعني "أفضل جودة متوفرة" بدون تحديد دقة

    return meta, sorted_heights, len(entries)


async def download_x(url: str, height: int) -> tuple[list[str], dict]:
    """يحمل كل فيديوهات المنشور (وحدة او اكثر) بأقرب دقة ممكنة للدقة المختارة."""
    out_template = os.path.join(
        config.DOWNLOAD_DIR, f"{uuid.uuid4()}_%(playlist_index)s.%(ext)s"
    )

    if height and height > 0:
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/best"
    else:
        fmt = "bv*+ba/best"

    def _download():
        opts = _base_opts()
        opts.update({
            "format": fmt,
            "outtmpl": out_template,
            "merge_output_format": "mp4",
        })
        if config.X_COOKIES_FILE:
            opts["cookiefile"] = config.X_COOKIES_FILE
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = _entries_of(info)
            meta = extract_meta(entries[0])
            files = [ydl.prepare_filename(e) for e in entries]
            return files, meta

    files, meta = await asyncio.to_thread(_download)
    return _fix_extensions(files), meta


async def download_douyin(url: str) -> tuple[list[str], dict]:
    """يحمل فيديو دوين بأعلى جودة، او كل صور المنشور اذا كان سلايدشو، مع معلومات صاحب المنشور."""
    out_template = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_%(playlist_index)s.%(ext)s")

    def _download():
        opts = _base_opts()
        opts.update({
            "format": "bestvideo+bestaudio/best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "writethumbnail": False,
        })
        if config.DOUYIN_COOKIES_FILE:
            opts["cookiefile"] = config.DOUYIN_COOKIES_FILE
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = _entries_of(info)
            meta = extract_meta(entries[0])
            files = [ydl.prepare_filename(e) for e in entries]
            return files, meta

    files, meta = await asyncio.to_thread(_download)
    return _fix_extensions(files), meta


async def download_audio(url: str, platform: str) -> tuple[list[str], dict]:
    """يحمل الصوت بس (MP3) من رابط X او دويين، لكل الفيديوهات بالمنشور اذا اكثر من وحدة."""
    out_template = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_%(playlist_index)s.%(ext)s")

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
        cookie_file = _cookie_file_for(platform)
        if cookie_file:
            opts["cookiefile"] = cookie_file
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

    files, meta = await asyncio.to_thread(_download)
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
        cookie_file = _cookie_file_for(platform)
        if cookie_file:
            opts["cookiefile"] = cookie_file
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=False)
            return True
        except Exception:
            return False

    return await asyncio.to_thread(_check)


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
