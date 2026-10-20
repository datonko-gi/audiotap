# Changelog

## 1.0.0

First public release.

- Windows call recorder: WASAPI loopback of every output device plus the microphone, written as two separate WAV tracks and a stereo mix.
- Crash-safe WAV files: headers are updated on every chunk, and a supervisor repairs and finishes any session that has no transcript.
- Local transcription with faster-whisper: each track is transcribed on its own and merged on timestamps, so the speakers are separated by track. Model, device (CUDA or CPU), compute type, language and speaker labels are set in `config.json`; the CPU is used when CUDA is not available.
- Desktop widget (PowerShell and WinForms): record and stop, live timer and output level, open the last session, output device and microphone options.
- Supervisor and widget watch each other and start at login (`install.bat`, `uninstall.bat`).
- Optional retention: `retain_days` deletes old audio after the transcript exists. Off by default.
