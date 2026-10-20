#!/usr/bin/env python3
r"""
Shared paths and settings for the AudioTap scripts. The desktop widget reads the same files.

  working files   %LOCALAPPDATA%\AudioTap\   status.json, last.json, logs, locks, config.json
  recordings      <repo>\recordings          unless config.json sets "recordings_dir"

config.json is created with these defaults by the supervisor on its first start. Every key
is optional; a key that is missing or empty falls back to the default below.
"""

import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)

_LOCAL = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
DATA_DIR = os.path.join(_LOCAL, "AudioTap")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    # where sessions are written; empty = a "recordings" folder next to the app
    "recordings_dir": "",
    # transcription: any faster-whisper model name (tiny, base, small, medium, large-v3 ...)
    "transcribe_model": "large-v3",
    # auto = CUDA when it works, otherwise CPU; or force "cuda" / "cpu"
    "transcribe_device": "auto",
    # empty = float16 on CUDA, int8 on CPU; other values: float32, int8_float16 ...
    "transcribe_compute_type": "",
    # empty = detect the language per segment; or a code such as "en", "de", "es"
    "language": "",
    # speaker labels in the transcript
    "label_remote": "Remote",
    "label_you": "You",
    # full path to ffmpeg.exe; empty = look it up on PATH
    "ffmpeg": "",
    # supervisor
    "poll_seconds": 20,
    "min_transcribe_mb": 10,     # sessions below this size are skipped (clicks and silence)
    "sweep_min_age_s": 120,      # a session must be this old before the sweep claims it
    "retain_days": 0,            # 0 = never delete anything; N = delete audio N days after a transcript exists
}


def read_json(path):
    # utf-8-sig: PowerShell's Set-Content -Encoding UTF8 stamps a BOM, which a plain
    # utf-8 json.load rejects.
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _same_kind(value, default):
    if isinstance(default, str):
        return isinstance(value, str)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_config():
    """DEFAULTS overlaid with config.json. Never writes.

    A value of the wrong type (null, text where a number belongs) is ignored, so a typo in
    the file cannot stop the supervisor; poll_seconds is kept inside a sane range.
    """
    cfg = dict(DEFAULTS)
    stored = read_json(CONFIG_PATH)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in DEFAULTS and not _same_kind(value, DEFAULTS[key]):
                continue
            cfg[key] = value
    cfg["poll_seconds"] = min(max(cfg["poll_seconds"], 1), 60)
    return cfg


def recordings_dir(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    custom = str(cfg.get("recordings_dir") or "").strip()
    if custom:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(custom)))
    return os.path.join(ROOT, "recordings")
