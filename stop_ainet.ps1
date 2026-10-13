# Stop ainet: end the scheduled task and kill uvicorn python processes started from D:\AINET
schtasks /end /tn ainet 2>$null | Out-Null
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'uvicorn main:app' -and $_.ExecutablePath -like 'D:\AINET\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
"port 8000 listening: " + [bool](Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue)
