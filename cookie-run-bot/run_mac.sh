#!/usr/bin/env bash
# Cookie Run Bot - macOS launcher (PyDirectInput excluded; keyboard backend is Windows-only).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "[ERROR] python3 not found (brew install python@3.11)"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { echo "[ERROR] Python 3.10+ required (tested: 3.11)"; exit 1; }

[ -x .venv/bin/python ] || "$PY" -m venv .venv
VPY=.venv/bin/python
"$VPY" -m pip install --upgrade pip
"$VPY" -m pip install -r backend/requirements-pc.txt   # pydirectinput is skipped by its win32 marker
"$VPY" -c 'import cv2, numpy, fastapi, uvicorn, pydantic, mss; print("imports OK: opencv", cv2.__version__)'
[ -f frontend/icons/icon-192.png ] || "$VPY" scripts/make_icons.py

if ! command -v adb >/dev/null; then
  echo "[INFO] adb not found. For ADB capture/input: brew install --cask android-platform-tools"
fi

cat <<'EOF'
[INFO] Desktop capture (MSS) needs Screen Recording permission:
       System Settings > Privacy & Security > Screen Recording > enable your terminal app, then restart it.
       Without it, captures are black or show only the wallpaper.
EOF

echo "Starting server: http://127.0.0.1:8765/  (Ctrl+C to stop)"
exec "$VPY" -m backend.app --host 127.0.0.1 --port 8765 --no-hotkey "$@"
