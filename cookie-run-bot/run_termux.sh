#!/data/data/com.termux/files/usr/bin/bash
# Cookie Run Bot - Termux launcher (Android). Uses Termux packages for NumPy/OpenCV.
# Package names per current Termux guidance: python-numpy (main repo), opencv-python (x11-repo).
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${TERMUX_VERSION:-}" ] && [ ! -d /data/data/com.termux ]; then
  echo "[ERROR] This script is for Termux. Use run_pc.bat or run_mac.sh on desktops."
  exit 1
fi

echo "Installing Termux packages..."
pkg update -y
pkg install -y x11-repo
pkg install -y python python-numpy opencv-python android-tools

# venv that can see the Termux-built numpy/opencv; do NOT pip install numpy/opencv here.
[ -x .venv/bin/python ] || python -m venv --system-site-packages .venv
VPY=.venv/bin/python
"$VPY" -m pip install -r scripts/requirements-termux.txt || {
  echo "[ERROR] pip install failed. If pydantic-core fails to build: pkg install rust, then re-run."
  exit 1
}
"$VPY" -c 'import cv2, numpy, fastapi, uvicorn, pydantic; print("imports OK: opencv", cv2.__version__, "numpy", numpy.__version__)' || {
  echo "[ERROR] Import check failed."; exit 1; }
[ -f frontend/icons/icon-192.png ] || "$VPY" scripts/make_icons.py

command -v adb >/dev/null || { echo "[ERROR] adb missing (pkg install android-tools)"; exit 1; }
cat <<'EOF'
Wireless ADB (same device): Settings > Developer options > Wireless debugging.
  1. "Pair device with pairing code": adb pair HOST:PAIR_PORT CODE   (or use the dashboard)
  2. adb connect HOST:CONNECT_PORT  (the port shown on the Wireless debugging screen, not the pairing port)
  3. adb devices -l   -> pick that serial in the dashboard.
Ports change between sessions; nothing is hardcoded. Shizuku is not supported in this version.
Select capture=adb and input=adb. For switching to the game on this same phone, set
"On dashboard disconnect" to "Keep running (background mode)" deliberately.
EOF
echo "Starting server: http://127.0.0.1:8765/"
exec "$VPY" -m backend.app --host 127.0.0.1 --port 8765 --no-hotkey "$@"
