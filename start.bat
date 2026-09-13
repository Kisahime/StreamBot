@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH. Install Python 3.11+ and try again.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo Starting Local Twitch Cohost at http://127.0.0.1:8080
echo Overlay for OBS: http://127.0.0.1:8080/overlay
echo.
python -m uvicorn src.main:app --host 127.0.0.1 --port 8080
