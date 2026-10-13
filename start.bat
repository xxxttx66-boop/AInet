@echo off
rem ainet start script (Windows)
cd /d "%~dp0backend"
set ENSP_HOST=127.0.0.1
start "" http://localhost:8000/
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
