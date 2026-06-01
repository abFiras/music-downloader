# BatchBeat

A local YouTube-to-audio batch downloader built with Flask and yt-dlp.

## Run locally

This project includes the required Python wheel files in `.vendor`, so it can be installed and run without fetching packages from the internet.

### Windows

1. Open PowerShell in the project folder.
2. Run:
   ```powershell
   .\start.bat
   ```

### Unix / macOS

1. Open a terminal in the project folder.
2. Run:
   ```bash
   chmod +x run.sh
   ./run.sh
   ```

## Notes

- The app will create a `downloads/` folder automatically when it runs.
- If `ffmpeg` is not available on the system, downloads will still work, but audio files may remain in their original format instead of converting to MP3.
