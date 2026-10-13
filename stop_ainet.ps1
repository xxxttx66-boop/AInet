# Stop only this checkout's AInet Python process.
$ainetPython = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'backend\.venv\Scripts\python.exe'))
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.ExecutablePath -eq $ainetPython -and
    $_.CommandLine -match '(?:-m\s+launcher|-m\s+uvicorn\s+main:app|uvicorn\s+main:app)'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }
Write-Output 'AInet process stop completed for this checkout.'
