@echo off
REM Lists the output taps (--device) and microphones (--mic) AudioTap can use.
cd /d "%~dp0"
python audiotap.py --list
echo.
pause
