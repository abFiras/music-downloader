#!/bin/sh
if [ ! -d ".vendor" ]; then
  echo "ERROR: Local vendor directory missing."
  echo "Please run: python -m pip download --dest .vendor Flask==3.1.3 yt-dlp==2026.3.17"
  exit 1
fi
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --no-index --find-links .vendor -r requirements.txt
python app.py
