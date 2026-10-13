param([string]$pat, [string]$excl = 'GET /api', [int]$n = 40)
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Get-Content D:\AINET\backend\_p2\server.log -Encoding UTF8 | Select-String -Pattern $pat | Select-String -NotMatch $excl | Select -First $n | ForEach { $_.Line.Substring(0, [Math]::Min(300, $_.Line.Length)) }
