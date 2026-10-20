#!/usr/bin/env python3
"""
AudioTap - captures the audio stream that Windows sends to an output device.

Method: WASAPI loopback. The app taps the digital render mix of the endpoint
(headphones or speakers), so it records what you hear, not what a microphone
picks up from the room. Zoom / Meet / Teams remote voices, YouTube, system
sounds - everything that is mixed into that device.

Optionally records the microphone in parallel and builds a two-track mix
(left = what you heard, right = what you said), which is what you want for
call transcription and speaker separation.

Console output is ASCII only on purpose: legacy Windows console code pages
break non-ASCII text when the output is piped. Docs are in README.md.
"""

import argparse
import ctypes
import datetime as dt
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import pyaudiowpatch as pyaudio

import atconfig

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = atconfig.recordings_dir()
TECH_DIR = atconfig.DATA_DIR
CHUNK = 2048
FMT = pyaudio.paInt16
SAMPWIDTH = 2
_RECORDER_MUTEX = None      # held for the life of a recording process


def to_db(level):
    return 20.0 * math.log10(level) if level > 0 else -99.0


def write_json_atomic(path, payload):
    """The widget polls these files, so never leave a half-written one behind."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# ----------------------------------------------------------------------------- devices


def wasapi_info(pa):
    return pa.get_host_api_info_by_type(pyaudio.paWASAPI)


def loopback_devices(pa):
    return list(pa.get_loopback_device_info_generator())


def default_output(pa):
    return pa.get_device_info_by_index(wasapi_info(pa)["defaultOutputDevice"])


def resolve_loopback(pa, spec):
    """spec: None (system default output), device index, or a name substring."""
    lbs = loopback_devices(pa)
    if not lbs:
        raise RuntimeError("No WASAPI loopback devices found.")

    if spec is None:
        name = default_output(pa)["name"]
        for lb in lbs:
            if lb["name"].startswith(name) or name in lb["name"]:
                return lb
        raise RuntimeError("Loopback endpoint for default output '%s' not found." % name)

    if str(spec).isdigit():
        return pa.get_device_info_by_index(int(spec))

    hits = [lb for lb in lbs if str(spec).lower() in lb["name"].lower()]
    if not hits:
        raise RuntimeError("No loopback device matches '%s'. Run --list." % spec)
    if len(hits) > 1:
        print("[!] '%s' matches %d devices, taking the first one." % (spec, len(hits)))
    return hits[0]


def render_endpoint_index(pa, lb):
    """Index of the playback endpoint that a [Loopback] device belongs to."""
    base = lb["name"].replace(" [Loopback]", "")
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        api = pa.get_host_api_info_by_index(d["hostApi"])["name"]
        if api == "Windows WASAPI" and d["maxOutputChannels"] > 0 and d["name"] == base:
            return d["index"]
    return wasapi_info(pa)["defaultOutputDevice"]


class SilentRender:
    """Keeps the output endpoint awake.

    WASAPI loopback delivers no buffers at all while nothing is playing on the device,
    so idle stretches would vanish from the recording instead of being written as
    silence, and the recorded timeline would drift away from the wall clock. Pushing a
    stream of zeros into the same endpoint keeps buffers flowing. It is inaudible.
    """

    def __init__(self, pa, dev_index, rate, channels=2):
        self.channels = channels
        self.stream = pa.open(
            format=FMT,
            channels=channels,
            rate=rate,
            output=True,
            output_device_index=dev_index,
            frames_per_buffer=CHUNK,
            stream_callback=self._callback,
        )

    def _callback(self, in_data, frame_count, time_info, status):
        return (b"\x00" * (frame_count * self.channels * SAMPWIDTH), pyaudio.paContinue)

    def close(self):
        try:
            self.stream.stop_stream()
        finally:
            self.stream.close()


def resolve_mic(pa, spec):
    if spec in (None, "", "default"):
        idx = wasapi_info(pa)["defaultInputDevice"]
        if idx < 0:
            raise RuntimeError("No default WASAPI input device.")
        return pa.get_device_info_by_index(idx)

    if str(spec).isdigit():
        return pa.get_device_info_by_index(int(spec))

    hits = []
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["maxInputChannels"] > 0 and not d["name"].endswith("[Loopback]"):
            if str(spec).lower() in d["name"].lower():
                hits.append(d)
    if not hits:
        raise RuntimeError("No input device matches '%s'. Run --list." % spec)
    return hits[0]


def cmd_devices_json(pa):
    """Machine readable device list for the desktop widget."""
    mics = []
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["maxInputChannels"] > 0 and not d["name"].endswith("[Loopback]"):
            if pa.get_host_api_info_by_index(d["hostApi"])["name"] == "Windows WASAPI":
                mics.append({"index": d["index"], "name": d["name"]})
    payload = {
        "default_output": default_output(pa)["name"],
        "taps": [{"index": lb["index"], "name": lb["name"].replace(" [Loopback]", "")}
                 for lb in loopback_devices(pa)],
        "mics": mics,
    }
    # ASCII-escaped on purpose: the widget reads this through a pipe with a legacy code page
    print(json.dumps(payload, ensure_ascii=True))
    return 0


def cmd_list(pa):
    wi = wasapi_info(pa)
    print("Default output : %s" % default_output(pa)["name"])
    try:
        print("Default mic    : %s" % pa.get_device_info_by_index(wi["defaultInputDevice"])["name"])
    except Exception:
        print("Default mic    : none")

    print("\nOUTPUT TAPS (use with --device)")
    for lb in loopback_devices(pa):
        print("  [%2d] %-55s %d ch @ %d Hz" % (
            lb["index"], lb["name"], lb["maxInputChannels"], int(lb["defaultSampleRate"])))

    print("\nMICROPHONES (use with --mic)")
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["maxInputChannels"] > 0 and not d["name"].endswith("[Loopback]"):
            api = pa.get_host_api_info_by_index(d["hostApi"])["name"]
            if api == "Windows WASAPI":
                print("  [%2d] %-55s %d ch @ %d Hz" % (
                    d["index"], d["name"], d["maxInputChannels"], int(d["defaultSampleRate"])))


# ----------------------------------------------------------------------------- recorder


class Recorder:
    """One WASAPI stream -> one WAV file, written from a background thread."""

    def __init__(self, pa, dev, path, label):
        self.label = label
        self.path = path
        self.rate = int(dev["defaultSampleRate"])
        self.channels = min(2, int(dev["maxInputChannels"])) or 1
        self.device_name = dev["name"]
        self.level = 0.0
        self.peak = 0.0
        self.frames = 0
        self.overflows = 0
        self.error = None
        self._q = queue.Queue()
        self._stop = threading.Event()

        self.wf = wave.open(path, "wb")
        self.wf.setnchannels(self.channels)
        self.wf.setsampwidth(SAMPWIDTH)
        self.wf.setframerate(self.rate)

        self.stream = pa.open(
            format=FMT,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=CHUNK,
            stream_callback=self._callback,
        )
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.writer.start()

    def _callback(self, in_data, frame_count, time_info, status):
        if status:
            self.overflows += 1
        if self.error is None:
            self._q.put(in_data)
        buf = np.frombuffer(in_data, dtype=np.int16)
        if buf.size:
            rms = float(np.sqrt(np.mean(np.square(buf.astype(np.float32))))) / 32768.0
            self.level = rms
            self.peak = max(self.peak, float(np.max(np.abs(buf))) / 32768.0)
        return (None, pyaudio.paContinue)

    def _write_loop(self):
        while True:
            item = self._q.get()
            if item is None:
                break
            if self.error is not None:
                continue                      # keep draining, so the queue cannot grow
            try:
                self.wf.writeframes(item)
            except Exception as exc:          # disk full, I/O error, WAV size limit
                self.error = repr(exc)
                continue
            self.frames += len(item) // (SAMPWIDTH * self.channels)

    def start(self):
        self.stream.start_stream()

    def stop(self):
        try:
            self.stream.stop_stream()
        finally:
            self.stream.close()
        self._q.put(None)
        self.writer.join(timeout=10)
        try:
            self.wf.close()
        except Exception as exc:
            if self.error is None:
                self.error = repr(exc)

    @property
    def seconds(self):
        return self.frames / float(self.rate) if self.rate else 0.0


def bar(level, width=22):
    if level <= 0:
        db = -99.0
    else:
        db = 20.0 * math.log10(level)
    filled = int(max(0.0, min(1.0, (db + 60.0) / 60.0)) * width)
    return "[%s%s] %5.1f dB" % ("#" * filled, "." * (width - filled), db)


# ----------------------------------------------------------------------------- mixing


def read_wav_mono(path):
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        ch = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, rate


def resample_linear(data, src_rate, dst_rate):
    if src_rate == dst_rate or data.size == 0:
        return data
    n_out = int(round(data.size * dst_rate / float(src_rate)))
    x_old = np.arange(data.size, dtype=np.float64)
    x_new = np.linspace(0.0, data.size - 1.0, n_out)
    return np.interp(x_new, x_old, data).astype(np.float32)


def build_mix(sys_path, mic_path, out_path, mic_gain=1.0, mode="stereo"):
    sys_d, sys_rate = read_wav_mono(sys_path)
    mic_d, mic_rate = read_wav_mono(mic_path)
    mic_d = resample_linear(mic_d, mic_rate, sys_rate) * float(mic_gain)

    n = max(sys_d.size, mic_d.size)
    sys_d = np.pad(sys_d, (0, n - sys_d.size))
    mic_d = np.pad(mic_d, (0, n - mic_d.size))

    if mode == "mono":
        mixed = np.clip((sys_d + mic_d) * 0.7, -1.0, 1.0)
        out = (mixed * 32767.0).astype(np.int16)
        channels = 1
    else:
        stereo = np.stack([np.clip(sys_d, -1, 1), np.clip(mic_d, -1, 1)], axis=1)
        out = (stereo * 32767.0).astype(np.int16)
        channels = 2

    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPWIDTH)
        wf.setframerate(sys_rate)
        wf.writeframes(out.tobytes())
    return out_path


# ----------------------------------------------------------------------------- self test


def play_tone(pa, dev_index, rate, seconds=1.5, freq=440.0, amp=0.15):
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    fade = np.minimum(1.0, np.minimum(t * 20.0, (seconds - t) * 20.0)).astype(np.float32)
    mono = (np.sin(2 * np.pi * freq * t) * amp * fade).astype(np.float32)
    stereo = np.stack([mono, mono], axis=1)
    pcm = (stereo * 32767.0).astype(np.int16).tobytes()
    out = pa.open(format=FMT, channels=2, rate=rate, output=True, output_device_index=dev_index)
    out.write(pcm)
    out.stop_stream()
    out.close()


def dominant_freq(path):
    data, rate = read_wav_mono(path)
    if data.size < rate // 4:
        return 0.0, 0.0
    seg = data[int(data.size * 0.25):int(data.size * 0.75)]
    if seg.size == 0:
        return 0.0, 0.0
    windowed = seg * np.hanning(seg.size)
    spec = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(seg.size, 1.0 / rate)
    peak = float(freqs[int(np.argmax(spec))])
    rms = float(np.sqrt(np.mean(np.square(seg))))
    return peak, rms


def cmd_selftest(pa, args):
    lb = resolve_loopback(pa, args.device)
    endpoint_index = None
    base_name = lb["name"].replace(" [Loopback]", "")
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        api = pa.get_host_api_info_by_index(d["hostApi"])["name"]
        if api == "Windows WASAPI" and d["maxOutputChannels"] > 0 and d["name"] == base_name:
            endpoint_index = d["index"]
            break
    if endpoint_index is None:
        endpoint_index = wasapi_info(pa)["defaultOutputDevice"]

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "_selftest.wav")
    print("Self test: tapping '%s', playing a 440 Hz tone into '%s'" % (lb["name"], base_name))

    rec = Recorder(pa, lb, path, "sys")
    rec.start()
    time.sleep(0.4)
    play_tone(pa, endpoint_index, rec.rate)
    time.sleep(0.4)
    rec.stop()

    peak, rms = dominant_freq(path)
    print("Captured %.2f s, dominant freq %.1f Hz, rms %.4f" % (rec.seconds, peak, rms))
    ok = rms > 0.002 and abs(peak - 440.0) < 15.0
    print("RESULT: %s" % ("PASS - loopback capture works" if ok else "FAIL - no tone captured"))
    print("File: %s" % path)
    return 0 if ok else 2


# ----------------------------------------------------------------------------- record


def merge_wavs(paths, dst):
    """Sum several equal-rate wavs into one, chunked so a gigabyte never sits in RAM.

    Exists because a call does not always play through the system default output: the
    far side can go to another endpoint and be lost. Now every endpoint is recorded and
    the tracks are summed here; wherever the audio went, it is in the mix.
    """
    readers = [wave.open(p, "rb") for p in paths]
    rate = readers[0].getframerate()
    ch = readers[0].getnchannels()
    keep = [r for r in readers if r.getframerate() == rate and r.getnchannels() == ch]
    if len(keep) < len(readers):
        print("[!] merge: %d tap(s) skipped, their sample rate or channel count differs from the first one"
              % (len(readers) - len(keep)))
    total = max(r.getnframes() for r in keep)
    out = wave.open(dst, "wb")
    out.setnchannels(ch)
    out.setsampwidth(SAMPWIDTH)
    out.setframerate(rate)
    block_frames = rate * 2
    done = 0
    while done < total:
        want = min(block_frames, total - done)
        acc = np.zeros(want * ch, dtype=np.int32)
        for r in keep:
            raw = r.readframes(want)
            if raw:
                a = np.frombuffer(raw, dtype=np.int16).astype(np.int32)
                acc[:a.size] += a
        out.writeframes(np.clip(acc, -32768, 32767).astype(np.int16).tobytes())
        done += want
    out.close()
    for r in readers:
        r.close()


def cmd_record(pa, args):
    # one recorder at a time, whatever the status file says: a double click on the widget
    # starts two processes within the same second, long before either writes a status file
    global _RECORDER_MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _RECORDER_MUTEX = k32.CreateMutexW(None, False, "Local\\AudioTapRecorder")
    if ctypes.get_last_error() == 183:        # ERROR_ALREADY_EXISTS
        print("ERROR: another recording is already running")
        return 1
    # no explicit device: tap EVERY output endpoint, not just the default. Calls do not
    # reliably play through the default device (a browser or conferencing app can pick
    # another endpoint, and the far side of the call would be lost).
    if args.device:
        taps = [resolve_loopback(pa, args.device)]
    else:
        taps = loopback_devices(pa)
        if not taps:
            raise RuntimeError("No WASAPI loopback devices found.")
    mic_dev = resolve_mic(pa, args.mic) if args.mic is not None else None

    # session names look like 260813_1726 (yymmdd_HHMM), optionally followed by --name
    stamp = dt.datetime.now().strftime("%y%m%d_%H%M")
    if args.name:
        prefix = "%s_%s" % (stamp, args.name)
    else:
        prefix = stamp
    # every session gets its own folder, otherwise the recordings folder grows into a
    # thousand-file pile
    out_dir = os.path.join(args.out, prefix)
    if os.path.exists(out_dir) and os.listdir(out_dir):
        # two recordings inside one minute: disambiguate with seconds
        stamp = dt.datetime.now().strftime("%y%m%d_%H%M%S")
        prefix = "%s_%s" % (stamp, args.name) if args.name else stamp
        out_dir = os.path.join(args.out, prefix)
    os.makedirs(out_dir, exist_ok=True)
    sys_path = os.path.join(out_dir, prefix + "_system.wav")
    if len(taps) == 1:
        part_paths = [sys_path]
    else:
        part_paths = [os.path.join(out_dir, prefix + "_system_part%d.wav" % i)
                      for i in range(len(taps))]
    mic_path = os.path.join(out_dir, prefix + "_mic.wav") if mic_dev else None

    for lb in taps:
        print("Output tap : %s  (%d ch @ %d Hz)" % (
            lb["name"], min(2, lb["maxInputChannels"]), int(lb["defaultSampleRate"])))
    if mic_dev:
        print("Microphone : %s  (%d ch @ %d Hz)" % (
            mic_dev["name"], min(2, mic_dev["maxInputChannels"]), int(mic_dev["defaultSampleRate"])))
    print("Writing    : %s" % sys_path)
    if mic_path:
        print("             %s" % mic_path)

    stop_path = args.stop_file or os.path.join(args.out, "STOP")
    if os.path.exists(stop_path):
        os.remove(stop_path)
    print("Stop with  : Ctrl+C, or create the file %s" % stop_path)
    if args.seconds:
        print("Hard limit : %s s" % args.seconds)
    print("")

    keepalives = []
    if not args.no_keepalive:
        for lb in taps:
            try:
                keepalives.append(SilentRender(pa, render_endpoint_index(pa, lb),
                                               int(lb["defaultSampleRate"])))
            except Exception as exc:
                print("[!] Keep-alive failed for %s (%s)" % (lb["name"], exc))

    sys_recs = []
    for i, lb in enumerate(taps):
        try:
            sys_recs.append(Recorder(pa, lb, part_paths[i], "sys%d" % i))
        except Exception as exc:
            print("[!] Tap failed for %s (%s), continuing without it" % (lb["name"], exc))
    if not sys_recs:
        raise RuntimeError("No output endpoint could be opened.")
    mic_rec = Recorder(pa, mic_dev, mic_path, "mic") if mic_dev else None
    recs = sys_recs + ([mic_rec] if mic_rec else [])
    for r in recs:
        r.start()

    def sys_level():
        return max(r.level for r in sys_recs)

    def sys_peak():
        return max(r.peak for r in sys_recs)

    def loudest_name():
        best = max(sys_recs, key=lambda r: r.level)
        return best.device_name.replace(" [Loopback]", "")

    status_path = args.status_file or os.path.join(TECH_DIR, "status.json")
    last_path = os.path.join(os.path.dirname(status_path), "last.json")

    # a crash or relogin leaves a status file claiming a recording that no longer exists;
    # this process holds the recorder mutex, so any status file is a ghost. Clear it so the
    # widget never shows a ghost session
    try:
        os.remove(status_path)
    except OSError:
        pass

    t0 = time.time()
    silent_warned = False
    stopped_by = "Ctrl+C"
    last_quiet = 0.0
    last_status = -1.0
    try:
        while True:
            elapsed = time.time() - t0
            if args.seconds and elapsed >= args.seconds:
                stopped_by = "time limit"
                break
            if os.path.exists(stop_path):
                stopped_by = "stop file"
                break
            if any(r.error for r in recs):
                stopped_by = "write error"
                break
            if elapsed - last_status >= 0.5:
                last_status = elapsed
                write_json_atomic(status_path, {
                    "state": "recording",
                    "pid": os.getpid(),
                    "elapsed": round(elapsed, 2),
                    "limit": args.seconds,
                    "sys_db": round(to_db(sys_level()), 1),
                    "mic_db": round(to_db(mic_rec.level), 1) if mic_rec else None,
                    "sys_peak_db": round(to_db(sys_peak()), 1),
                    "bytes": sum(r.frames * r.channels * SAMPWIDTH for r in recs),
                    "device": (loudest_name() if len(taps) > 1 else
                               taps[0]["name"].replace(" [Loopback]", "")),
                    "mic": mic_dev["name"] if mic_dev else None,
                    "sys_path": sys_path,
                    "mic_path": mic_path,
                    "stop_file": stop_path,
                })
            if args.quiet:
                if elapsed - last_quiet >= 10.0:
                    last_quiet = elapsed
                    msg = "%s  sys %s" % (time.strftime("%H:%M:%S", time.gmtime(elapsed)), bar(sys_level()))
                    if mic_rec:
                        msg += "   mic %s" % bar(mic_rec.level)
                    print(msg, flush=True)
            else:
                line = "\r%s  sys %s" % (time.strftime("%M:%S", time.gmtime(elapsed)), bar(sys_level()))
                if mic_rec:
                    line += "   mic %s" % bar(mic_rec.level)
                sys.stdout.write(line + "   ")
                sys.stdout.flush()
            if not silent_warned and elapsed > 8 and sys_peak() < 1e-4:
                sys.stdout.write("\n[!] All output taps are silent. Is anything playing?\n")
                sys.stdout.flush()
                silent_warned = True
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass

    print("\nStopping (%s)..." % stopped_by, flush=True)
    write_json_atomic(status_path, {"state": "finishing", "pid": os.getpid(), "elapsed": time.time() - t0})
    if os.path.exists(stop_path):
        try:
            os.remove(stop_path)
        except OSError:
            pass
    for r in recs:
        r.stop()
    for ka in keepalives:
        ka.close()

    for r in recs:
        print("  %-5s %6.1f s  peak %5.1f dB  %s" % (
            r.label, r.seconds, 20 * math.log10(r.peak) if r.peak > 0 else -99.0, r.path))
        if r.overflows:
            print("       %d buffer warnings (dropouts possible)" % r.overflows)
        if r.error:
            print("[!] %s: writing to disk failed, the recording stopped early (%s)" % (r.label, r.error))

    if len(part_paths) > 1:
        print("  merging %d output taps -> %s" % (len(part_paths), sys_path))
        # the loudest tap goes first: it sets the format of the merge, and a call plays on
        # the loudest endpoint. A tap that failed to open is not merged at all.
        merge_wavs([r.path for r in sorted(sys_recs, key=lambda r: -r.peak)], sys_path)
        for p in part_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    mix_path = None
    if mic_path and not args.no_mix:
        mix_path = os.path.join(out_dir, prefix + "_mix.wav")
        try:
            build_mix(sys_path, mic_path, mix_path, mic_gain=args.mic_gain, mode=args.mix_mode)
            print("  mix  %s  (%s)" % (mix_path,
                                       "L=system, R=mic" if args.mix_mode == "stereo" else "mono sum"))
        except Exception as exc:              # MemoryError on a very long call, disk full ...
            print("[!] mix failed (%r), the system and mic tracks are kept" % exc)
            try:
                os.remove(mix_path)
            except OSError:
                pass
            mix_path = None

    # the recording length goes into the folder and file names, so the records list
    # answers "how long was that call" without opening anything
    dur = int(recs[0].seconds)
    new_prefix = "%s_%dmin%02dsec" % (prefix, dur // 60, dur % 60)
    new_dir = os.path.join(args.out, new_prefix)
    try:
        os.rename(out_dir, new_dir)
        for old in list(os.listdir(new_dir)):
            if old.startswith(prefix):
                os.rename(os.path.join(new_dir, old),
                          os.path.join(new_dir, new_prefix + old[len(prefix):]))
        prefix = new_prefix
        out_dir = new_dir
        sys_path = os.path.join(out_dir, prefix + "_system.wav")
        if mic_path:
            mic_path = os.path.join(out_dir, prefix + "_mic.wav")
        if mix_path:
            mix_path = os.path.join(out_dir, prefix + "_mix.wav")
        print("  named %s" % out_dir)
    except OSError as exc:
        print("[!] rename failed (%s), keeping %s" % (exc, out_dir))

    silent = sys_peak() < 1e-4
    if silent:
        print("\n[!] The system track is silent on every output endpoint.")

    write_json_atomic(last_path, {
        "finished": dt.datetime.now().isoformat(timespec="seconds"),
        "stopped_by": stopped_by,
        "seconds": round(recs[0].seconds, 1),
        "sys_path": sys_path,
        "mic_path": mic_path,
        "mix_path": mix_path,
        "open_path": mix_path or sys_path,
        "silent": bool(silent),
        "device": lb["name"].replace(" [Loopback]", ""),
    })
    try:
        os.remove(status_path)
    except OSError:
        pass

    if args.transcribe:
        # postprocess.py, NOT transcribe.py: it repairs headers, builds a missing mix,
        # refreshes last.json and holds the per-session lock, so this call and the
        # supervisor sweep can race harmlessly and whoever gets there first wins.
        # Calling transcribe.py directly skipped all of that.
        # detached and windowless: this fires the moment a call ends, and a black console
        # popping up on the desktop is not acceptable
        script = os.path.join(HERE, "postprocess.py")
        if os.path.exists(script):
            flags = 0x08000000 | 0x00000008 if os.name == "nt" else 0   # NO_WINDOW|DETACHED
            log_path = os.path.join(TECH_DIR, "postprocess_%s.log" % prefix)
            try:
                os.makedirs(TECH_DIR, exist_ok=True)
                fh = open(log_path, "a", encoding="utf-8")
                subprocess.Popen([sys.executable, script, "--session", prefix],
                                 stdout=fh, stderr=subprocess.STDOUT, creationflags=flags)
                print("postprocess started -> %s" % log_path)
            except OSError as exc:
                print("[!] could not start postprocess: %s" % exc)
        else:
            print("[!] postprocess.py not found at %s" % script)
    return 0


# ----------------------------------------------------------------------------- cli


def build_parser():
    p = argparse.ArgumentParser(
        prog="audiotap",
        description="Record what Windows plays to your headphones/speakers (WASAPI loopback).",
    )
    p.add_argument("--list", action="store_true", help="list output taps and microphones")
    p.add_argument("--devices-json", action="store_true", help="same list as json (used by the widget)")
    p.add_argument("--selftest", action="store_true", help="play a test tone and prove capture works")
    p.add_argument("--device", metavar="IDX|NAME", default=None,
                   help="output device to tap (default: every output device, summed)")
    p.add_argument("--mic", nargs="?", const="default", metavar="IDX|NAME", default=None,
                   help="also record the microphone (bare flag = default mic)")
    p.add_argument("--seconds", type=float, default=None, metavar="N", help="stop after N seconds")
    p.add_argument("--out", default=DEFAULT_OUT_DIR, metavar="DIR",
                   help="output folder (default: recordings_dir from config.json, else <repo>\\recordings)")
    p.add_argument("--name", default=None, metavar="TAG", help="tag added to the file name")
    p.add_argument("--mix-mode", choices=("stereo", "mono"), default="stereo",
                   help="stereo: L=system R=mic (default); mono: single summed track")
    p.add_argument("--mic-gain", type=float, default=1.0, help="gain applied to the mic track in the mix")
    p.add_argument("--no-mix", action="store_true", help="do not build the combined file")
    p.add_argument("--stop-file", default=None, metavar="PATH",
                   help="stop cleanly when this file appears (default: <out>\\STOP)")
    p.add_argument("--quiet", action="store_true",
                   help="one status line every 10 s instead of a live meter (for background runs)")
    p.add_argument("--no-keepalive", action="store_true",
                   help="do not hold the endpoint awake (idle stretches will be dropped)")
    p.add_argument("--status-file", default=None, metavar="PATH",
                   help="live status json for the desktop widget (default: status.json in %%LOCALAPPDATA%%\\AudioTap)")
    p.add_argument("--log", default=None, metavar="PATH",
                   help="redirect console output to a file (needed when started by pythonw)")
    p.add_argument("--transcribe", action="store_true",
                   help="start the transcription automatically once the recording is closed")
    return p


def main():
    args = build_parser().parse_args()

    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
    elif sys.stdout is None:  # started by pythonw.exe: no console at all
        sys.stdout = sys.stderr = open(os.devnull, "w")

    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # keep the machine awake
        except Exception:
            pass

    pa = pyaudio.PyAudio()
    try:
        if args.devices_json:
            return cmd_devices_json(pa)
        if args.list:
            cmd_list(pa)
            return 0
        if args.selftest:
            return cmd_selftest(pa, args)
        return cmd_record(pa, args)
    except RuntimeError as exc:
        print("ERROR: %s" % exc)
        return 1
    finally:
        pa.terminate()
        if os.name == "nt":
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
