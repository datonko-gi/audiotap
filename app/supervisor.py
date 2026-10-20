#!/usr/bin/env python3
"""
AudioTap supervisor: the one resident process that keeps the whole system alive.

Components used to live as orphan processes that nobody restarted: one relogin could kill
the recorder mid-call, leave its files unprocessed and make the widget vanish. Now
ownership is explicit:

    Startup (.lnk)  ->  supervisor.py (this file, pythonw, resident singleton)
                          |- keeps the widget alive          (restarts it if missing)
                          |- orphan sweep                    (crashed or unprocessed
                          |                                    sessions -> postprocess)
                          |- retention                       (optional, off by default)
                          |- stale status.json cleanup
                          '- heartbeat for the widget's reverse watchdog
    Startup (.lnk)  ->  AudioTapWidget.vbs -> audiotap-widget.ps1 (view/controller; if the
                                       heartbeat goes stale it restarts the supervisor:
                                       mutual watchdog)

Recording itself stays in audiotap.py, one process per recording; its WAV headers are
patched on every chunk, so even a hard kill leaves playable files, and the sweep here
turns them into a mix and a transcript within a couple of minutes of the crash.

INVISIBLE BY CONSTRUCTION: run with pythonw; every child gets CREATE_NO_WINDOW.
"""

import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time

import atconfig

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TECH = atconfig.DATA_DIR
STATUS = os.path.join(TECH, "status.json")
STATE = os.path.join(TECH, "supervisor_state.json")
CONFIG = atconfig.CONFIG_PATH
HEARTBEAT = os.path.join(TECH, "supervisor_heartbeat.txt")
# A supervisor that stops ticking is worse than one that dies: it keeps its pid, so
# every rescue launch exits as a duplicate and nothing ever notices. Past this many
# seconds without a completed tick the process is presumed wedged: it dumps its stacks
# and exits, and a newcomer is allowed to take the pidfile over.
STALL_S = 180
PIDFILE = os.path.join(TECH, "supervisor.pid")
LOG = os.path.join(TECH, "supervisor.log")
WIDGET_VBS = os.path.join(ROOT, "widget", "AudioTapWidget.vbs")
PYEXE_PIN = os.path.join(TECH, "python_exe.txt")

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED = 0x00000008 if os.name == "nt" else 0


def log(msg):
    os.makedirs(TECH, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("[%s] %s\n" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def read_json(path):
    # utf-8-sig: PowerShell's Set-Content -Encoding UTF8 stamps a BOM, and a plain
    # utf-8 json.load dies on it. That single byte-order mark once made load_config
    # think the file was corrupt and CLOBBER it with defaults.
    return atconfig.read_json(path)


def write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def pid_alive(pid):
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, int(pid))     # SYNCHRONIZE
    if not h:
        return False
    alive = k32.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT = still running
    k32.CloseHandle(h)
    return alive


def kill_pid(pid):
    """Terminate a wedged supervisor. Only ever called on a pid from our own pidfile."""
    if not pid or int(pid) == os.getpid():
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0001, False, int(pid))         # PROCESS_TERMINATE
    if not h:
        return False
    ok = bool(k32.TerminateProcess(h, 1))
    k32.CloseHandle(h)
    return ok


def heartbeat_age():
    try:
        return time.time() - os.path.getmtime(HEARTBEAT)
    except OSError:
        return float("inf")


def singleton_or_exit():
    old = read_json(PIDFILE) if os.path.exists(PIDFILE) else None
    if isinstance(old, dict) and pid_alive(old.get("pid")):
        # A live pid is NOT proof of a working supervisor: one that stops ticking holds
        # its pid until the machine reboots, and every relaunch would die right here,
        # silently, while recordings sit with no transcript. The heartbeat, not the
        # pid, says whether that process is doing the job.
        age = heartbeat_age()
        if age < STALL_S:
            sys.exit(0)                              # healthy incumbent, we are the dupe
        log("takeover: pid %s alive but heartbeat %.0f s stale, terminating it" % (
            old.get("pid"), age))
        kill_pid(old.get("pid"))
        time.sleep(1.0)
    write_json(PIDFILE, {"pid": os.getpid(), "started": dt.datetime.now().isoformat()})


_TICK = {"at": time.time()}


def start_stall_watchdog():
    """Turn an invisible wedge into a logged crash. A blocked syscall (network, disk,
    a child that never returns) cannot be interrupted from Python, so the only honest
    move is to record WHERE it is stuck and let the widget start a fresh process."""
    def watch():
        while True:
            time.sleep(5)
            stalled = time.time() - _TICK["at"]
            if stalled < STALL_S:
                continue
            try:
                import traceback
                lines = ["STALLED: no completed tick for %.0f s, stacks follow, exiting"
                         % stalled]
                for tid, frame in sys._current_frames().items():
                    lines.append("  thread %s:" % tid)
                    for entry in traceback.format_stack(frame):
                        lines.extend("    " + x for x in entry.rstrip().splitlines())
                log("\n".join(lines))
            except Exception:
                pass
            os._exit(1)
    threading.Thread(target=watch, daemon=True).start()


def run_hidden(cmd_list):
    return subprocess.Popen(cmd_list, creationflags=NO_WINDOW | DETACHED,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pythonw():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def write_pyexe_pin():
    """Interpreter pin: the absolute pythonw.exe path, written at every supervisor start
    so launchers stop trusting PATH first. A Microsoft Store "Python Install Manager"
    shim can sit ahead of the real interpreter in the login PATH and silently delegate
    to a Python without the packages AudioTap needs."""
    try:
        tmp = PYEXE_PIN + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(pythonw())
        os.replace(tmp, PYEXE_PIN)
    except OSError as exc:
        log("failed to write python_exe.txt: %s" % exc)


# ---------------------------------------------------------------- widget watchdog

def widget_alive():
    """Ask WMI through a hidden powershell; the caller does it only every third tick,
    because tasklist-style polling from a console-less process is what opens black
    windows on the desktop."""
    try:
        # the search pattern is built by concatenation so the checker's own command line
        # does not contain it verbatim: a literal pattern matches the checker itself,
        # which makes this probe report the widget alive forever
        psi_cmd = ("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
                   "Where-Object { $_.CommandLine -like ('*audiotap-wid' + 'get.ps1*') } | "
                   "Measure-Object | Select-Object -ExpandProperty Count")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", psi_cmd],
                             capture_output=True, text=True, timeout=30,
                             creationflags=NO_WINDOW).stdout.strip()
        return out.isdigit() and int(out) > 0
    except Exception:
        return True                                      # fail closed: do not spawn dupes


def ensure_widget():
    # respect a deliberate close: the widget's "Close widget" menu drops this flag so the
    # supervisor stops resurrecting it. A fresh intentional launch clears the flag again.
    if os.path.exists(os.path.join(TECH, "widget_closed.flag")):
        return
    if os.path.exists(WIDGET_VBS) and not widget_alive():
        log("widget missing, restarting")
        run_hidden(["wscript.exe", WIDGET_VBS])


# ---------------------------------------------------------------- config

def load_config():
    cfg = atconfig.load_config()
    if not os.path.exists(CONFIG):
        # seed defaults ONLY when there is no file at all. An existing file that
        # fails to parse must never be overwritten: that would destroy the user's settings.
        try:
            os.makedirs(TECH, exist_ok=True)
            write_json(CONFIG, atconfig.DEFAULTS)
        except OSError as exc:
            log("could not write config.json: %s" % exc)
    elif read_json(CONFIG) is None:
        log("config file exists but does not parse; running on defaults, NOT overwriting")
    try:
        cfg["_mtime"] = os.path.getmtime(CONFIG)
    except OSError:
        cfg["_mtime"] = 0
    return cfg


def maybe_reload_config(cfg):
    """Pick up edits to config.json without a restart."""
    try:
        if os.path.getmtime(CONFIG) != cfg.get("_mtime"):
            log("config reloaded")
            return load_config()
    except OSError:
        pass
    return cfg


def recorder_status():
    st = read_json(STATUS)
    if not st or st.get("state") != "recording":
        return None
    try:
        age = time.time() - os.path.getmtime(STATUS)
    except OSError:
        return None
    if age > 60:
        return None                                      # stale corpse, sweep handles it
    return st


# ---------------------------------------------------------------- orphan sweep

def iter_sessions():
    """(dir, session) pairs: one folder per session, renamed folders, flat files."""
    records = atconfig.recordings_dir()
    try:
        entries = os.listdir(records)
    except OSError:
        return
    for entry in entries:
        full = os.path.join(records, entry)
        if os.path.isdir(full):
            try:
                inner = os.listdir(full)
            except OSError:
                continue
            seen = set()
            for fn in inner:
                # a crashed multi-endpoint recording dies before merging its parts, so
                # a session may exist ONLY as _system_part0..N.wav; it must still be
                # swept, postprocess merges the parts
                if fn.endswith("_system.wav"):
                    seen.add(fn[:-len("_system.wav")])
                elif fn.endswith("_system_part0.wav"):
                    seen.add(fn[:-len("_system_part0.wav")])
            for session in seen:
                yield full, session
        elif entry.endswith("_system.wav"):
            yield records, entry[:-len("_system.wav")]


def sweep(cfg, state):
    """Any finished session without a transcript gets postprocessed, crashed or clean."""
    running = recorder_status()
    busy = running.get("sys_path", "") if running else ""
    for base, session in iter_sessions():
        sys_wav = os.path.join(base, session + "_system.wav")
        if sys_wav == busy:
            continue
        if os.path.exists(os.path.join(base, session + "_transcript.txt")):
            continue
        if session in state.get("pp_done", []):
            continue
        # a crashed session has no merged _system.wav yet, only _system_part*.wav;
        # judge its age and size by the parts then, postprocess does the merge
        probes = [sys_wav]
        if not os.path.exists(sys_wav):
            probes = [os.path.join(base, fn) for fn in os.listdir(base)
                      if fn.startswith(session + "_system_part") and fn.endswith(".wav")]
            if not probes:
                continue
        try:
            newest = max(os.path.getmtime(p) for p in probes)
            # min age before a session is claimed: it may still be closing
            if time.time() - newest < float(cfg.get("sweep_min_age_s", 120)):
                continue
            size_mb = max(os.path.getsize(p) for p in probes) / 1048576.0
        except (OSError, ValueError):
            continue
        if size_mb < cfg["min_transcribe_mb"]:
            state.setdefault("pp_done", []).append(session)
            log("sweep: %s is %.1f MB, below threshold, skipping" % (session, size_mb))
            continue
        lock_path = os.path.join(TECH, "pp_%s.lock" % session)
        if os.path.exists(lock_path):
            # a fresh lock = another postprocess owns the session; a stale one means
            # that owner died and must not block forever
            try:
                if time.time() - os.path.getmtime(lock_path) < 3 * 3600:
                    continue
                os.remove(lock_path)
                log("sweep: removed stale lock for %s" % session)
            except OSError:
                continue
        log("sweep: postprocessing %s (%.0f MB)" % (session, size_mb))
        out = open(os.path.join(TECH, "postprocess_%s.log" % session), "a", encoding="utf-8")
        subprocess.Popen([sys.executable, os.path.join(HERE, "postprocess.py"),
                          "--session", session],
                         stdout=out, stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW | DETACHED)
        state.setdefault("pp_done", []).append(session)
        return                                           # one at a time, the GPU is shared


def retention(cfg, state):
    """Optional: N days after a recording, the audio goes and the transcript stays.

    Off unless retain_days is set above 0 in config.json; nothing is ever deleted by
    default. Watchdog rules: audio is deleted ONLY after the transcript is verified
    present and non-trivial, and the deletion itself is verified afterwards. A big
    session without a transcript is never deleted, it is an alarm. Runs shortly after
    every start (the machine may have been off when the day passed) and every ~30 min
    after.
    """
    retain_days = float(cfg.get("retain_days") or 0)
    if retain_days <= 0:
        return
    now = time.time()
    running = recorder_status()
    busy = running.get("sys_path", "") if running else ""
    report = []
    for base, session in list(iter_sessions()):
        sys_wav = os.path.join(base, session + "_system.wav")
        if sys_wav == busy:
            continue
        try:
            age_days = (now - os.path.getmtime(sys_wav)) / 86400.0
        except OSError:
            continue
        if age_days < retain_days:
            continue
        audio = [os.path.join(base, session + suffix)
                 for suffix in ("_system.wav", "_mic.wav", "_mix.wav")]
        audio = [a for a in audio if os.path.exists(a)]
        total_mb = sum(os.path.getsize(a) for a in audio) / 1048576.0
        transcript = os.path.join(base, session + "_transcript.txt")

        ok_transcript = os.path.exists(transcript) and os.path.getsize(transcript) > 200
        if not ok_transcript:
            if total_mb < cfg["min_transcribe_mb"]:
                # a stub of clicks and silence: nothing worth keeping at all
                try:
                    for a in audio:
                        os.remove(a)
                    if not os.listdir(base) and os.path.basename(base) == session:
                        os.rmdir(base)
                    log("retention: stub %s removed (%.1f MB)" % (session, total_mb))
                except OSError as exc:
                    log("retention: stub %s removal failed: %s" % (session, exc))
            else:
                log("retention ALARM: %s is %.0f MB, %.1f days old and has NO transcript, "
                    "audio kept" % (session, total_mb, age_days))
            continue

        try:
            for a in audio:
                os.remove(a)
        except OSError as exc:
            log("retention: %s delete failed: %s" % (session, exc))
        # the watchdog part: verify the world is in the state we claim
        left = [a for a in audio if os.path.exists(a)]
        t_ok = os.path.exists(transcript) and os.path.getsize(transcript) > 200
        if not left and t_ok:
            log("retention OK: %s audio deleted (%.0f MB), transcript kept (%d bytes)"
                % (session, total_mb, os.path.getsize(transcript)))
            report.append({"session": session, "freed_mb": round(total_mb), "ok": True})
        else:
            log("retention FAILED: %s leftovers=%s transcript_ok=%s" % (session, left, t_ok))
            report.append({"session": session, "leftovers": left, "ok": False})
        # leftover 16 kHz ASR copies belong to the same session
        for cache in ("_asr_out.wav", "_asr_mic.wav"):
            try:
                os.remove(os.path.join(TECH, session + cache))
            except OSError:
                pass
    if report:
        write_json(os.path.join(TECH, "retention_report.json"),
                   {"ran": dt.datetime.now().isoformat(timespec="seconds"), "actions": report})


def clean_stale_status():
    st = read_json(STATUS)
    if not st:
        return
    try:
        age = time.time() - os.path.getmtime(STATUS)
    except OSError:
        return
    if age > 60 and not pid_alive(st.get("pid")):
        os.replace(STATUS, os.path.join(TECH, "status_stale.json"))
        log("archived stale status.json (age %.0f s, pid %s dead)" % (age, st.get("pid")))


# ---------------------------------------------------------------- main loop

def main():
    if sys.stdout is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    os.makedirs(TECH, exist_ok=True)
    singleton_or_exit()
    write_pyexe_pin()
    cfg = load_config()
    state = read_json(STATE) or {}
    log("supervisor up: pid %d, poll %ds, recordings in %s" % (
        os.getpid(), cfg["poll_seconds"], atconfig.recordings_dir(cfg)))

    start_stall_watchdog()
    tick = 0
    while True:
        tick += 1
        try:
            with open(HEARTBEAT, "w") as fh:
                fh.write(dt.datetime.now().isoformat())
            cfg = maybe_reload_config(cfg)
            clean_stale_status()
            if tick % 3 == 0:
                ensure_widget()
                sweep(cfg, state)
            # shortly after every start and roughly every 30 minutes afterwards
            if tick == 6 or tick % 90 == 0:
                retention(cfg, state)
            state["pp_done"] = state.get("pp_done", [])[-80:]
            write_json(STATE, state)
        except Exception as exc:
            log("tick failed: %r" % exc)
        # a tick that raised still counts as alive: the watchdog hunts wedges, not bugs
        _TICK["at"] = time.time()
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
