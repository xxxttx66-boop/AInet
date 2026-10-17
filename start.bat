@echo off
cd /d "%~dp0backend"
.venv\Scripts\python.exe -m launcher --open-browser
pause
