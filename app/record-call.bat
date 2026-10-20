@echo off
REM Records the current output device + microphone, then builds the two-track mix.
REM Stop with Ctrl+C in this window. The supervisor transcribes the session afterwards.
cd /d "%~dp0"
python audiotap.py --mic --name call %*
echo.
pause
