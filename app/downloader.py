import os
import re
import sys
import time
import uuid
import asyncio
import logging
import traceback
import yt_dlp

from . import config, alipay, douyin_web

X_PATTERN = re.compile(r"(https?://)?(www\.)?(twitter\.com|x\.com)/\S+", re.IGNORECASE)
DOUYIN_PATTERN = re.compile(r"(https?://)?(www\.|v\.)?(douyin\.com|iesdouyin\.com)/\S+", re.IGNORECASE)
REDNOTE_PATTERN = re.compile(
    r"(https?://)?(www\.)?(xiaohongshu\.com|rednote\.com|xhslink\.com)/\S+", re.IGNORECASE
)
BILIBILI_PATTERN = re.compile(
    r"(https?://)?(www\.)?(bilibili\.com|b23\.tv)/\S+", re.IGNORECASE
)

ALIPAY_PATTERN = alipay.ALIPAY_PATTERN

_PATTERNS = {
    "alipay": ALIPAY_PATTERN,
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
        # مهلة الاتصال/القراءة لكل عملية شبكية منفردة (اتصال او جزء "fragment" واحد من
        # الفيديو). رفعناها من 60 إلى 180 ثانية لأن الملفات الكبيرة (80+ ميكا) كانت تفشل
        # بـ "Timed out" لو جزء واحد تأخر مؤقتاً بسبب ازدحام الخادم، حتى لو باقي الأجزاء
        # نزلت بسرعة عادية - وهذا بالضبط سبب نجاح إعادة المحاولة اليدوية مباشرة بعد الفشل.
        "socket_timeout": 180,
        # عدد محاولات إعادة الاتصال قبل الاستسلام كلياً (رفعناها من 5 إلى 10)
        "retries": 10,
        "fragment_retries": 10,
        # فاصل انتظار متزايد بين المحاولات (بدل إعادة المحاولة فوراً على نفس الظرف السيئ) -
        # يعطي وقت للازدحام المؤقت بالشبكة/الخادم يتحسن قبل المحاولة التالية.
        "retry_sleep_functions": {
            "http": lambda n: min(4 * (n + 1), 15),
            "fragment": lambda n: min(3 * (n + 1), 10),
        },
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


# ==================== DEBUG مؤقت: مشكلة دويين / yt-dlp ====================
# يطبع تشخيص آمن بـ Render Logs (بس لما platform == "douyin") بدون أي تغيير بمنطق التحميل.
# ما يطبع أبداً: قيم الكوكيز، اسماء الكوكيز، توكن البوت، مفاتيح API، اي headers حساسة.
# بعد معرفة السبب احذف هذا القسم + الاسطر اللي تبدأ بـ _douyin_debug_ بالدوال تحت.
_dbg_logger = logging.getLogger("douyin_debug")
_DBG = "[DOUYIN-DEBUG]"
_DBG_SAFE_OPT_KEYS = (
    "cookiefile", "cookiesfrombrowser", "format", "socket_timeout", "retries",
    "noplaylist", "nocheckcertificate", "merge_output_format",
)


def _douyin_debug_scrub(text: str) -> str:
    """حماية اضافية: يخفي اي سر معروف للبوت لو ظهر بالغلط بنص الخطأ."""
    for name in ("BOT_TOKEN", "TIKHUB_API_KEY", "MONGO_URI"):
        secret = getattr(config, name, "") or ""
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


def _douyin_debug_cookie_file(path) -> str:
    """وصف هيكلي لملف الكوكيز (أرقام وحالات بس - ما يطبع محتوى ولا اسماء ولا قيم)."""
    if not path:
        return "cookiefile=None (yt-dlp will run WITHOUT cookies)"
    if not os.path.isfile(path):
        return f"cookiefile path={path} exists=False"
    size = os.path.getsize(path)
    header_ok = False
    rows = douyin_rows = expired = session = bad_rows = 0
    now = int(time.time())
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f):
            line = raw.rstrip("\r\n")
            if i == 0:
                header_ok = re.match(r"#( Netscape)? HTTP Cookie File", line) is not None
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                bad_rows += 1
                continue
            rows += 1
            if "douyin" in parts[0].lower():
                douyin_rows += 1
            try:
                exp = int(parts[4])
            except ValueError:
                bad_rows += 1
                continue
            if exp == 0:
                session += 1
            elif exp < now:
                expired += 1
    return (
        f"cookiefile path={path} exists=True size_bytes={size} "
        f"netscape_header_ok={header_ok} cookie_rows={rows} douyin_domain_rows={douyin_rows} "
        f"expired_rows={expired} session_rows={session} malformed_rows={bad_rows}"
    )


def _douyin_debug_start(stage: str, platform: str, url: str, opts: dict):
    """يطبع حالة yt-dlp/الكوكيز/الخيارات قبل لا يبدأ الاستخراج. ما يرفع اي خطأ ابداً."""
    if platform != "douyin":
        return
    try:
        try:
            from yt_dlp.version import __version__ as ytdlp_version
        except Exception:
            ytdlp_version = "unknown"
        try:
            from yt_dlp.version import CHANNEL as ytdlp_channel
        except Exception:
            ytdlp_channel = "unknown"
        try:
            from yt_dlp.utils.networking import std_headers
        except Exception:
            from yt_dlp.utils import std_headers
        headers = {**std_headers, **(opts.get("http_headers") or {})}
        safe_opts = {k: opts.get(k) for k in _DBG_SAFE_OPT_KEYS if k in opts}
        safe_opts["proxy_set"] = bool(opts.get("proxy"))
        safe_opts["http_headers_override_names"] = sorted((opts.get("http_headers") or {}).keys())
        env_data = getattr(config, "DOUYIN_COOKIES_DATA", "") or ""
        lines = [
            f"{_DBG} stage={stage} yt-dlp starting extraction url={url}",
            f"{_DBG} yt-dlp version={ytdlp_version} channel={ytdlp_channel} python={sys.version.split()[0]}",
            f"{_DBG} env DOUYIN_COOKIES_DATA set={bool(env_data)} length_chars={len(env_data)} "
            f"config.DOUYIN_COOKIES_FILE={'None' if config.DOUYIN_COOKIES_FILE is None else config.DOUYIN_COOKIES_FILE}",
            f"{_DBG} {_douyin_debug_cookie_file(opts.get('cookiefile'))}",
            f"{_DBG} yt-dlp safe options={safe_opts}",
            f"{_DBG} user-agent={headers.get('User-Agent')}",
            f"{_DBG} proxy env set: HTTP_PROXY={bool(os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy'))} "
            f"HTTPS_PROXY={bool(os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy'))}",
        ]
        _dbg_logger.info(_douyin_debug_scrub("\n".join(lines)))
    except Exception as dbg_err:  # التشخيص ما لازم يكسر التحميل ابداً
        try:
            _dbg_logger.info("%s start-diagnostics failed: %r", _DBG, dbg_err)
        except Exception:
            pass


def _douyin_debug_fail(stage: str, platform: str, url: str):
    """يُستدعى داخل except: يطبع الخطأ الكامل مع traceback (واذا yt-dlp غلّف الخطأ يطبع الأصلي بعد).
    ما يرفع اي خطأ ابداً."""
    if platform != "douyin":
        return
    try:
        exc = sys.exc_info()[1]
        parts = [
            f"{_DBG} stage={stage} FAILED url={url}",
            f"{_DBG} exception type={type(exc).__module__}.{type(exc).__name__}",
            f"{_DBG} exception message={exc}",
            f"{_DBG} full traceback:\n{traceback.format_exc()}",
        ]
        inner = getattr(exc, "exc_info", None)  # yt-dlp DownloadError يحمل الخطأ الأصلي هنا
        if inner and len(inner) == 3 and inner[1] is not None and inner[1] is not exc:
            parts.append(
                f"{_DBG} inner (original yt-dlp) traceback:\n"
                + "".join(traceback.format_exception(*inner))
            )
        _dbg_logger.error(_douyin_debug_scrub("\n".join(parts)))
    except Exception:
        pass


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


async def get_direct_url(url: str, platform: str, height: int = 0) -> str | None:
    """يستخرج الرابط المباشر للفيديو (بدون تحميله على سيرفرنا) لأعلى جودة متوفرة عند الدقة
    المطلوبة. يفضّل صيغة فيها الصوت والفيديو مدموجين بملف واحد أصلاً (شائع بفيديوهات X القصيرة)
    حتى يقدر المستخدم يفتح الرابط مباشرة بمتصفحه بدون ما يحتاج دمج. يرجع None لو ما لقى صيغة مناسبة
    (نادر، ونتعامل معه بالكود اللي يستدعي هذي الدالة بالرجوع للتحميل العادي)."""

    def _extract():
        opts = _base_opts()
        opts.update(_platform_opts(platform))
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.to_thread(_extract)
    entries = _entries_of(info)
    entry = entries[0]

    effective_height = height if (height and height > 0) else config.MAX_QUALITY_HEIGHT
    formats = entry.get("formats") or []

    # نفضّل صيغة عندها فيديو وصوت مدموجين بملف واحد (يفتح مباشرة بالمتصفح بدون تعقيد)
    combined = [
        f for f in formats
        if f.get("height") and f["height"] <= effective_height
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and f.get("url")
    ]
    if combined:
        best = max(combined, key=lambda f: f["height"])
        return best["url"]

    # ما لقينا صيغة مدموجة: نرجع رابط الفيديو فقط (بدون صوت) كخيار أخير، أفضل من ما نرجع شي
    video_only = [
        f for f in formats
        if f.get("height") and f["height"] <= effective_height
        and f.get("vcodec") not in (None, "none")
        and f.get("url")
    ]
    if video_only:
        best = max(video_only, key=lambda f: f["height"])
        return best["url"]

    # آخر خيار: الرابط المباشر العام لو yt-dlp رجّعه بمستوى الـ entry نفسه
    return entry.get("url")


async def _download_alipay(url: str, on_stage=None) -> tuple[list[str], dict]:
    """Alipay: استخراج contentId + API ثم تنزيل الملف لنفس مجلد التحميل المؤقت.
    on_stage(name) دالة async اختيارية ('extracting' / 'downloading') لتحديث رسالة الحالة."""
    if on_stage:
        await on_stage("extracting")
    info = await asyncio.to_thread(alipay.fetch_info, url)
    if on_stage:
        await on_stage("downloading")
    try:
        files = await asyncio.to_thread(alipay.download, info)
    except Exception:
        alipay.invalidate(url)  # إعادة المحاولة تجيب رابط فيديو جديد بدل المخزن
        raise
    return files, info["meta"]


async def download_video(url: str, platform: str, height: int = 0, on_stage=None) -> tuple[list[str], dict]:
    """يحمل كل فيديوهات/صور المنشور (وحدة او اكثر) بأقرب دقة ممكنة للدقة المختارة
    (height=0 يعني أفضل جودة متوفرة تلقائياً - مستخدم لكل المنصات غير X)."""
    if platform == "alipay":
        return await _download_alipay(url, on_stage)

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
        _douyin_debug_start("download", platform, url, opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = _entries_of(info)
            meta = extract_meta(entries[0])
            files = [ydl.prepare_filename(e) for e in entries]
            return files, meta

    try:
        files, meta = await asyncio.to_thread(_download)
    except Exception as yt_error:
        _douyin_debug_fail("download", platform, url)
        cleanup_by_prefix(prefix)  # ننظف اي ملفات جزئية/مؤقتة تركها الفشل (خصوصاً اثناء الدمج)

        # Douyin's current web-detail API can return an empty response even with valid
        # cookies. Try the lightweight rendered-web parser before exposing the existing
        # paid TikHub fallback. This path uses requests only (no Chromium/Playwright),
        # so it stays suitable for Render Free.
        if platform == "douyin":
            try:
                web_files, web_meta = await asyncio.to_thread(douyin_web.download, url)
                return _fix_extensions(web_files), web_meta
            except Exception as web_error:
                logging.getLogger("douyin_web").warning(
                    "Douyin web fallback failed; keeping original yt-dlp error: %s", web_error
                )

        raise yt_error
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
        _douyin_debug_start("audio", platform, url, opts)
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
        _douyin_debug_fail("audio", platform, url)
        cleanup_by_prefix(prefix)
        raise
    files = [f for f in files if os.path.exists(f)]

    # اسم عرض للملف مبني على اسم صاحب الحساب (نظافة الاسم من رموز ممنوعة بأسماء الملفات)
    uploader_name = meta.get("uploader") or meta.get("uploader_id") or "audio"
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", uploader_name).strip() or "audio"
    meta["audio_display_name"] = safe_name

    return files, meta


async def verify_link(url: str, platform: str) -> tuple[bool, str | None]:
    """يتحقق ان الرابط شغال وقابل للوصول قبل لا نبدأ تحميل فعلي (بدون تحميل فعلي للملف).
    يسوي محاولتين قبل ما يحكم "فشل" - بعض المنصات (خصوصاً دويين) تصير عندها
    تذبذبات مؤقتة ترجع خطأ لحظي حتى لو الرابط شغال فعلاً.
    يرجع (نجح؟, نص الخطأ الحقيقي لو فشل والا None) - نص الخطأ يفيد بتقارير الأخطاء
    للمطور، كان يُبتلع سابقاً بدون أي تسجيل."""

    def _check():
        if platform == "alipay":
            try:
                alipay.fetch_info(url)
                return True, None
            except Exception as e:
                return False, str(e)
        opts = _base_opts()
        opts.update(_platform_opts(platform))
        _douyin_debug_start("verify", platform, url, opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=False)
            return True, None
        except Exception as e:
            _douyin_debug_fail("verify", platform, url)
            return False, str(e)

    ok, error = await asyncio.to_thread(_check)
    if ok:
        return True, None

    # محاولة ثانية بعد مهلة قصيرة - تتجنب الحكم بالفشل بسبب تذبذب لحظي
    await asyncio.sleep(2)
    ok2, error2 = await asyncio.to_thread(_check)

    # دويين: اذا yt-dlp فشل (مثلاً "Fresh cookies are needed") لا نوقف هنا - نجرب محلل صفحة الويب
    # المجاني (نفس الطريقة اللي يستخدمها التحميل)، حتى الرابط يوصل لمرحلة التحميل والـ Web Fallback.
    if not ok2 and platform == "douyin":
        try:
            await asyncio.to_thread(douyin_web.verify, url)
            return True, None
        except Exception as web_error:
            return False, f"{error2 or error}\n[Douyin web parser verification also failed: {web_error}]"

    return ok2, (error2 or error) if not ok2 else None


async def get_preview(url: str, platform: str) -> dict | None:
    """يجيب صورة مصغرة (thumbnail) ومدة الفيديو بدون تحميل فعلي، لعرض معاينة سريعة."""

    def _fetch():
        if platform == "alipay":
            try:
                meta = alipay.fetch_info(url)["meta"]
            except Exception:
                return None
            return {
                "thumbnail": meta.get("thumbnail"),
                "duration": meta.get("duration"),
                "title": meta.get("description") or "",
            }
        opts = _base_opts()
        opts.update(_platform_opts(platform))
        _douyin_debug_start("preview", platform, url, opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            _douyin_debug_fail("preview", platform, url)
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
