@echo off
REM Installs AudioTap for the current user: the supervisor (pythonw, no window) and the
REM desktop widget go into the Startup folder and are started now. The supervisor keeps
REM the widget alive and transcribes finished recordings; the widget restarts the
REM supervisor if it dies. Run once, it survives reboots. Run it again after moving
REM this folder. No admin rights needed.
setlocal
set "HERE=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SUPLNK=%STARTUP%\AudioTapSupervisor.lnk"
set "WLNK=%STARTUP%\AudioTapWidget.lnk"

REM check Python and the three packages AudioTap imports, with the same interpreter that will run the supervisor
powershell -NoProfile -ExecutionPolicy Bypass -Command "$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source; if (-not $pyw) { exit 2 }; $py = $pyw -replace 'pythonw\.exe$','python.exe'; & $py -c 'import numpy, pyaudiowpatch, faster_whisper' 2>$null; if ($LASTEXITCODE -ne 0) { exit 3 }"
if errorlevel 3 goto nopackages
if errorlevel 2 goto nopython

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pyw = (Get-Command pythonw.exe).Source; $s = New-Object -ComObject WScript.Shell;" ^
  "$l = $s.CreateShortcut('%SUPLNK%'); $l.TargetPath = $pyw; $l.Arguments = '\"%HERE%app\supervisor.py\"'; $l.WorkingDirectory = '%HERE%app'; $l.WindowStyle = 7; $l.Save();" ^
  "$l2 = $s.CreateShortcut('%WLNK%'); $l2.TargetPath = '%HERE%widget\AudioTapWidget.vbs'; $l2.WorkingDirectory = '%HERE%widget'; $l2.Save()"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pyw = (Get-Command pythonw.exe).Source;" ^
  "$psi = New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName = $pyw;" ^
  "$psi.Arguments = '\"%HERE%app\supervisor.py\"'; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true;" ^
  "[System.Diagnostics.Process]::Start($psi) | Out-Null"

start "" wscript.exe "%HERE%widget\AudioTapWidget.vbs"

echo AudioTap installed: supervisor and widget are in Startup and running.
echo Recordings go to %HERE%recordings unless config.json says otherwise.
ping -n 4 127.0.0.1 >nul
exit /b 0

:nopython
echo Python was not found on PATH.
echo Install Python 3.9 to 3.13 from python.org, tick "Add python.exe to PATH", then run install.bat again.
pause
exit /b 1

:nopackages
echo The Python packages AudioTap needs are missing. Run this once, then run install.bat again:
echo     python -m pip install -r "%HERE%requirements.txt"
pause
exit /b 1
