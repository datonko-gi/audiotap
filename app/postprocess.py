#!/usr/bin/env python3
"""
Finishes a session no matter how it ended: repairs headers if a crash left them short,
builds the mix if it is missing, runs the transcription, updates last.json.

Idempotent and lock-guarded, so the supervisor's sweep and a manual run cannot collide
or do the work twice. Safe to run on a session that finished cleanly: every step checks
whether its output already exists.

Usage:
  python postprocess.py --session 261019_1420_call [--force]
"""

import argparse
import datetime as dt
import json
import os
import re
import struct
import subprocess
import sys
import time

import atconfig

HERE = os.path.dirname(os.path.abspath(__file__))
RECORDS = atconfig.recordings_dir()
TECH = atconfig.DATA_DIR

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def log(msg):
    line = "[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)


def repair_header(path):
    """A hard-killed writer can leave RIFF sizes short. Sizes are recomputed from the
    file itself; audio data is never touched."""
    size = os.path.getsize(path)
    with open(path, "r+b") as fh:
        head = fh.read(44)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE" or head[36:40] != b"data":
            log("%s: nonstandard header, leaving alone" % os.path.basename(path))
            return False
        riff = struct.unpack("<I", head[4:8])[0]
        data = struct.unpack("<I", head[40:44])[0]
        if riff == size - 8 and data == size - 44:
            return False
        fh.seek(4)
        fh.write(struct.pack("<I", size - 8))
        fh.seek(40)
        fh.write(struct.pack("<I", size - 44))
        log("%s: header repaired (%d -> %d data bytes)" % (os.path.basename(path), data, size - 44))
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if sys.stdout is None:
        os.makedirs(TECH, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(TECH, "postprocess_%s.log" % args.session),
                                       "a", encoding="utf-8", buffering=1)

    sys.path.insert(0, HERE)
    from transcribe import locate_session
    base = locate_session(args.session)
    if not base:
        log("no such session: %s" % args.session)
        return 1

    # sessions that ended cleanly already carry their duration in the name; a crashed
    # one does not, so stamp it here before anything else derives names from the prefix.
    # A crashed multi-endpoint session has no merged _system.wav yet, only parts, so
    # take the duration from part0 then (they are all the same length).
    if not re.search(r"_\d+min\d{2}sec$", args.session):
        wav = os.path.join(base, args.session + "_system.wav")
        if not os.path.exists(wav):
            import glob as _g
            parts0 = sorted(_g.glob(os.path.join(base, args.session + "_system_part*.wav")))
            if parts0:
                wav = parts0[0]
        dur = int(os.path.getsize(wav) / 192000.0)
        new_session = "%s_%dmin%02dsec" % (args.session, dur // 60, dur % 60)
        # FOLDER first: an Explorer window sitting in the folder blocks the folder
        # rename with WinError 5. When files were renamed before that failure, the
        # session lost name consistency and the run bailed out. With the folder going
        # first, a lock aborts the stamp before anything is touched, and the session
        # simply keeps its unstamped name. Cosmetic loss only.
        try:
            if os.path.basename(base) == args.session:
                new_base = os.path.join(os.path.dirname(base), new_session)
                os.rename(base, new_base)
                base = new_base
            for old in list(os.listdir(base)):
                if old.startswith(args.session):
                    os.rename(os.path.join(base, old),
                              os.path.join(base, new_session + old[len(args.session):]))
            log("stamped duration: %s" % new_session)
            args.session = new_session
        except OSError as exc:
            log("duration stamp skipped (%s), continuing under the original names" % exc)
    sys_wav = os.path.join(base, args.session + "_system.wav")
    mic_wav = os.path.join(base, args.session + "_mic.wav")
    mix_wav = os.path.join(base, args.session + "_mix.wav")
    transcript = os.path.join(base, args.session + "_transcript.txt")
    lock = os.path.join(TECH, "pp_%s.lock" % args.session)

    def write_last():
        last = {
            "finished": dt.datetime.now().isoformat(timespec="seconds"),
            "stopped_by": "postprocess",
            "seconds": round(os.path.getsize(sys_wav) / 192000.0, 1),
            "sys_path": sys_wav,
            "mic_path": mic_wav if os.path.exists(mic_wav) else None,
            "mix_path": mix_wav if os.path.exists(mix_wav) else None,
            "transcript_path": transcript,
            "open_path": transcript,
            "silent": False,
            "device": "",
        }
        tmp = os.path.join(TECH, "last.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(last, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(TECH, "last.json"))

    # a crashed multi-endpoint session legitimately has no merged _system.wav yet, only
    # _system_part*.wav; the merge happens inside the locked block below. Bail out only
    # when NEITHER exists (this check once ran unconditionally and silently killed every
    # crash recovery).
    import glob as _g0
    if not os.path.exists(sys_wav) and \
            not _g0.glob(os.path.join(base, args.session + "_system_part*.wav")):
        log("no such session: %s" % sys_wav)
        return 1
    if os.path.exists(transcript) and not args.force:
        write_last()
        log("transcript already exists, refreshed last.json only")
        return 0
    if os.path.exists(lock):
        age = dt.datetime.now().timestamp() - os.path.getmtime(lock)
        if age < 3 * 3600:
            log("another postprocess holds the lock (%.0f s old), exiting" % age)
            return 0
        log("stale lock (%.0f s), taking over" % age)

    os.makedirs(TECH, exist_ok=True)
    with open(lock, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        # a hard-killed multi-endpoint recording leaves unmerged _system_part*.wav and
        # no _system.wav at all; finish the merge here
        if not os.path.exists(sys_wav):
            import glob as _glob
            parts = sorted(_glob.glob(os.path.join(base, args.session + "_system_part*.wav")))
            if parts:
                for p in parts:
                    repair_header(p)
                log("merging %d crashed parts -> %s" % (len(parts), os.path.basename(sys_wav)))
                from audiotap import merge_wavs
                merge_wavs(parts, sys_wav)
                for p in parts:
                    try:
                        os.remove(p)
                    except OSError:
                        pass

        repair_header(sys_wav)
        if os.path.exists(mic_wav):
            repair_header(mic_wav)

        if not os.path.exists(mix_wav) and os.path.exists(mic_wav):
            log("building mix")
            from audiotap import build_mix
            build_mix(sys_wav, mic_wav, mix_wav)
            log("mix done: %s" % mix_wav)

        log("transcribing")
        # model, device, language and labels are read from config.json by transcribe.py
        cmd = [sys.executable, os.path.join(HERE, "transcribe.py"), "--session", args.session]
        # hand the child our own stdout explicitly: without it CreateProcess gives a
        # pythonw child no std handles at all, its prints vanish, and this log shows
        # "transcribing" and then nothing for 20 minutes. utf-8 so a stray non-ASCII
        # character in a progress line cannot kill the run with an encode error.
        env = dict(os.environ, PYTHONIOENCODING="utf-8:replace")
        rc = subprocess.run(cmd, stdout=sys.stdout, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW, env=env).returncode
        # the artifact is the truth, not the exit code: ctranslate2's CUDA teardown can
        # return nonzero after the transcript was already written in full
        ok = False
        for _ in range(5):
            if os.path.exists(transcript):
                ok = True
                break
            time.sleep(2)
        if not ok:
            log("FAILED: transcribe rc=%s and no transcript on disk" % rc)
            return 2
        log("transcribe done (rc=%s, transcript present)" % rc)
        write_last()

        # a status file still claiming this session is a corpse from a crash
        status = os.path.join(TECH, "status.json")
        try:
            with open(status, encoding="utf-8-sig") as fh:
                st = json.load(fh)
            if st.get("sys_path") == sys_wav:
                os.replace(status, os.path.join(TECH, "status_stale_%s.json" % args.session))
                log("archived stale status.json")
        except (OSError, ValueError):
            pass

        log("DONE: %s" % transcript)
        return 0
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
