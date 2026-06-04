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
COOKIES_CONTENT_B64    = os.environ.get("YTDLP_COOKIES_CONTENT_B64")    # ← NEW: base64-encoded (recommended for Render)
COOKIES_TEMP_FILE      = None

# ── Optional extras ───────────────────────────────────────────────────────────
# Residential proxy — the most reliable fix for datacenter IP blocks.
# Format: http://user:pass@host:port  or  socks5://user:pass@host:port
PROXY_URL              = os.environ.get("YTDLP_PROXY")

# PO Token — helps bypass bot-detection on server IPs when combined with cookies.
# Obtain with: https://github.com/YunzheZJU/youtube-po-token-generator
# Format for env var:  <po_token>   (just the token string)
PO_TOKEN               = os.environ.get("YTDLP_PO_TOKEN")
VISITOR_DATA           = os.environ.get("YTDLP_VISITOR_DATA")           # companion to PO_TOKEN


# ── Cookie bootstrap ──────────────────────────────────────────────────────────

def _write_temp_cookie(content: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    tmp.write(content)
    tmp.close()
    return tmp.name


# 1. Prefer base64-encoded content (most reliable on Render — avoids multiline mangling)
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
        print("[cookies] Detected cookie content pasted into YTDLP_COOKIES_FILE — writing to temp file")
        COOKIES_TEMP_FILE = _write_temp_cookie(COOKIES_FILE)
        COOKIES_FILE = COOKIES_TEMP_FILE
    else:
        print(f"[cookies] Warning: YTDLP_COOKIES_FILE path does not exist: {COOKIES_FILE}")
        COOKIES_FILE = None
elif COOKIES_FILE:
    print(f"[cookies] Using YTDLP_COOKIES_FILE: {COOKIES_FILE}")

if PO_TOKEN:
    print("[auth] PO token provided — will attach to youtube extractor args")
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
    # `tv_embedded` bypasses the bot-check gate on datacenter IPs.
    # `android` is the best source for full format lists (audio-only streams).
    # `web` is the final safety net.
    # Order matters: yt-dlp uses the first client that succeeds for auth,
    # then merges format lists from all clients.
    ydl_opts.setdefault("extractor_args", {})
    ydl_opts["extractor_args"].setdefault("youtube", {})
    ydl_opts["extractor_args"]["youtube"]["player_client"] = ["tv_embedded", "android", "web"]

    # PO Token — attach when provided (greatly helps on server IPs)
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


# Format preference: explicit codec/container fallbacks so yt-dlp always
# finds something even when a client only exposes combined streams.
#   m4a  → best for direct mp3 conversion (AAC → MP3 is lossless-ish)
#   webm → Opus, still great quality
#   bestaudio → whatever audio-only stream is available
#   best  → last resort: combined video+audio (ffmpeg will still extract audio)
_FORMAT_PRIMARY  = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
_FORMAT_FALLBACK = "best"  # always available; ffmpeg extracts audio from it


def _attempt_download(ydl_opts: dict, url: str) -> None:
    """
    Try the primary format. If yt-dlp says the format is unavailable
    (which can happen when tv_embedded exposes a limited format list),
    automatically retry with the broadest possible selector.
    """
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        if "Requested format is not available" in str(exc):
            print(f"[format] Primary format unavailable for {url!r}, retrying with '{_FORMAT_FALLBACK}'")
            fallback_opts = {**ydl_opts, "format": _FORMAT_FALLBACK}
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                ydl.download([url])
        else:
            raise


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
                download_jobs[job_id]["songs"][i]["status"]   = "done"
                download_jobs[job_id]["songs"][i]["progress"]  = 100
                download_jobs[job_id]["songs"][i]["folder"]    = artist

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