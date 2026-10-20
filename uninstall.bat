@echo off
REM Removes AudioTap from Startup and stops the supervisor and the widget.
REM Recordings and settings are kept. A recording that is running right now is not touched.
REM Patterns are concatenated so this script's own powershell does not match itself.
setlocal
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AudioTapSupervisor.lnk" 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AudioTapWidget.lnk" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$wid = '*audiotap-wid' + 'get.ps1*'; $pf = Join-Path $env:LOCALAPPDATA 'AudioTap\supervisor.pid';" ^
  "if (Test-Path -LiteralPath $pf) { $sp = (Get-Content -LiteralPath $pf -Raw | ConvertFrom-Json).pid; $p = Get-Process -Id $sp -ErrorAction SilentlyContinue; if ($p -and $p.ProcessName -like 'python*') { Stop-Process -Id $sp -Force } };" ^
  "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -like $wid } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo AudioTap stopped and removed from Startup. Recordings and settings are kept.
ping -n 3 127.0.0.1 >nul
