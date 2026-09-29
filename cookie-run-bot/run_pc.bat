@echo off
REM Cookie Run Bot - Windows launcher. Creates .venv, installs deps, starts the server on 127.0.0.1.
setlocal EnableExtensions
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3.10+ not found. Install from https://www.python.org/downloads/ and tick "Add to PATH".
  exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo [ERROR] Python 3.10 or newer is required. Tested version: 3.11.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [ERROR] venv creation failed. & exit /b 1 )
)
set "VPY=.venv\Scripts\python.exe"

echo Installing dependencies...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 ( echo [ERROR] pip upgrade failed. & exit /b 1 )
"%VPY%" -m pip install -r backend\requirements-pc.txt
if errorlevel 1 ( echo [ERROR] Dependency installation failed. & exit /b 1 )

"%VPY%" -c "import cv2, numpy, fastapi, uvicorn, pydantic, mss; print('imports OK: opencv', cv2.__version__)"
if errorlevel 1 ( echo [ERROR] Import check failed. & exit /b 1 )

if not exist "frontend\icons\icon-192.png" (
  "%VPY%" scripts\make_icons.py
  if errorlevel 1 ( echo [ERROR] Icon generation failed. & exit /b 1 )
)

REM Check ADB when an ADB backend is configured in the active or default profile.
"%VPY%" -c "import json,pathlib,sys; p=pathlib.Path('data/config.json'); p=p if p.exists() else pathlib.Path('profiles/default.json'); c=json.loads(p.read_text()); sys.exit(1 if 'adb' in (c['capture']['backend'], c['input']['backend']) else 0)"
if errorlevel 1 (
  where adb >nul 2>nul
  if errorlevel 1 (
    echo [WARN] ADB backend selected but 'adb' is not on PATH. Install Android platform-tools or set the ADB executable path in the dashboard.
  ) else (
    adb version
  )
)

echo.
echo Starting server: http://127.0.0.1:8765/   (Ctrl+C to stop; F12 = emergency stop)
"%VPY%" -m backend.app --host 127.0.0.1 --port 8765 %*
exit /b %errorlevel%
