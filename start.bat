@echo off
setlocal
cd /d "%~dp0"

set NOBROWSER=0
if /I "%~1"=="--no-browser" set NOBROWSER=1

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

.venv\Scripts\python.exe -c "import hashlib,pathlib,sys; p=pathlib.Path('requirements.txt'); h=hashlib.sha256(p.read_bytes()).hexdigest(); s=pathlib.Path(r'.venv\requirements.stamp'); sys.exit(0 if s.exists() and s.read_text(encoding='utf-8').strip()==h else 1)"
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  .venv\Scripts\python.exe -c "import hashlib,pathlib; p=pathlib.Path('requirements.txt'); pathlib.Path(r'.venv\requirements.stamp').write_text(hashlib.sha256(p.read_bytes()).hexdigest(), encoding='utf-8')"
)

for /f "tokens=1,2" %%a in ('.venv\Scripts\python.exe -c "from src.settings import load_settings; s=load_settings(); print(s.host, s.port)"') do (
  set HOST=%%a
  set PORT=%%b
)

echo.
echo Starting Local Twitch Cohost at http://%HOST%:%PORT%
echo Overlay for OBS: http://%HOST%:%PORT%/overlay
echo.

if "%NOBROWSER%"=="0" start "" "http://%HOST%:%PORT%/"

set COHOST_BIND_HOST=%HOST%
python -m uvicorn src.main:app --host %HOST% --port %PORT%
