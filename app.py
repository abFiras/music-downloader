import os
import re
import shutil
import threading
from flask import Flask, render_template, request, jsonify
import yt_dlp

app = Flask(__name__, static_folder="images", static_url_path="/images")
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

download_jobs = {}
jobs_lock = threading.Lock()


def sanitize(name):
    """Remove characters not safe for folder/file names."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip() or "Unknown"


def make_progress_hook(job_id, song_index):
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


def resolve_job(job_id, urls):
    resolved_songs = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info:
                    for entry in info["entries"]:
                        if entry:
                            resolved_songs.append({
                                "url": entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry['id']}",
                                "title": entry.get("title", "Unknown"),
                                "artist": entry.get("uploader") or entry.get("channel") or "Unknown Artist",
                                "status": "queued",
                                "progress": 0,
                                "selected": True,
                            })
                else:
                    resolved_songs.append({
                        "url": url,
                        "title": info.get("title", "Unknown"),
                        "artist": info.get("uploader") or info.get("channel") or "Unknown Artist",
                        "status": "queued",
                        "progress": 0,
                        "selected": True,
                    })
        except Exception as e:
            resolved_songs.append({
                "url": url,
                "title": url,
                "artist": "Unknown Artist",
                "status": "error",
                "progress": 0,
                "selected": False,
                "error": str(e),
            })

    with jobs_lock:
        download_jobs[job_id]["songs"] = resolved_songs
        download_jobs[job_id]["resolved"] = True
        download_jobs[job_id]["status"] = "ready"


def download_job(job_id):
    with jobs_lock:
        songs = download_jobs[job_id]["songs"]

    for i, song in enumerate(songs):
        with jobs_lock:
            selected = download_jobs[job_id]["songs"][i].get("selected", True)
            current_status = download_jobs[job_id]["songs"][i]["status"]

        if not selected or current_status in ("done", "error"):
            continue

        with jobs_lock:
            download_jobs[job_id]["songs"][i]["status"] = "downloading"

        # Organize by artist folder
        artist = sanitize(song.get("artist", "Unknown Artist"))
        artist_folder = os.path.join(DOWNLOAD_FOLDER, artist)
        os.makedirs(artist_folder, exist_ok=True)

        has_ffmpeg = bool(shutil.which("ffmpeg") or shutil.which("ffmpeg.exe"))
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(artist_folder, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [make_progress_hook(job_id, i)],
        }
        if has_ffmpeg:
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                },
                {
                    "key": "FFmpegMetadata",
                    "add_metadata": True,
                },
            ]
        else:
            ydl_opts["noplaylist"] = True

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([song["url"]])
            with jobs_lock:
                download_jobs[job_id]["songs"][i]["status"] = "done"
                download_jobs[job_id]["songs"][i]["progress"] = 100
                download_jobs[job_id]["songs"][i]["folder"] = artist
        except Exception as e:
            with jobs_lock:
                download_jobs[job_id]["songs"][i]["status"] = "error"
                download_jobs[job_id]["songs"][i]["error"] = str(e)

    with jobs_lock:
        download_jobs[job_id]["status"] = "done"


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
            "status": "resolving",
            "resolved": False,
            "songs": [],
        }

    thread = threading.Thread(target=resolve_job, args=(job_id, urls), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    job_id = data.get("job_id")
    selected_indices = data.get("selected", [])

    with jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        for i, song in enumerate(job["songs"]):
            song["selected"] = i in selected_indices
            if song["selected"] and song["status"] not in ("done",):
                song["status"] = "queued"
                song["progress"] = 0
        job["status"] = "downloading"

    thread = threading.Thread(target=download_job, args=(job_id,), daemon=True)
    thread.start()
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