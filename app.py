import atexit
import base64
import os
import re
import shutil
import tempfile
import threading
from flask import Flask, render_template, request, jsonify
import yt_dlp

app = Flask(__name__, static_folder="images", static_url_path="/images")
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ── Cookie sources (checked in priority order) ────────────────────────────────
COOKIES_FILE           = os.environ.get("YTDLP_COOKIES_FILE")           # path to a .txt file
COOKIES_FROM_BROWSER   = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")   # e.g. "chrome"
COOKIES_CONTENT        = os.environ.get("YTDLP_COOKIES_CONTENT")        # raw Netscape text
COOKIES_CONTENT_B64    = os.environ.get("YTDLP_COOKIES_CONTENT_B64")    # base64-encoded (recommended for Render)
COOKIES_TEMP_FILE      = None

# ── Optional extras ───────────────────────────────────────────────────────────
PROXY_URL    = os.environ.get("YTDLP_PROXY")        # http://user:pass@host:port
PO_TOKEN     = os.environ.get("YTDLP_PO_TOKEN")     # YouTube PO token
VISITOR_DATA = os.environ.get("YTDLP_VISITOR_DATA") # companion to PO_TOKEN


# ── Cookie bootstrap ──────────────────────────────────────────────────────────

def _write_temp_cookie(content: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    tmp.write(content)
    tmp.close()
    return tmp.name


# 1. Prefer base64-encoded content (safest for Render — avoids multiline mangling)
if COOKIES_CONTENT_B64 and not COOKIES_CONTENT and not COOKIES_FILE:
    try:
        decoded = base64.b64decode(COOKIES_CONTENT_B64).decode("utf-8")
        print(f"[cookies] Decoded YTDLP_COOKIES_CONTENT_B64 ({len(decoded)} bytes)")
        COOKIES_CONTENT = decoded
    except Exception as e:
        print(f"[cookies] Failed to decode YTDLP_COOKIES_CONTENT_B64: {e}")

# 2. Raw content string → write to temp file
if COOKIES_CONTENT and not COOKIES_FILE:
    print(f"[cookies] Using YTDLP_COOKIES_CONTENT ({len(COOKIES_CONTENT)} bytes)")
    COOKIES_FILE = _write_temp_cookie(COOKIES_CONTENT)
    COOKIES_TEMP_FILE = COOKIES_FILE

# 3. COOKIES_FILE is a path — but sometimes people paste the content there by mistake
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
    print("[auth] PO token provided")
if PROXY_URL:
    print(f"[proxy] Using proxy: {PROXY_URL}")


def clean_temp_cookie():
    try:
        if COOKIES_TEMP_FILE and os.path.exists(COOKIES_TEMP_FILE):
            os.unlink(COOKIES_TEMP_FILE)
    except OSError:
        pass


atexit.register(clean_temp_cookie)


# ── Central yt-dlp options builder ────────────────────────────────────────────

def apply_common_opts(ydl_opts: dict) -> dict:
    """Attach cookies, proxy, and anti-bot arguments to any yt-dlp options dict."""

    # Cookies
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    if COOKIES_FROM_BROWSER:
        ydl_opts["cookiesfrombrowser"] = COOKIES_FROM_BROWSER

    # Proxy
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL

    # ── Player client strategy ────────────────────────────────────────────────
    # Client order matters — yt-dlp uses the FIRST successful client for both
    # auth and the format table. Wrong first client = bot-check OR no audio streams.
    #
    # `ios`         — bypasses most datacenter bot-checks AND exposes m4a audio-only
    #                 streams (format 140). Best first choice on Render.
    # `tv_embedded` — guaranteed no bot-check; but only has combined streams (18/22).
    #                 Fallback if ios gets flagged.
    # `android`     — full format access; sometimes bot-checked on server IPs.
    # `web`         — full formats with cookies; most likely to hit the sign-in gate
    #                 without a residential IP, so kept last.
    ydl_opts.setdefault("extractor_args", {})
    ydl_opts["extractor_args"].setdefault("youtube", {})
    ydl_opts["extractor_args"]["youtube"]["player_client"] = ["ios", "tv_embedded", "android", "web"]

    # PO Token — attach when provided (helps on server IPs)
    if PO_TOKEN:
        ydl_opts["extractor_args"]["youtube"]["po_token"] = [f"web+{PO_TOKEN}"]
    if VISITOR_DATA:
        ydl_opts["extractor_args"]["youtube"]["visitor_data"] = [VISITOR_DATA]

    # Safer networking
    ydl_opts.setdefault("retries", 5)
    ydl_opts.setdefault("socket_timeout", 30)

    return ydl_opts


# ── Job state ─────────────────────────────────────────────────────────────────

download_jobs = {}
jobs_lock = threading.Lock()


def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip() or "Unknown"


def make_progress_hook(job_id: str, song_index: int):
    def hook(d):
        with jobs_lock:
            job = download_jobs.get(job_id)
            if not job:
                return
            song = job["songs"][song_index]

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


# ── Background workers ────────────────────────────────────────────────────────

def resolve_job(job_id: str, urls: list):
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

    with jobs_lock:
        download_jobs[job_id]["songs"]    = resolved_songs
        download_jobs[job_id]["resolved"] = True
        download_jobs[job_id]["status"]   = "ready"


# ── Format constants & download helper ───────────────────────────────────────

# Format preference chain:
#   m4a audio-only  → best for mp3 conversion (AAC → MP3 is near-lossless)
#   webm audio-only → Opus, excellent quality
#   bestaudio       → any audio-only stream
#   best            → combined stream; ffmpeg will extract the audio track
_FORMAT_PRIMARY  = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
_FORMAT_FALLBACK = "best"   # always exists on any client; ffmpeg handles the rest


def _attempt_download(ydl_opts: dict, url: str) -> None:
    """
    Three-pass download with automatic format/client fallback.

    Pass 1 — preferred formats (m4a / webm audio-only, then combined best)
              with the configured client list (ios → tv_embedded → android → web).
    Pass 2 — absolute selector 'best' with the same clients.
              Catches the case where ios/tv_embedded return a limited format table
              that has combined streams but nothing matching 'bestaudio'.
    Pass 3 — 'best' forced through web+android only.
              Last resort for region-locked or age-restricted videos where the
              ios/tv_embedded table comes back empty.

    We catch plain Exception (not just DownloadError) because yt-dlp can raise
    ExtractorError or even a bare ValueError from its format-selector code path
    depending on the installed version.
    """
    _NO_FORMAT = "Requested format is not available"

    # ── Pass 1: preferred format chain ────────────────────────────────────
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return
    except Exception as exc:
        if _NO_FORMAT not in str(exc):
            raise                   # real error (network, auth, etc.) — surface it
        print(f"[format] pass-1 format unavailable for {url!r} — retrying with 'best'")

    # ── Pass 2: 'best' with same clients ─────────────────────────────────
    try:
        opts2 = {**ydl_opts, "format": _FORMAT_FALLBACK}
        with yt_dlp.YoutubeDL(opts2) as ydl:
            ydl.download([url])
        return
    except Exception as exc:
        if _NO_FORMAT not in str(exc):
            raise
        print(f"[format] pass-2 still unavailable — switching to web+android clients")

    # ── Pass 3: web+android clients, 'best' ───────────────────────────────
    web_ea: dict = {"youtube": {"player_client": ["web", "android"]}}
    if PO_TOKEN:
        web_ea["youtube"]["po_token"] = [f"web+{PO_TOKEN}"]
    if VISITOR_DATA:
        web_ea["youtube"]["visitor_data"] = [VISITOR_DATA]

    opts3 = {**ydl_opts, "format": _FORMAT_FALLBACK, "extractor_args": web_ea}
    with yt_dlp.YoutubeDL(opts3) as ydl:
        ydl.download([url])


def download_job(job_id: str):
    with jobs_lock:
        songs = download_jobs[job_id]["songs"]

    for i, song in enumerate(songs):
        with jobs_lock:
            selected       = download_jobs[job_id]["songs"][i].get("selected", True)
            current_status = download_jobs[job_id]["songs"][i]["status"]

        if not selected or current_status in ("done", "error"):
            continue

        with jobs_lock:
            download_jobs[job_id]["songs"][i]["status"] = "downloading"

        artist        = sanitize(song.get("artist", "Unknown Artist"))
        artist_folder = os.path.join(DOWNLOAD_FOLDER, artist)
        os.makedirs(artist_folder, exist_ok=True)

        has_ffmpeg = bool(shutil.which("ffmpeg") or shutil.which("ffmpeg.exe"))

        ydl_opts = apply_common_opts({
            "format":      _FORMAT_PRIMARY,
            "outtmpl":     os.path.join(artist_folder, "%(title)s.%(ext)s"),
            "quiet":       True,
            "no_warnings": True,
            "progress_hooks": [make_progress_hook(job_id, i)],
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

            with jobs_lock:
                download_jobs[job_id]["songs"][i]["status"]  = "done"
                download_jobs[job_id]["songs"][i]["progress"] = 100
                download_jobs[job_id]["songs"][i]["folder"]   = artist

        except Exception as e:
            with jobs_lock:
                download_jobs[job_id]["songs"][i]["status"] = "error"
                download_jobs[job_id]["songs"][i]["error"]  = str(e)

    with jobs_lock:
        download_jobs[job_id]["status"] = "done"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/resolve", methods=["POST"])
def resolve():
    data = request.get_json()
    urls = data.get("urls", [])

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    import uuid
    job_id = str(uuid.uuid4())

    with jobs_lock:
        download_jobs[job_id] = {
            "status":   "resolving",
            "resolved": False,
            "songs":    [],
        }

    threading.Thread(target=resolve_job, args=(job_id, urls), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    job_id           = data.get("job_id")
    selected_indices = data.get("selected", [])

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

    threading.Thread(target=download_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = download_jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(job)


@app.route("/downloads-path")
def downloads_path():
    return jsonify({"path": DOWNLOAD_FOLDER})


@app.route("/favicon.ico")
def favicon():
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