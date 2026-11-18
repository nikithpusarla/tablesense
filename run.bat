@echo off
REM TableSense launcher for Windows. Double-click, or run from a terminal.
REM   run.bat          -> start (address/port from config\config.yaml, server.port)
REM   set PORT=8077    -> then run.bat: override the port for this window
REM   run.bat test     -> run the test suite
REM   run.bat demo     -> build the static browser-only demo into dist\
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo [TableSense] Creating virtual environment...
  py -3.13 -m venv .venv 2>nul || py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || python -m venv .venv
  if not exist .venv\Scripts\python.exe (
    echo [TableSense] Could not create a Python virtual environment. Install Python 3.11-3.13 from python.org first.
    pause
    exit /b 1
  )
  .venv\Scripts\python -m pip install --upgrade pip
  echo [TableSense] Installing CPU-only PyTorch, about 200 MB...
  .venv\Scripts\python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
  echo [TableSense] Installing the remaining packages...
  .venv\Scripts\python -m pip install -r requirements.txt
)

if /i "%1"=="test" (
  .venv\Scripts\python -m pytest -q
  exit /b %errorlevel%
)
if /i "%1"=="demo" (
  .venv\Scripts\python tools\build_static.py
  exit /b %errorlevel%
)

echo.
echo [TableSense] Starting... open the address shown in the log below. Ctrl+C stops it.
echo [TableSense] Phones on the same Wi-Fi: http://YOUR-PC-IP:PORT  (find the IP with ipconfig)
echo.
.venv\Scripts\python -m backend
if errorlevel 1 (
  echo.
  echo [TableSense] Server stopped with an error. If the port is busy, run:  set PORT=8077  and then run.bat again
  pause
)
