# BatchBeat

A local YouTube-to-audio batch downloader built with Flask and yt-dlp. Download multiple YouTube videos or playlists and convert them to MP3 audio files with a modern web interface.

## Features

- 🎵 **Batch Downloads** – Download multiple YouTube videos or entire playlists at once
- 🎨 **Modern Web UI** – Clean, responsive interface for easy file selection and management
- 📁 **Automatic Organization** – Downloads are automatically organized by artist folder
- 🔄 **Real-time Progress** – Watch download progress for each track in real-time
- 🛡️ **Local & Offline** – All processing happens locally; no cloud dependency
- 🚀 **Offline Installation** – Comes with bundled Python wheels in `.vendor` (no internet required)
- ✂️ **Smart Conversion** – Automatically converts to MP3 with metadata (when ffmpeg is available)

## System Requirements

- **Python** 3.8 or higher
- **FFmpeg** (optional but recommended for MP3 conversion and metadata)
  - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html) or install via package manager
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg` (Ubuntu/Debian) or equivalent for your distro

## Installation & Setup

This project includes pre-built Python wheel files in the `.vendor` directory, so you can install and run it without fetching packages from the internet.

### Windows

1. Open **PowerShell** in the project folder.
2. Run the startup script:
   ```powershell
   .\start.bat
   ```
3. The app will open at `http://localhost:5000` (or the next available port)

### Unix / macOS

1. Open a **terminal** in the project folder.
2. Make the script executable and run it:
   ```bash
   chmod +x run.sh
   ./run.sh
   ```
3. The app will open at `http://localhost:5000` (or the next available port)

## Usage

1. **Add URLs** – Paste YouTube video URLs or playlist links into the text area
2. **Resolve** – Click "Resolve" to fetch video details and check for errors
3. **Review** – See the list of videos with titles and artists; uncheck any you don't want
4. **Download** – Click "Download Selected" to start the batch process
5. **Find Your Files** – Downloads are saved in the `downloads/` folder organized by artist

### URL Examples

- Single video: `https://www.youtube.com/watch?v=dQw4w9WgXcQ`
- Playlist: `https://www.youtube.com/playlist?list=PLxxx...`
- Shorts: `https://www.youtube.com/shorts/xxxxx`

## Project Structure

```
music-downloader/
├── app.py                 # Flask application and download logic
├── requirements.txt       # Python dependencies
├── start.bat             # Windows startup script
├── run.sh                # Unix/macOS startup script
├── templates/
│   └── index.html        # Web UI
├── images/               # Static assets and favicon
├── downloads/            # Downloaded files (created automatically)
├── mobile-app/           # Expo mobile app workspace
│   ├── App.js
│   ├── package.json
│   └── README.md
└── README.md             # This file
```

## Configuration

The app runs on `http://localhost:5000` by default. Downloads are organized in the following structure:

```
downloads/
├── Artist Name 1/
│   ├── Song 1.mp3
│   └── Song 2.mp3
└── Artist Name 2/
    └── Song 3.mp3
```

## Free deployment

This app can be hosted on free Python web platforms such as Render, Railway, or Replit. It already includes:

- `requirements.txt` for Python dependencies
- `Procfile` for `gunicorn`
- `runtime.txt` to pin Python 3.11

### Recommended free deploy workflow

1. Push this repository to GitHub.
2. Sign in to Render.com or Railway.app.
3. Create a new Python web service.
4. Point the service to this GitHub repo.
5. Set the start command to:
   ```bash
   gunicorn app:app --bind 0.0.0.0:$PORT
   ```
6. Deploy.

> Note: Many free hosts use ephemeral storage. `downloads/` is created automatically, but files may not persist after the app restarts.
>
> Some YouTube videos require login/cookies to download. On Render, this means the app may need a cookies file to access those URLs.
>
> If you want to use cookies, set this environment variable in Render:
>
> ```bash
> YTDLP_COOKIES_FILE=/path/to/cookies.txt
> ```
>
> Then redeploy.
>
## Troubleshooting

### Downloads don't convert to MP3
- **Solution:** Install FFmpeg on your system. Downloads will still work without it, but files may remain in their original format.

### "No such file or directory" errors
- **Solution:** Ensure the project folder exists and the startup script has execute permissions (on Unix/macOS, run `chmod +x run.sh`)

### Port already in use
- **Solution:** The app will automatically try the next available port (5001, 5002, etc.)

### Invalid URL errors
- **Reason:** The URL may be private, age-restricted, or no longer available
- **Solution:** Check the error message in the app and try a different URL

## Notes

- The app will create a `downloads/` folder automatically when it runs
- If `ffmpeg` is not available on your system, downloads will still work, but audio files may remain in their original format instead of converting to MP3
- Downloads are processed sequentially to avoid overloading your system
- File names and artist folders are automatically sanitized to be compatible with all operating systems

## License

Feel free to use and modify this project for personal or commercial purposes.
