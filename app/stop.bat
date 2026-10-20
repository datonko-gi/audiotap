@echo off
REM Stops a running AudioTap recording cleanly, so the WAV headers close and the mix is built.
cd /d "%~dp0"
python -c "import os, atconfig; st = atconfig.read_json(os.path.join(atconfig.DATA_DIR, 'status.json')) or {}; p = st.get('stop_file') or os.path.join(atconfig.recordings_dir(), 'STOP'); open(p, 'w').write('stop'); print('Stop signal written to ' + p)"
echo The recorder will close the files and build the mix.
timeout /t 3 >nul
