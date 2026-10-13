@echo off
title AInet Stop

echo.
echo   Stopping AInet ...

for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000"') do (
  echo   Killing PID %%p
  taskkill /PID %%p /T /F > nul 2>&1
)

ping -n 3 127.0.0.1 > nul

netstat -ano | findstr "LISTENING" | findstr ":8000" > nul
if %errorlevel%==0 (
  echo.
  echo   [FAILED] Port 8000 still in use. Right-click this file
  echo            and choose "Run as administrator".
) else (
  echo.
  echo   Done. AInet stopped, port 8000 released.
)

echo.
echo   Press any key to close ...
pause > nul
