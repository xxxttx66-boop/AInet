@echo off
title AInet Server
cd /d "%~dp0backend"

netstat -ano | findstr "LISTENING" | findstr ":8000" > nul
if %errorlevel%==0 (
  echo.
  echo   AInet is already running. Opening browser ...
  ping -n 2 127.0.0.1 > nul
  start "" http://localhost:8000/
  exit /b
)

set ENSP_HOST=127.0.0.1
set AINET_ENSPRO_URL=https://172.16.133.138:8443
set AINET_ENSPRO_START_TIMEOUT=300
set AINET_LAB_PROVIDER=enspro
set AINET_ENSPRO_AUTO_RELOGIN=1
set AINET_AUTO_LOGIN=admin
set AINET_ENSPRO_MAX_DEVICES=3

echo.
echo   ==================================================
echo    Starting AInet ... please wait about 3 seconds
echo.
echo    KEEP THIS WINDOW OPEN.
echo    Closing this window = stopping the service.
echo.
echo    Browser will open http://localhost:8000/
echo   ==================================================
echo.

start "" http://localhost:8000/
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000

echo.
echo   Service stopped. Press any key to close ...
pause > nul
