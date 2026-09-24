"""
تحميلي 🎀 — تطبيق ويب لتحميل الفيديو والصوت من الروابط.

الأوضاع:
  video  → فيديو كامل بالصوت (MP4)
  mute   → فيديو بدون صوت (MP4)
  audio  → صوت فقط (MP3 / M4A / WAV)

إعدادات الأمان (متغيرات بيئة):
  APP_PASSWORD        كلمة مرور لحماية التطبيق (إلزامية إذا فتحته على الشبكة)
  ALLOWED_HOSTS       أسماء النطاق المسموحة في ترويسة Host (مفصولة بفاصلة)
  ALLOW_DIRECT_LINKS  1 للسماح بالروابط المباشرة للملفات (مغلق افتراضياً)
  MAX_FILESIZE_MB     الحد الأقصى لحجم الملف (افتراضي 2048)
  MAX_DURATION_MIN    الحد الأقصى لمدة المقطع بالدقائق (افتراضي 180)
  MAX_ACTIVE_JOBS     عدد التحميلات المتزامنة (افتراضي 3)
"""
import base64
import hmac
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
FILE_TTL_SECONDS = 60 * 60  # تُحذف الملفات بعد ساعة

AUDIO_FORMATS = {"mp3", "m4a", "wav"}
AUDIO_BITRATES = {"128", "192", "256", "320"}

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
ALLOW_DIRECT_LINKS = os.environ.get("ALLOW_DIRECT_LINKS") == "1"
MAX_FILESIZE = int(os.environ.get("MAX_FILESIZE_MB", "2048")) * 1024 * 1024
MAX_DURATION = int(os.environ.get("MAX_DURATION_MIN", "180")) * 60
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "3"))
MIN_FREE_DISK = 1024 * 1024 * 1024  # نرفض التحميل إذا بقي أقل من 1GB
MAX_URL_LENGTH = 2048
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# حدود الطلبات لكل عنوان IP: (عدد الطلبات، خلال كم ثانية)
RATE_LIMITS = {"info": (20, 60), "download": (10, 600)}

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_env_hosts = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
ALLOWED_HOSTS = _env_hosts or (LOCAL_HOSTS if HOST in LOCAL_HOSTS else set())

log = logging.getLogger("tahmeeli")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def find_ffmpeg() -> str:
    """نستخدم ffmpeg من النظام إن وُجد، وإلا النسخة المرفقة مع imageio-ffmpeg."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    import imageio_ffmpeg

    bundled = imageio_ffmpeg.get_ffmpeg_exe()
    # yt-dlp يبحث عن ملف اسمه ffmpeg داخل المجلد، فننشئ رابطاً بهذا الاسم
    bin_dir = BASE_DIR / ".bin"
    bin_dir.mkdir(exist_ok=True)
    link = bin_dir / "ffmpeg"
    if not link.exists():
        try:
            link.symlink_to(bundled)
        except OSError:
            shutil.copy2(bundled, link)
    return str(link)


FFMPEG = find_ffmpeg()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # الطلبات عبارة عن JSON صغير فقط
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
rate_hits: dict[tuple[str, str], deque] = defaultdict(deque)
rate_lock = threading.Lock()


# ─────────────────────────── طبقة الأمان ───────────────────────────

class ForbiddenURL(Exception):
    pass


def check_public_url(url: str) -> None:
    """يمنع SSRF: نرفض أي رابط يشير لعناوين داخلية (الراوتر، localhost، خوادم السحابة…)."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise ForbiddenURL("الرابط غير صالح")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ForbiddenURL("الرجاء إدخال رابط صحيح يبدأ بـ http أو https")
    if parts.username or parts.password:
        raise ForbiddenURL("الروابط التي تحتوي على بيانات دخول غير مسموحة")
    try:
        port = parts.port
    except ValueError:
        raise ForbiddenURL("الرابط غير صالح") from None
    try:
        infos = socket.getaddrinfo(parts.hostname, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ForbiddenURL("تعذّر الوصول للرابط، تأكد من صحته 🌐") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise ForbiddenURL("هذا الرابط يشير لعنوان داخلي وغير مسموح 🚫")


def client_ip() -> str:
    # لا نثق بـ X-Forwarded-For لأنه يمكن تزويره
    return request.remote_addr or "unknown"


def rate_limited(bucket: str) -> bool:
    limit, window = RATE_LIMITS[bucket]
    now = time.monotonic()
    with rate_lock:
        q = rate_hits[(bucket, client_ip())]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


def active_jobs() -> int:
    with jobs_lock:
        return sum(j["status"] in {"queued", "starting", "downloading", "processing"} for j in jobs.values())


def password_ok() -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        _, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(pwd.encode(), APP_PASSWORD.encode())


@app.before_request
def security_gate():
    # حماية من DNS rebinding: نقبل فقط أسماء المضيف المعروفة
    if ALLOWED_HOSTS:
        host = (request.host or "").lower()
        host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if host not in ALLOWED_HOSTS:
            abort(400)

    if APP_PASSWORD and not password_ok():
        time.sleep(0.5)  # يبطئ محاولات تخمين كلمة المرور
        return Response(
            "مطلوب كلمة مرور", 401,
            {"WWW-Authenticate": 'Basic realm="tahmeeli", charset="UTF-8"'},
        )

    # حماية CSRF: طلبات POST يجب أن تكون JSON ومن نفس الموقع
    if request.method == "POST":
        if not request.is_json:
            return jsonify(error="نوع الطلب غير مسموح"), 415
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != (request.host or "").lower():
            return jsonify(error="طلب من مصدر غير موثوق"), 403
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return jsonify(error="طلب من مصدر غير موثوق"), 403


@app.after_request
def security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' https: data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ─────────────────────────── أدوات مساعدة ───────────────────────────

def clean_error(err: Exception) -> str:
    """رسائل مفهومة للمستخدم بدون كشف تفاصيل داخلية عن الخادم."""
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(err))
    if "Unsupported URL" in msg or "No suitable extractor" in msg:
        return "الرابط غير مدعوم 😕 جرّب رابطاً من يوتيوب، تيك توك، إنستغرام، X أو غيرها"
    if "File is larger than max-filesize" in msg or "larger than" in msg:
        return f"الملف أكبر من الحد المسموح ({MAX_FILESIZE // 1024 // 1024} MB) 📦"
    if "does not pass filter" in msg or "المقطع أطول" in msg:
        return f"المقطع أطول من الحد المسموح ({MAX_DURATION // 60} دقيقة) ⏱️"
    if "Private video" in msg or "login" in msg.lower() or "sign in" in msg.lower():
        return "المقطع خاص أو يحتاج تسجيل دخول 🔒"
    if "Unable to download" in msg or "connect" in msg.lower() or "HTTP Error" in msg:
        return "تعذّر الوصول للرابط، تأكد من الإنترنت ومن صحة الرابط 🌐"
    return "حدث خطأ غير متوقع، جرّب مرة ثانية أو رابطاً آخر"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', " ", name or "video")
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name[:120] or "video"


def cleanup_old_files() -> None:
    now = time.time()
    for child in DOWNLOAD_DIR.iterdir():
        try:
            if now - child.stat().st_mtime > FILE_TTL_SECONDS:
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
        except FileNotFoundError:
            pass
    with jobs_lock:
        for jid in [j for j, v in jobs.items() if now - v["created"] > FILE_TTL_SECONDS]:
            jobs.pop(jid, None)


def duration_filter(info, *, incomplete):
    d = info.get("duration")
    if d and d > MAX_DURATION:
        return "المقطع أطول من الحد المسموح"
    return None


def base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG,
        "socket_timeout": 30,
        "retries": 3,
        # بدون الروابط المباشرة يتصل yt-dlp فقط بالمواقع المعروفة (يوتيوب، تيك توك…)
        "allowed_extractors": ["default"] if ALLOW_DIRECT_LINKS else ["default", "-generic"],
        "enable_file_urls": False,
        "match_filter": duration_filter,
        "cookiefile": None,
    }


def safe_http_url(value):
    return value if isinstance(value, str) and value.startswith("https://") else None


# ─────────────────────────── الصفحات ───────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/info")
def api_info():
    if rate_limited("info"):
        return jsonify(error="طلبات كثيرة، انتظر شوي وجرّب ⏳"), 429
    url = str((request.get_json(silent=True) or {}).get("url", "")).strip()
    try:
        check_public_url(url)
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except ForbiddenURL as e:
        return jsonify(error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        log.warning("info failed for %s: %s", client_ip(), e)
        return jsonify(error=clean_error(e)), 400

    if info.get("_type") == "playlist":
        info = next((e for e in info.get("entries") or [] if e), None)
        if not info:
            return jsonify(error="ما لقينا مقطع في هذا الرابط"), 400

    formats = info.get("formats") or [info]
    heights = sorted(
        {int(f["height"]) for f in formats
         if isinstance(f.get("height"), (int, float)) and f.get("vcodec") != "none"},
        reverse=True,
    )
    has_audio = any(f.get("acodec") not in (None, "none") for f in formats) or not heights

    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")

    duration = info.get("duration")
    return jsonify(
        title=str(info.get("title") or "مقطع بدون عنوان")[:300],
        thumbnail=safe_http_url(thumb),
        duration=duration if isinstance(duration, (int, float)) else None,
        uploader=str(info.get("uploader") or info.get("channel") or "")[:100] or None,
        source=str(info.get("extractor_key") or info.get("extractor") or "")[:40],
        heights=heights,
        # نعتبره صوتياً فقط إذا صرّح الموقع بذلك (مثل ساوند كلاود)؛ الترميز المجهول قد يكون فيديو
        has_video=bool(heights) or any(f.get("vcodec") != "none" for f in formats),
        has_audio=has_audio,
    )


@app.post("/api/download")
def api_download():
    cleanup_old_files()
    if rate_limited("download"):
        return jsonify(error="حمّلت كثير خلال وقت قصير، ارتاح شوي وارجع ⏳"), 429
    if active_jobs() >= MAX_ACTIVE_JOBS:
        return jsonify(error="الخادم مشغول بتحميلات ثانية، جرّب بعد دقيقة ⏳"), 429
    if shutil.disk_usage(DOWNLOAD_DIR).free < MIN_FREE_DISK:
        return jsonify(error="المساحة على الخادم ممتلئة 💾"), 507

    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    mode = data.get("mode", "video")
    quality = str(data.get("quality", "best"))
    audio_format = data.get("audio_format", "mp3")
    bitrate = str(data.get("bitrate", "320"))

    try:
        check_public_url(url)
    except ForbiddenURL as e:
        return jsonify(error=str(e)), 400
    if mode not in {"video", "mute", "audio"}:
        return jsonify(error="وضع غير معروف"), 400
    if quality != "best" and not (quality.isdigit() and 1 <= int(quality) <= 8640):
        return jsonify(error="جودة غير صالحة"), 400
    if audio_format not in AUDIO_FORMATS:
        audio_format = "mp3"
    if bitrate not in AUDIO_BITRATES:
        bitrate = "320"

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "stage": "بالانتظار…",
            "percent": 0.0,
            "speed": None,
            "eta": None,
            "file": None,
            "name": None,
            "error": None,
            "created": time.time(),
            # نربط الملف بمن طلبه حتى لا يستطيع غيره تنزيله
            "owner": secrets.token_urlsafe(24),
        }
        owner = jobs[job_id]["owner"]
    threading.Thread(
        target=run_job,
        args=(job_id, url, mode, quality, audio_format, bitrate),
        daemon=True,
    ).start()
    resp = jsonify(job_id=job_id)
    resp.set_cookie(f"job_{job_id}", owner, max_age=FILE_TTL_SECONDS,
                    httponly=True, samesite="Strict", secure=request.is_secure)
    return resp


def get_owned_job(job_id: str) -> dict:
    if not JOB_ID_RE.match(job_id):
        abort(404)
    job = jobs.get(job_id)
    token = request.cookies.get(f"job_{job_id}", "")
    if not job or not hmac.compare_digest(token.encode(), job["owner"].encode()):
        abort(404)
    return job


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    job = get_owned_job(job_id)
    return jsonify({k: v for k, v in job.items() if k not in {"file", "created", "owner"}})


@app.get("/api/file/<job_id>")
def api_file(job_id):
    job = get_owned_job(job_id)
    if job["status"] != "done" or not job["file"]:
        abort(404)
    path = Path(job["file"]).resolve()
    if DOWNLOAD_DIR.resolve() not in path.parents or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=job["name"])


@app.errorhandler(404)
def not_found(_):
    return jsonify(error="غير موجود"), 404


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="الطلب كبير جداً"), 413


# ─────────────────────────── منطق التحميل ───────────────────────────

def update(job_id: str, **kw) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw)


def run_job(job_id, url, mode, quality, audio_format, bitrate):
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    h = "" if quality == "best" else f"[height<={int(quality)}]"

    opts = base_opts()
    opts["outtmpl"] = str(out_dir / "media.%(ext)s")
    # نفضّل H.264 + AAC عند تساوي الدقة لأنها تعمل على كل الأجهزة
    opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:m4a"]

    if mode == "video":
        opts["format"] = f"bv*{h}+ba/b{h}/bv*+ba/b"
        opts["merge_output_format"] = "mp4"
    elif mode == "mute":
        opts["format"] = f"bv*{h}/b{h}/bv*/b"
    else:  # audio
        opts["format"] = "ba/b"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": bitrate,
        }]

    state = {"part": 0, "parts": 1, "bytes": 0}

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            # حماية إضافية: نوقف التحميل إذا تجاوز الحجم الحد (بعض المواقع لا تعلن الحجم مسبقاً)
            if state["bytes"] + max(done, total or 0) > MAX_FILESIZE:
                raise yt_dlp.utils.DownloadError("File is larger than max-filesize")
            p = (done / total * 100) if total else 0
            overall = (state["part"] * 100 + p) / state["parts"]
            update(
                job_id,
                status="downloading",
                stage="جاري التحميل…" if state["parts"] == 1
                else f"جاري التحميل ({state['part'] + 1}/{state['parts']})…",
                percent=round(min(overall, 99.0), 1),
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            state["part"] += 1
            state["bytes"] += d.get("downloaded_bytes") or d.get("total_bytes") or 0

    def pp_hook(d):
        if d["status"] == "started":
            update(job_id, status="processing", stage="نجهّز الملف لك ✨", percent=99.0)

    opts["progress_hooks"] = [progress_hook]
    opts["postprocessor_hooks"] = [pp_hook]

    try:
        update(job_id, status="starting", stage="نقرأ الرابط…")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                info = next((e for e in info.get("entries") or [] if e), None)
                if not info:
                    raise RuntimeError("no entries")
            chosen = info.get("requested_formats") or [info]
            state["parts"] = len(chosen)
            # نرفض الملفات الكبيرة قبل ما نبدأ (yt-dlp يتخطاها بصمت)
            size = sum(f.get("filesize") or f.get("filesize_approx") or 0 for f in chosen)
            if size > MAX_FILESIZE:
                raise yt_dlp.utils.DownloadError("File is larger than max-filesize")
            info = ydl.process_ie_result(info, download=True)

        files = [p for p in out_dir.iterdir()
                 if p.is_file() and not p.is_symlink() and not p.name.endswith(".part")]
        if not files:
            raise RuntimeError("لم يتم العثور على الملف بعد التحميل")
        result = max(files, key=lambda p: p.stat().st_size)

        if mode == "mute":
            update(job_id, status="processing", stage="نفصل الصوت عن الفيديو 🔇", percent=99.0)
            result = strip_audio(result)

        title = safe_filename(info.get("title"))
        suffix = {"video": "", "mute": " (بدون صوت)", "audio": ""}[mode]
        update(
            job_id,
            status="done",
            stage="جاهز! 🎉",
            percent=100.0,
            file=str(result),
            name=f"{title}{suffix}{result.suffix}",
            speed=None,
            eta=None,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("job %s failed: %s", job_id, e)
        shutil.rmtree(out_dir, ignore_errors=True)
        update(job_id, status="error", stage="حدث خطأ", error=clean_error(e))


def strip_audio(src: Path) -> Path:
    """يحذف مسار الصوت بدون إعادة ترميز الفيديو (بدون فقدان جودة)."""
    dst = src.with_name("media_mute.mp4")
    cmd = [FFMPEG, "-y", "-nostdin", "-loglevel", "error", "-i", str(src),
           "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        # بعض الترميزات لا تدخل حاوية MP4 كما هي — نرجع لنفس الامتداد الأصلي
        dst = src.with_name("media_mute" + src.suffix)
        cmd[-1] = str(dst)
        cmd = [c for c in cmd if c not in ("-movflags", "+faststart")]
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    src.unlink(missing_ok=True)
    return dst


def main():
    if HOST not in LOCAL_HOSTS and not APP_PASSWORD:
        raise SystemExit(
            "⛔ رفضنا التشغيل: فتحت التطبيق على الشبكة بدون كلمة مرور.\n"
            "   أضف APP_PASSWORD=كلمة_قوية ، مثال:\n"
            "   HOST=0.0.0.0 APP_PASSWORD='xxxx' python app.py"
        )
    print(f"\n  🎀 تحميلي يعمل الآن على: http://{HOST}:{PORT}\n")
    try:
        from waitress import serve  # خادم مناسب للاستخدام الفعلي بدل خادم التطوير
    except ImportError:
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    else:
        serve(app, host=HOST, port=PORT, threads=8)


if __name__ == "__main__":
    main()
