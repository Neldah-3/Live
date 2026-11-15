#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
  echo "[Setup] Membuat Python virtual environment (.venv)..."
  python3 -m venv .venv
fi

echo "[Setup] Memastikan dependensi terbaru (yt-dlp & requests) terpasang..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet --upgrade -r requirements.txt

exec .venv/bin/python download_and_upload.py "$@"
