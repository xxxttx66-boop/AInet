@echo off
cd /d "%~dp0backend"
if not exist data mkdir data
.venv\Scripts\python.exe -m launcher >> data\server.log 2>&1
