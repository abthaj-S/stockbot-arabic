"""
تحميلي 🎀 — تطبيق ويب لتحميل الفيديو والصوت من الروابط.

الأوضاع:
  video  → فيديو كامل بالصوت (MP4)
  mute   → فيديو بدون صوت (MP4)
  audio  → صوت فقط (MP3 / M4A / WAV)
"""
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
FILE_TTL_SECONDS = 60 * 60  # تُحذف الملفات بعد ساعة

AUDIO_FORMATS = {"mp3", "m4a", "wav"}
AUDIO_BITRATES = {"128", "192", "256", "320"}


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
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ─────────────────────────── أدوات مساعدة ───────────────────────────

def is_valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://\S+$", url or ""))


def clean_error(err: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(err))
    msg = msg.replace("ERROR: ", "")
    if "Unsupported URL" in msg:
        return "الرابط غير مدعوم 😕 جرّب رابطاً من يوتيوب، تيك توك، إنستغرام، X أو غيرها"
    if "Private video" in msg or "login" in msg.lower():
        return "المقطع خاص أو يحتاج تسجيل دخول 🔒"
    if "Unable to download" in msg or "connect" in msg.lower():
        return "تعذّر الوصول للرابط، تأكد من الإنترنت ومن صحة الرابط 🌐"
    return msg[:300]


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", name or "video").strip()
    return (name[:120] or "video").strip(". ")


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


def base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG,
        "socket_timeout": 30,
        "retries": 3,
    }


# ─────────────────────────── الصفحات ───────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/info")
def api_info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not is_valid_url(url):
        return jsonify(error="الرجاء إدخال رابط صحيح يبدأ بـ http أو https"), 400
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        return jsonify(error=clean_error(e)), 400

    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)

    formats = info.get("formats") or [info]
    heights = sorted(
        {f["height"] for f in formats if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    has_audio = any(f.get("acodec") not in (None, "none") for f in formats) or not heights

    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")

    return jsonify(
        title=info.get("title") or "مقطع بدون عنوان",
        thumbnail=thumb,
        duration=info.get("duration"),
        uploader=info.get("uploader") or info.get("channel"),
        source=info.get("extractor_key") or info.get("extractor"),
        heights=heights,
        # نعتبره صوتياً فقط إذا صرّح الموقع بذلك (مثل ساوند كلاود)؛ الترميز المجهول قد يكون فيديو
        has_video=bool(heights) or any(f.get("vcodec") != "none" for f in formats),
        has_audio=has_audio,
    )


@app.post("/api/download")
def api_download():
    cleanup_old_files()
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    mode = data.get("mode", "video")
    quality = str(data.get("quality", "best"))
    audio_format = data.get("audio_format", "mp3")
    bitrate = str(data.get("bitrate", "320"))

    if not is_valid_url(url):
        return jsonify(error="رابط غير صالح"), 400
    if mode not in {"video", "mute", "audio"}:
        return jsonify(error="وضع غير معروف"), 400
    if quality != "best" and not quality.isdigit():
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
        }
    threading.Thread(
        target=run_job,
        args=(job_id, url, mode, quality, audio_format, bitrate),
        daemon=True,
    ).start()
    return jsonify(job_id=job_id)


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="المهمة غير موجودة"), 404
    return jsonify({k: v for k, v in job.items() if k not in {"file", "created"}})


@app.get("/api/file/<job_id>")
def api_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        abort(404)
    return send_file(job["file"], as_attachment=True, download_name=job["name"])


# ─────────────────────────── منطق التحميل ───────────────────────────

def update(job_id: str, **kw) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw)


def run_job(job_id, url, mode, quality, audio_format, bitrate):
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    h = "" if quality == "best" else f"[height<={quality}]"

    opts = base_opts()
    opts["outtmpl"] = str(out_dir / "%(id)s.%(ext)s")
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

    state = {"part": 0, "parts": 1}

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
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

    def pp_hook(d):
        if d["status"] == "started":
            update(job_id, status="processing", stage="نجهّز الملف لك ✨", percent=99.0)

    opts["progress_hooks"] = [progress_hook]
    opts["postprocessor_hooks"] = [pp_hook]

    try:
        update(job_id, status="starting", stage="نقرأ الرابط…")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = next(e for e in info["entries"] if e)
            state["parts"] = len(info.get("requested_formats") or []) or 1
            info = ydl.process_ie_result(info, download=True)

        files = [p for p in out_dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
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
        update(job_id, status="error", stage="حدث خطأ", error=clean_error(e))


def strip_audio(src: Path) -> Path:
    """يحذف مسار الصوت بدون إعادة ترميز الفيديو (بدون فقدان جودة)."""
    dst = src.with_name(src.stem + "_mute.mp4")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
           "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # بعض الترميزات لا تدخل حاوية MP4 كما هي — نرجع لنفس الامتداد الأصلي
        dst = src.with_name(src.stem + "_mute" + src.suffix)
        cmd[-1] = str(dst)
        cmd = [c for c in cmd if c not in ("-movflags", "+faststart")]
        subprocess.run(cmd, check=True, capture_output=True)
    src.unlink(missing_ok=True)
    return dst


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  🎀 تحميلي يعمل الآن على: http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
