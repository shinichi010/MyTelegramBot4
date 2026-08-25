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


async def list_x_qualities(url: str):
    """يستخرج خيارات الجودة المتوفرة لفيديو من اكس/تويتر."""

    def _extract():
        opts = _base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info

    info = await asyncio.to_thread(_extract)

    # بعض المنشورات تحتوي اكثر من فيديو (thread/gallery) - ناخذ الأول حالياً
    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    formats = info.get("formats") or []
    video_formats = [
        f for f in formats
        if f.get("vcodec") not in (None, "none") and f.get("height")
    ]

    # نلغي التكرار حسب الدقة، ونحتفظ بأعلى بت-ريت لكل دقة
    best_by_height = {}
    for f in video_formats:
        h = f["height"]
        if h not in best_by_height or (f.get("tbr") or 0) > (best_by_height[h].get("tbr") or 0):
            best_by_height[h] = f

    sorted_formats = sorted(best_by_height.values(), key=lambda f: f["height"], reverse=True)
    options = sorted_formats[: config.MAX_QUALITY_OPTIONS]

    if not options and formats:
        # اذا ما گدرنا نميز الدقة، نرجع افضل فورمات موجود بس
        options = [formats[-1]]

    return info, options


async def download_x(url: str, format_id: str) -> list[str]:
    out_template = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}.%(ext)s")

    def _download():
        opts = _base_opts()
        opts.update({
            "format": f"{format_id}+bestaudio/best" if format_id != "best" else "best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

    path = await asyncio.to_thread(_download)
    # merge_output_format ممكن يغير الامتداد، نتأكد الملف موجود
    if not os.path.exists(path):
        base, _ = os.path.splitext(path)
        candidate = base + ".mp4"
        if os.path.exists(candidate):
            path = candidate
    return [path]


async def download_douyin(url: str) -> list[str]:
    """يحمل فيديو دوين بأعلى جودة، او كل صور المنشور اذا كان سلايدشو."""
    out_template = os.path.join(config.DOWNLOAD_DIR, f"{uuid.uuid4()}_%(playlist_index)s.%(ext)s")

    def _download():
        opts = _base_opts()
        opts.update({
            "format": "bestvideo+bestaudio/best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "writethumbnail": False,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            files = []
            if "entries" in info and info["entries"]:
                for entry in info["entries"]:
                    if entry:
                        files.append(ydl.prepare_filename(entry))
            else:
                files.append(ydl.prepare_filename(info))
            return files

    files = await asyncio.to_thread(_download)
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
