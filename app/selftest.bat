@echo off
REM Plays a short 440 Hz tone into the current output device and proves the tap catches it.
REM The tone is audible. Do not run this during a call.
cd /d "%~dp0"
python audiotap.py --selftest
echo.
pause
