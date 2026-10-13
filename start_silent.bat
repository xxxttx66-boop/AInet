@echo off
title AInet Server (background)
cd /d "%~dp0backend"
set ENSP_HOST=127.0.0.1
set AINET_ENSPRO_URL=https://172.16.133.138:8443
set AINET_ENSPRO_START_TIMEOUT=300
set AINET_LAB_PROVIDER=enspro
set AINET_ENSPRO_AUTO_RELOGIN=1
set AINET_AUTO_LOGIN=admin
set AINET_ENSPRO_MAX_DEVICES=3
if not exist data mkdir data
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 >> data\server.log 2>&1
