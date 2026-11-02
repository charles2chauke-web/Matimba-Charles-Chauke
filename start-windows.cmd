@echo off
setlocal
cd /d "%~dp0"
set "CHARLES_PY=.venv-win312\Scripts\python.exe"
set "CHARLES_STAGE=Checking Python and Node.js"

where py >nul 2>nul
if errorlevel 1 goto need_python
py -3.12-64 -c "import platform,struct,sys; sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and platform.machine().lower() in ('amd64','x86_64') else 1)" >nul 2>nul
if errorlevel 1 goto need_python
where node >nul 2>nul
if errorlevel 1 goto need_node
where npm >nul 2>nul
if errorlevel 1 goto need_node
node -e "const v=process.versions.node.split('.').map(Number);process.exit(v[0]>22 || v[0]===22 && v[1]>=12 ? 0 : 1)" >nul 2>nul
if errorlevel 1 goto need_node

rem Do not reuse an older .venv created with another Python or architecture.
rem Application data and credentials are in data/, outside this environment.
if exist "%CHARLES_PY%" goto check_environment
set "CHARLES_STAGE=Creating the Windows Python 3.12 environment"
py -3.12-64 -m venv .venv-win312
if errorlevel 1 goto fail

:check_environment
"%CHARLES_PY%" -c "import platform,struct,sys; sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and platform.machine().lower() in ('amd64','x86_64') else 1)" >nul 2>nul
if errorlevel 1 goto wrong_environment
set "CHARLES_STAGE=Updating pip in the isolated environment"
"%CHARLES_PY%" -m pip install --upgrade --only-binary=:all: pip
if errorlevel 1 goto fail
set "CHARLES_STAGE=Installing prebuilt Python packages"
rem Refuse source compilation: no Fortran or C/C++ build tools are required.
"%CHARLES_PY%" -m pip install --only-binary=:all: -r requirements-lock.txt
if errorlevel 1 goto fail
"%CHARLES_PY%" -m pip check
if errorlevel 1 goto fail

pushd frontend
set "CHARLES_STAGE=Installing frontend dependencies"
call npm ci --no-audit --no-fund
if errorlevel 1 goto frontend_fail
set "CHARLES_STAGE=Building the dashboard"
call npm run build
if errorlevel 1 goto frontend_fail
popd

echo.
echo CHARLES FOOTBALL BETTING INTELLIGENCE SYSTEM
echo Private local address: http://127.0.0.1:8000
echo Keep this window open. Press Ctrl+C to stop.
set "CHARLES_STAGE=Starting the local server"
"%CHARLES_PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 goto fail
exit /b 0

:frontend_fail
popd
goto fail

:need_python
echo.
echo This launcher requires Python 3.12, Windows installer 64-bit x86-64.
echo Install it alongside your other Python versions, including the py launcher.
echo Official installer: https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
echo Then close this window and run start-windows.cmd again.
echo Do not install a Fortran compiler to fix this application.
pause
exit /b 1

:need_node
echo Install Node.js 22.12 or newer from https://nodejs.org/en/download
echo Then close this window and run start-windows.cmd again.
pause
exit /b 1

:wrong_environment
echo The .venv-win312 folder uses an incompatible Python installation.
echo Rename only that generated folder, then run this launcher again.
echo Keep your data folder unchanged.
pause
exit /b 1

:fail
echo.
echo Failed during: %CHARLES_STAGE%
echo Review the error above. The local web server has not started successfully.
echo Prebuilt-package installation failures must not be bypassed with source builds.
echo Your data folder has not been removed.
pause
exit /b 1
