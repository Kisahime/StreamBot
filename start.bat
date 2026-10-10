@echo off
setlocal
cd /d "%~dp0"

set NOBROWSER=0
set EXTRAS=0
:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--no-browser" set NOBROWSER=1
if /I "%~1"=="--extras" set EXTRAS=1
shift
goto parse_args
:args_done

set "PY="
py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3.11"
) else (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python 3.11 or newer was not found. Install Python 3.11+ and try again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Failed to create the virtualenv.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

.venv\Scripts\python.exe -c "import hashlib,pathlib,sys; p=pathlib.Path('requirements.txt'); h=hashlib.sha256(p.read_bytes()).hexdigest(); s=pathlib.Path(r'.venv\requirements.stamp'); sys.exit(0 if s.exists() and s.read_text(encoding='utf-8').strip()==h else 1)"
if errorlevel 1 (
  echo Installing dependencies...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo pip install failed. Fix the error above and run start.bat again.
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -c "import hashlib,pathlib; p=pathlib.Path('requirements.txt'); pathlib.Path(r'.venv\requirements.stamp').write_text(hashlib.sha256(p.read_bytes()).hexdigest(), encoding='utf-8')"
  if errorlevel 1 (
    echo Could not record the install stamp. The next launch will retry pip.
    pause
    exit /b 1
  )
)

if "%EXTRAS%"=="1" (
  .venv\Scripts\python.exe -c "import hashlib,pathlib,sys; p=pathlib.Path('requirements-extras.txt'); h=hashlib.sha256(p.read_bytes()).hexdigest(); s=pathlib.Path(r'.venv\requirements-extras.stamp'); sys.exit(0 if s.exists() and s.read_text(encoding='utf-8').strip()==h else 1)"
  if errorlevel 1 (
    echo Installing optional extras...
    .venv\Scripts\python.exe -m pip install -r requirements-extras.txt
    if errorlevel 1 (
      echo pip install of extras failed. Fix the error above and run start.bat --extras again.
      pause
      exit /b 1
    )
    .venv\Scripts\python.exe -c "import hashlib,pathlib; p=pathlib.Path('requirements-extras.txt'); pathlib.Path(r'.venv\requirements-extras.stamp').write_text(hashlib.sha256(p.read_bytes()).hexdigest(), encoding='utf-8')"
    if errorlevel 1 (
      echo Could not record the extras install stamp. The next launch with --extras will retry pip.
      pause
      exit /b 1
    )
  )
)

for /f "tokens=1,2" %%a in ('.venv\Scripts\python.exe -c "from src.settings import load_settings; s=load_settings(); print(s.host, s.port)"') do (
  set HOST=%%a
  set PORT=%%b
)
if not defined HOST (
  echo Could not read server.host and server.port from config.yaml.
  pause
  exit /b 1
)

echo.
echo Starting Local Twitch Cohost at http://%HOST%:%PORT%
echo Overlay for OBS: http://%HOST%:%PORT%/overlay
echo.

if "%NOBROWSER%"=="0" start "" "http://%HOST%:%PORT%/"

set COHOST_BIND_HOST=%HOST%
.venv\Scripts\python.exe -m uvicorn src.main:app --host %HOST% --port %PORT%
if errorlevel 1 (
  echo The app exited with an error.
  pause
  exit /b 1
)
