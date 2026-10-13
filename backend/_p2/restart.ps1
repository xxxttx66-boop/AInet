Get-CimInstance Win32_Process | Where { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'server.bat' } | ForEach { taskkill /T /F /PID $_.ProcessId | Out-Null }
Get-CimInstance Win32_Process | Where { $_.Name -eq 'python.exe' -and $_.CommandLine -match '--port 8796' } | ForEach { taskkill /T /F /PID $_.ProcessId | Out-Null }
Start-Sleep 2
if (Get-NetTCPConnection -State Listen -LocalPort 8796 -ErrorAction SilentlyContinue) { "STILL LISTENING"; exit 1 }
if ($args[0] -ne 'stop') {
  $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="cmd.exe /c D:\AINET\backend\_p2\server.bat"; CurrentDirectory="D:\AINET\backend"}
  Start-Sleep 10
  Get-NetTCPConnection -State Listen -LocalPort 8796 | Select LocalAddress,OwningProcess
}
