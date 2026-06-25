import atexit
import base64
import glob
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from flask import Flask, render_template, request, jsonify, send_file

_ROOT = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    static_folder=os.path.join(_ROOT, "images"),
    static_url_path="/images",
    template_folder=os.path.join(_ROOT, "templates"),
)
_is_serverless = bool(
    os.environ.get("VERCEL")
    or os.environ.get("VERCEL_ENV")
    or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
)
_is_render = bool(os.environ.get("RENDER"))
app.config["TEMPLATES_AUTO_RELOAD"] = not _is_serverless
app.jinja_env.auto_reload = not _is_serverless


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.unlink(probe)
        return True
    except OSError:
        return False


def _init_download_folder() -> str:
    override = os.environ.get("DOWNLOAD_FOLDER")
    local = os.path.join(_ROOT, "downloads")
    tmp = os.path.join(tempfile.gettempdir(), "downloads")
    if override:
        candidates = [override, tmp, local]
    elif _is_serverless:
        candidates = [tmp, local]
    else:
        candidates = [local, tmp]
    seen: set[str] = set()
    for folder in candidates:
        if not folder or folder in seen:
            continue
        seen.add(folder)
        if _is_writable_dir(folder):
            return folder
    fallback = os.path.join(tempfile.gettempdir(), "music-downloader")
    os.makedirs(fallback, exist_ok=True)
    return fallback


DOWNLOAD_FOLDER = _init_download_folder()
JOBS_DIR = os.path.join(DOWNLOAD_FOLDER, ".jobs")

# ── Cookie sources ─────────────────────────────────────────────────────────────
COOKIES_FILE         = os.environ.get("YTDLP_COOKIES_FILE")
COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")
COOKIES_CONTENT      = os.environ.get("YTDLP_COOKIES_CONTENT")
COOKIES_CONTENT_B64  = os.environ.get("YTDLP_COOKIES_CONTENT_B64")
COOKIES_TEMP_FILE    = None

# ── PO Token & Visitor Data (CRITICAL for server IPs) ───────────────────────
PO_TOKEN     = os.environ.get("YTDLP_PO_TOKEN")
VISITOR_DATA = os.environ.get("YTDLP_VISITOR_DATA")

# ── Proxy (HIGHLY RECOMMENDED for Render/datacenter IPs) ────────────────────
PROXY_URL = os.environ.get("YTDLP_PROXY")

# ── Sleep intervals (REQUIRED to avoid rate limits) ──────────────────────────
# YouTube rate limit: ~300 videos/hour for guest, ~2000 for accounts
# With sleep 5-10s, you stay well under the limit
MIN_SLEEP = int(os.environ.get("YTDLP_MIN_SLEEP", "5"))
MAX_SLEEP = int(os.environ.get("YTDLP_MAX_SLEEP", "10"))


# ── Cookie bootstrap ──────────────────────────────────────────────────────────

def _write_temp_cookie(content: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    tmp.write(content)
    tmp.close()
    return tmp.name

# 1. Base64-encoded content (safest for Render)
if COOKIES_CONTENT_B64 and not COOKIES_CONTENT and not COOKIES_FILE:
    try:
        decoded = base64.b64decode(COOKIES_CONTENT_B64).decode("utf-8")
        print(f"[cookies] Decoded YTDLP_COOKIES_CONTENT_B64 ({len(decoded)} bytes)")
        COOKIES_CONTENT = decoded
    except Exception as e:
        print(f"[cookies] Failed to decode YTDLP_COOKIES_CONTENT_B64: {e}")

# 2. Raw content → temp file
if COOKIES_CONTENT and not COOKIES_FILE:
    print(f"[cookies] Using YTDLP_COOKIES_CONTENT ({len(COOKIES_CONTENT)} bytes)")
    COOKIES_FILE = _write_temp_cookie(COOKIES_CONTENT)
    COOKIES_TEMP_FILE = COOKIES_FILE

# 3. Handle path vs content confusion
if COOKIES_FILE and not os.path.exists(COOKIES_FILE):
    looks_like_content = ("\n" in COOKIES_FILE) or COOKIES_FILE.strip().startswith("# Netscape")
    if looks_like_content:
        print("[cookies] Detected cookie content in YTDLP_COOKIES_FILE — writing to temp file")
        COOKIES_TEMP_FILE = _write_temp_cookie(COOKIES_FILE)
        COOKIES_FILE = COOKIES_TEMP_FILE
    else:
        print(f"[cookies] Warning: path does not exist: {COOKIES_FILE}")
        COOKIES_FILE = None
elif COOKIES_FILE:
    print(f"[cookies] Using YTDLP_COOKIES_FILE: {COOKIES_FILE}")

if PO_TOKEN:
    print(f"[auth] PO Token provided")
if VISITOR_DATA:
    print(f"[auth] Visitor Data provided")
if PROXY_URL:
    print(f"[proxy] Using proxy: {PROXY_URL}")
else:
    print("[warning] No proxy configured — datacenter IPs are frequently blocked by YouTube")


def clean_temp_cookie():
    try:
        if COOKIES_TEMP_FILE and os.path.exists(COOKIES_TEMP_FILE):
            os.unlink(COOKIES_TEMP_FILE)
    except OSError:
        pass

atexit.register(clean_temp_cookie)


# ── Central yt-dlp options builder ────────────────────────────────────────────

def apply_common_opts(ydl_opts: dict) -> dict:
    """Attach cookies, proxy, PO token, and anti-bot configuration."""

    # Cookies
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    if COOKIES_FROM_BROWSER:
        ydl_opts["cookiesfrombrowser"] = COOKIES_FROM_BROWSER

    # Proxy
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL

    # ── CRITICAL: Sleep intervals to avoid rate limits ─────────────────────
    # The wiki explicitly states: "add a delay of around 5-10 seconds between downloads"
    ydl_opts.setdefault("sleep_interval", MIN_SLEEP)
    ydl_opts.setdefault("max_sleep_interval", MAX_SLEEP)
    ydl_opts.setdefault("sleep_interval_requests", 5)

    # ── CRITICAL: Client strategy for blocked IPs ──────────────────────────
    # 
    # For datacenter IPs (Render, AWS, etc.), the recommended approach is:
    # 1. mweb client with PO Token (official yt-dlp recommendation)
    # 2. tv_embedded — no bot-check, guaranteed to work but limited formats
    # 3. ios — good audio streams, bypasses most bot checks
    #
    # WARNING: Do NOT use "web" client on server IPs — it will hit the bot check
    ydl_opts.setdefault("extractor_args", {})
    ydl_opts["extractor_args"].setdefault("youtube", {})

    # Priority: mweb (with PO token) > tv_embedded > ios
    # mweb is the officially recommended client for PO token usage
    clients = []
    
    if PO_TOKEN:
        # mweb + PO token is the officially supported combination
        clients.append("mweb")
    
    # tv_embedded has NO bot-check — essential for server IPs
    clients.append("tv_embedded")
    
    # ios bypasses most bot-checks and exposes audio-only streams
    clients.append("ios")
    
    # android as final fallback
    clients.append("android")

    ydl_opts["extractor_args"]["youtube"]["player_client"] = clients

    # PO Token — attach when provided (required for mweb client)
    if PO_TOKEN:
        # Format: "web+PO_TOKEN" or "mweb+PO_TOKEN" depending on client
        ydl_opts["extractor_args"]["youtube"]["po_token"] = [f"web+{PO_TOKEN}"]
    
    # Visitor Data — companion to PO token
    if VISITOR_DATA:
        ydl_opts["extractor_args"]["youtube"]["visitor_data"] = [VISITOR_DATA]

    # ── Skip webpage to avoid cookie rotation issues ───────────────────────
    # The wiki recommends skipping webpage requests when using visitor data
    # to avoid VISITOR_INFO1_LIVE cookie interference
    if VISITOR_DATA and not (COOKIES_FILE or COOKIES_FROM_BROWSER):
        ydl_opts["extractor_args"]["youtube"].setdefault("player_skip", [])
        ydl_opts["extractor_args"]["youtube"]["player_skip"].append("webpage")

    # Safer networking
    ydl_opts.setdefault("retries", 5)
    ydl_opts.setdefault("socket_timeout", 30)
    ydl_opts.setdefault("fragment_retries", 5)

    return ydl_opts


# ── Job state ─────────────────────────────────────────────────────────────────

download_jobs = {}
jobs_lock = threading.Lock()


def _save_job(job_id: str, job: dict) -> None:
    if _is_serverless:
        return
    try:
        os.makedirs(JOBS_DIR, exist_ok=True)
        path = os.path.join(JOBS_DIR, f"{job_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(job, f)
    except OSError as exc:
        print(f"[jobs] Failed to persist job {job_id}: {exc}")


def _load_job(job_id: str) -> dict | None:
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[jobs] Failed to load job {job_id}: {exc}")
        return None


def _get_job(job_id: str) -> dict | None:
    with jobs_lock:
        job = download_jobs.get(job_id)
    if job:
        return job
    return _load_job(job_id)


def _set_job(job_id: str, job: dict) -> None:
    with jobs_lock:
        download_jobs[job_id] = job
    _save_job(job_id, job)


def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip() or "Unknown"


def _locate_downloaded_file(artist_folder: str, title: str) -> str | None:
    safe = sanitize(title)
    for ext in (".mp3", ".m4a", ".webm", ".opus", ".ogg"):
        path = os.path.join(artist_folder, safe + ext)
        if os.path.isfile(path):
            return path
    matches = sorted(
        glob.glob(os.path.join(artist_folder, "*")),
        key=os.path.getmtime,
        reverse=True,
    )
    for path in matches:
        if os.path.isfile(path) and path.lower().endswith((".mp3", ".m4a", ".webm", ".opus", ".ogg")):
            return path
    return None


def make_progress_hook(songs: list, song_index: int):
    def hook(d):
        song = songs[song_index]

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            percent = int((downloaded / total) * 100) if total else 0
            song["progress"] = percent
            song["status"] = "downloading"

        elif d["status"] == "finished":
            song["progress"] = 100
            song["status"] = "converting"

        elif d["status"] == "error":
            song["status"] = "error"
            song["error"] = str(d.get("error", "Unknown error"))

    return hook


def _resolve_urls(urls: list) -> list:
    import yt_dlp

    resolved_songs = []

    for url in urls:
        url = url.strip()
        if not url:
            continue

        try:
            ydl_opts = apply_common_opts({
                "quiet": True,
                "extract_flat": True,
                "skip_download": True,
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                if "entries" in info:
                    for entry in info["entries"]:
                        if entry:
                            resolved_songs.append({
                                "url": (
                                    entry.get("url")
                                    or entry.get("webpage_url")
                                    or f"https://www.youtube.com/watch?v={entry['id']}"
                                ),
                                "title":  entry.get("title", "Unknown"),
                                "artist": entry.get("uploader") or entry.get("channel") or "Unknown Artist",
                                "status": "queued",
                                "progress": 0,
                                "selected": True,
                            })
                else:
                    resolved_songs.append({
                        "url":    url,
                        "title":  info.get("title", "Unknown"),
                        "artist": info.get("uploader") or info.get("channel") or "Unknown Artist",
                        "status": "queued",
                        "progress": 0,
                        "selected": True,
                    })

        except Exception as e:
            resolved_songs.append({
                "url":     url,
                "title":   url,
                "artist":  "Unknown Artist",
                "status":  "error",
                "progress": 0,
                "selected": False,
                "error":   str(e),
            })

    return resolved_songs


# ── Background workers ────────────────────────────────────────────────────────

def resolve_job(job_id: str, urls: list):
    resolved_songs = _resolve_urls(urls)

    with jobs_lock:
        download_jobs[job_id]["songs"]    = resolved_songs
        download_jobs[job_id]["resolved"] = True
        download_jobs[job_id]["status"]   = "ready"
    _save_job(job_id, download_jobs[job_id])


# ── Format constants & download helper ───────────────────────────────────────

# Format preference:
#   m4a audio-only → best for mp3 conversion (AAC → MP3 is near-lossless)
#   webm audio-only → Opus, excellent quality
#   bestaudio → any audio-only stream
#   best → combined stream; ffmpeg will extract audio track
_FORMAT_PRIMARY  = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
_FORMAT_FALLBACK = "best"


def _attempt_download(ydl_opts: dict, url: str) -> None:
    import yt_dlp

    """
    Three-pass download with automatic format/client fallback.
    
    Pass 1: Preferred audio formats with configured clients (mweb/tv_embedded/ios)
    Pass 2: 'best' combined format with same clients
    Pass 3: 'best' with web+android clients (last resort)
    """
    _NO_FORMAT = "Requested format is not available"
    _BOT_ERROR = "Sign in to confirm you're not a bot"
    _RATE_LIMIT = "This content isn't available, try again later"

    # ── Pass 1: preferred format chain ────────────────────────────────────
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return
    except Exception as exc:
        err_str = str(exc)
        if _NO_FORMAT not in err_str and _BOT_ERROR not in err_str and _RATE_LIMIT not in err_str:
            raise
        print(f"[format] pass-1 failed for {url!r}: {err_str[:120]}... retrying")

    # ── Pass 2: 'best' with same clients ─────────────────────────────────
    try:
        opts2 = {**ydl_opts, "format": _FORMAT_FALLBACK}
        with yt_dlp.YoutubeDL(opts2) as ydl:
            ydl.download([url])
        return
    except Exception as exc:
        err_str = str(exc)
        if _NO_FORMAT not in err_str and _BOT_ERROR not in err_str and _RATE_LIMIT not in err_str:
            raise
        print(f"[format] pass-2 failed — switching to web+android clients")

    # ── Pass 3: web+android clients, 'best' ─────────────────────────────
    web_ea: dict = {"youtube": {"player_client": ["web", "android"]}}
    if PO_TOKEN:
        web_ea["youtube"]["po_token"] = [f"web+{PO_TOKEN}"]
    if VISITOR_DATA:
        web_ea["youtube"]["visitor_data"] = [VISITOR_DATA]

    opts3 = {**ydl_opts, "format": _FORMAT_FALLBACK, "extractor_args": web_ea}
    with yt_dlp.YoutubeDL(opts3) as ydl:
        ydl.download([url])


def _download_songs(songs: list) -> None:
    for i, song in enumerate(songs):
        if not song.get("selected", True) or song.get("status") in ("done", "error"):
            continue

        song["status"] = "downloading"

        artist        = sanitize(song.get("artist", "Unknown Artist"))
        artist_folder = os.path.join(DOWNLOAD_FOLDER, artist)
        os.makedirs(artist_folder, exist_ok=True)

        has_ffmpeg = bool(shutil.which("ffmpeg") or shutil.which("ffmpeg.exe"))

        ydl_opts = apply_common_opts({
            "format":      _FORMAT_PRIMARY,
            "outtmpl":     os.path.join(artist_folder, "%(title)s.%(ext)s"),
            "quiet":       True,
            "no_warnings": True,
            "progress_hooks": [make_progress_hook(songs, i)],
        })

        if has_ffmpeg:
            ydl_opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata",     "add_metadata": True},
            ]
        else:
            ydl_opts["noplaylist"] = True

        try:
            _attempt_download(ydl_opts, song["url"])
            song["status"]  = "done"
            song["progress"] = 100
            song["folder"]   = artist
            located = _locate_downloaded_file(artist_folder, song.get("title", ""))
            if located:
                song["file"] = located
        except Exception as e:
            song["status"] = "error"
            song["error"]  = str(e)


def download_job(job_id: str):
    with jobs_lock:
        songs = download_jobs[job_id]["songs"]

    _download_songs(songs)

    with jobs_lock:
        download_jobs[job_id]["status"] = "done"
    _save_job(job_id, download_jobs[job_id])


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "download_folder": DOWNLOAD_FOLDER,
        "serverless": _is_serverless,
        "render": _is_render,
        "recommended_host": "render" if not _is_serverless else "local-or-render",
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/resolve", methods=["POST"])
def resolve():
    data = request.get_json()
    urls = data.get("urls", [])

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    job_id = str(uuid.uuid4())

    if _is_serverless:
        songs = _resolve_urls(urls)
        return jsonify({
            "job_id": job_id,
            "resolved": True,
            "status": "ready",
            "songs": songs,
        })

    with jobs_lock:
        download_jobs[job_id] = {
            "status":   "resolving",
            "resolved": False,
            "songs":    [],
        }
    _save_job(job_id, download_jobs[job_id])

    threading.Thread(target=resolve_job, args=(job_id, urls), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    job_id           = data.get("job_id")
    selected_indices = data.get("selected", [])
    client_songs     = data.get("songs")

    if _is_serverless:
        if not client_songs:
            return jsonify({"error": "No songs provided"}), 400

        songs = json.loads(json.dumps(client_songs))
        for i, song in enumerate(songs):
            song["selected"] = i in selected_indices
            if song["selected"] and song.get("status") != "done":
                song["status"]   = "queued"
                song["progress"] = 0

        job = {"status": "downloading", "resolved": True, "songs": songs}
        _download_songs(songs)
        job["status"] = "done"
        return jsonify({**job, "ok": True})

    with jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        for i, song in enumerate(job["songs"]):
            song["selected"] = i in selected_indices
            if song["selected"] and song["status"] != "done":
                song["status"]   = "queued"
                song["progress"] = 0

        job["status"] = "downloading"
    _save_job(job_id, job)

    threading.Thread(target=download_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/status/<job_id>")
def status(job_id):
    job = _get_job(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(job)


@app.route("/download-zip/<job_id>")
def download_zip(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    files = [
        song for song in job.get("songs", [])
        if song.get("selected") and song.get("status") == "done" and song.get("file") and os.path.isfile(song["file"])
    ]
    if not files:
        return jsonify({"error": "No completed downloads available"}), 404

    zip_path = os.path.join(tempfile.gettempdir(), f"batchbeat-{job_id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for song in files:
            artist = song.get("folder") or song.get("artist") or "Unknown"
            arcname = os.path.join(sanitize(artist), os.path.basename(song["file"]))
            archive.write(song["file"], arcname)

    return send_file(zip_path, as_attachment=True, download_name="batchbeat.zip")


@app.route("/downloads-path")
def downloads_path():
    return jsonify({"path": DOWNLOAD_FOLDER})


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/favicon.png")
def favicon_png():
    return "", 204


@app.route("/.well-known/appspecific/<path:filename>")
def suppress_appspecific(filename):
    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")

    print("\n  Music Downloader is running!")
    print(f"  Open http://{host}:{port} in your browser\n")

    app.run(debug=False, host=host, port=port)