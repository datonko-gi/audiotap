#!/usr/bin/env python3
"""
Waits for a running recorder to finish, then starts the transcription by itself.

Used for recordings started without --transcribe (or started by hand), so the text is
ready without anyone remembering to launch it.

INVISIBLE BY CONSTRUCTION. It waits on a real process handle instead of polling, because
calling tasklist from a console-less process opens a black window on every single poll.
Every child process is created with CREATE_NO_WINDOW for the same reason. Nothing here
may ever flash on the desktop: the user is usually on the call being recorded.

Usage:
  pythonw watch_and_transcribe.py --pid 11076 --session 261019_1420_call
"""

import argparse
import ctypes
import datetime as dt
import os
import subprocess
import sys
import time

import atconfig

HERE = os.path.dirname(os.path.abspath(__file__))
TECH = atconfig.DATA_DIR

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102


def wait_for_exit(pid, timeout_s):
    """Block on the process handle. No polling, no console, no window."""
    if os.name != "nt":
        while timeout_s > 0:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(5)
            timeout_s -= 5
        return False

    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return True                      # already gone
    try:
        left = timeout_s
        while left > 0:
            chunk = min(60.0, left)
            if k32.WaitForSingleObject(handle, int(chunk * 1000)) != WAIT_TIMEOUT:
                return True
            left -= chunk
        return False
    finally:
        k32.CloseHandle(handle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--timeout", type=float, default=8 * 3600)
    args = ap.parse_args()

    os.makedirs(TECH, exist_ok=True)
    log = open(os.path.join(TECH, "watch_and_transcribe.log"), "a", encoding="utf-8", buffering=1)

    def say(msg):
        log.write("[%s] %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), msg))

    say("watching pid %d for session %s" % (args.pid, args.session))
    t0 = time.time()
    if not wait_for_exit(args.pid, args.timeout):
        say("timeout, giving up")
        return 1
    say("recorder exited after %.1f min, starting transcription" % ((time.time() - t0) / 60))

    time.sleep(10)                        # let the mix finish and the handles close
    python = sys.executable
    # postprocess.py, not transcribe.py: it also repairs headers and builds a missing mix
    cmd = [python, os.path.join(HERE, "postprocess.py"), "--session", args.session]
    out = os.path.join(TECH, "transcribe_%s.log" % args.session)
    with open(out, "w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW).returncode
    say("transcription finished rc=%s, log %s" % (rc, out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
