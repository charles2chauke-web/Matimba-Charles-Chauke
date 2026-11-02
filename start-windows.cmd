@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.12 or newer, with the Windows Python launcher.
  pause
  exit /b 1
)
where npm >nul 2>nul
if errorlevel 1 (
  echo Install Node.js 22.12 or newer, then reopen this window.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
if errorlevel 1 goto fail
pushd frontend
call npm ci --no-audit --no-fund
if errorlevel 1 (popd & goto fail)
call npm run build
if errorlevel 1 (popd & goto fail)
popd
echo.
echo CHARLES FOOTBALL BETTING INTELLIGENCE SYSTEM
echo Private local address: http://127.0.0.1:8000
echo Keep this window open. Press Ctrl+C to stop.
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
exit /b %errorlevel%
:fail
echo Setup failed. Review the error above.
pause
exit /b 1
