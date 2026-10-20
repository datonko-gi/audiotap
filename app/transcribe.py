#!/usr/bin/env python3
"""
Turns an AudioTap session into one readable dialogue transcript.

The two tracks are transcribed separately - the loopback track holds the far side of
the call, the mic track holds you - and then merged on timestamps. That is more
reliable than speaker diarisation on a mixed file, because the separation is physical.

Usage:
  python transcribe.py --session 261019_1420_call
  python transcribe.py --session <name> --model small --device cpu --language en

Model, device, compute type, language, speaker labels and the ffmpeg path come from
config.json (see atconfig.py); the flags override them for one run.
"""

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import wave

import atconfig

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = atconfig.load_config()
RECORDS = atconfig.recordings_dir(CFG)
TECH = atconfig.DATA_DIR


def find_ffmpeg():
    """config.json 'ffmpeg', then PATH. None when ffmpeg is not installed."""
    custom = str(CFG.get("ffmpeg") or "").strip()
    if custom and os.path.exists(custom):
        return custom
    return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()

# never let a child console app pop a black window on the desktop: this often runs while
# the user is on the very call being recorded
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SPEAKER = {"out": str(CFG.get("label_remote") or "Remote"),
           "mic": str(CFG.get("label_you") or "You")}

CYRILLIC_LANGS = {"ru", "uk", "be", "bg", "sr", "mk", "kk", "ky", "mn", "tg"}


def load_whisper(model_name, device="auto", compute_type=""):
    """CUDA when it works, CPU int8 otherwise, so the same code runs without a GPU.

    device: "auto" tries CUDA and falls back to CPU, "cuda" insists on CUDA (an error
    if it is not usable), "cpu" never touches the GPU.
    """
    from faster_whisper import WhisperModel
    if device in ("auto", "cuda"):
        try:
            return WhisperModel(model_name, device="cuda",
                                compute_type=compute_type or "float16"), "cuda"
        except Exception as exc:
            if device == "cuda":
                raise
            print("[!] CUDA unavailable (%s), falling back to CPU - slower" % exc)
    # a CUDA-only compute type (float16) is rejected on CPU, so it only applies to an
    # explicit cpu choice; the automatic fallback uses int8
    cpu_type = compute_type if (device == "cpu" and compute_type) else "int8"
    # ctranslate2's default thread count leaves cores idle; use them all
    return WhisperModel(model_name, device="cpu", compute_type=cpu_type,
                        cpu_threads=(os.cpu_count() or 4)), "cpu"


def log(msg):
    print("[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def clock(sec):
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def clean(text):
    return re.sub(r"\s+", " ", text.strip())


def segment_lang(text, track_lang):
    """Short language tag for one segment.

    Whisper reports one language per track, so a call that switches between a Cyrillic
    language and English is told apart by script: Cyrillic text is ru (uk when it has
    Ukrainian-only letters), Latin text on a Cyrillic track is en, and everything else
    carries the language detected for the track.
    """
    if re.search(r"[\u0456\u0457\u0454\u0491]", text, re.I):
        return "uk"
    if re.search(r"[\u0430-\u044f\u0451]", text, re.I):
        return "ru"
    if track_lang in CYRILLIC_LANGS:
        return "en"
    return track_lang or "en"


def wav_seconds(path):
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def locate_session(session):
    """Folder that holds <session>_system.wav.

    Sessions live in their own folders, the folder may have been renamed by hand
    in Explorer, and files can also lie flat in the recordings folder. The file name
    inside keeps the session prefix in all three cases, so search by it.
    """
    import glob
    for base in (os.path.join(RECORDS, session), RECORDS):
        if os.path.exists(os.path.join(base, session + "_system.wav")):
            return base
        if glob.glob(os.path.join(base, session + "_system_part*.wav")):
            return base                       # crashed multi-endpoint session, unmerged
    hits = (glob.glob(os.path.join(RECORDS, "*", session + "_system.wav"))
            or glob.glob(os.path.join(RECORDS, "*", session + "_system_part0.wav")))
    return os.path.dirname(hits[0]) if hits else None


def to_asr_wav(src, dst):
    """16 kHz mono is what Whisper wants; doing it once beats decoding 1.3 GB twice.

    Without ffmpeg the original file is handed to faster-whisper, which decodes it
    itself (slower start, more memory, same result).
    """
    if not FFMPEG:
        log("ffmpeg not found, transcribing %s directly" % os.path.basename(src))
        return src
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        log("reuse %s" % os.path.basename(dst))
        return dst
    log("ffmpeg -> %s" % os.path.basename(dst))
    # converted under a temporary name: a run killed halfway must not leave a truncated
    # file that the next run would trust as complete
    part = dst[:-len(".wav")] + ".part.wav"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", part]
    subprocess.run(cmd, check=True, creationflags=NO_WINDOW)
    os.replace(part, dst)
    return dst


def transcribe_track(model, path, tag, language):
    log("%s: start %s" % (tag, os.path.basename(path)))
    # multilingual: detect the language per segment, not once for the whole track.
    # A single detection is a trap here: one call can hold two languages, and a track
    # forced to one language gets TRANSLATED into it.
    segments, info = model.transcribe(
        path,
        language=language,
        multilingual=(language is None),
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        word_timestamps=False,
    )
    log("%s: language=%s p=%.2f, speech %.1f min of %.1f min" % (
        tag, info.language, info.language_probability,
        getattr(info, "duration_after_vad", 0) / 60.0, info.duration / 60.0))

    out = []
    t0 = time.time()
    for seg in segments:
        text = clean(seg.text)
        if not text:
            continue
        out.append({"track": tag, "start": seg.start, "end": seg.end, "text": text,
                    "lang": language or segment_lang(text, info.language)})
        if len(out) % 25 == 0:
            log("%s: %4d segments, at %s, %.0f s elapsed" % (tag, len(out), clock(seg.end), time.time() - t0))
    log("%s: done, %d segments in %.0f s" % (tag, len(out), time.time() - t0))
    return out, info


def drop_bleed(mic_segs, out_segs):
    """The mic also hears the speakers, so the far side can appear twice. Drop the echo."""
    dropped = 0
    kept = []
    for m in mic_segs:
        echo = False
        for o in out_segs:
            if o["end"] < m["start"] - 1 or o["start"] > m["end"] + 1:
                continue
            overlap = min(m["end"], o["end"]) - max(m["start"], o["start"])
            if overlap <= 0:
                continue
            span = max(0.1, m["end"] - m["start"])
            if overlap / span < 0.5:
                continue
            if difflib.SequenceMatcher(None, m["text"].lower(), o["text"].lower()).ratio() > 0.6:
                echo = True
                break
        if echo:
            dropped += 1
        else:
            kept.append(m)
    return kept, dropped


def write_transcript(path, session, segs, meta):
    lines = []
    lines.append("TRANSCRIPT OF SESSION %s" % session)
    lines.append("=" * 72)
    lines.append("%s (system output track): %s" % (SPEAKER["out"], meta["sys_path"]))
    lines.append("%s (microphone track): %s" % (SPEAKER["mic"], meta["mic_path"]))

    def lang_mix(track):
        c = {}
        for s in segs:
            if s["track"] == track:
                key = s.get("lang", "?")
                c[key] = c.get(key, 0) + 1
        if not c:
            return "no speech"
        return ", ".join("%s %d" % (k, v) for k, v in sorted(c.items(), key=lambda kv: -kv[1]))

    lines.append("Duration: %.1f min | model: %s | language: %s" % (
        meta["minutes"], meta["model"], meta.get("language") or "detected per segment"))
    lines.append("Segments by language: output [%s], microphone [%s]" % (lang_mix("out"), lang_mix("mic")))
    lines.append("Generated: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    if meta["bleed"]:
        lines.append("Removed %d segments that the microphone picked up from the speakers." % meta["bleed"])
    lines.append("")
    lines.append("-" * 72)
    lines.append("")

    prev = None
    buf = []
    start = 0.0
    last_end = 0.0

    langs = []

    def flush():
        if prev and buf:
            tag = max(set(langs), key=langs.count) if langs else ""
            lines.append("[%s] %s (%s):" % (clock(start), SPEAKER[prev], tag))
            lines.append("    " + " ".join(buf))
            lines.append("")

    for s in segs:
        # new paragraph on a speaker change, on a pause, or when the block gets too long
        # to read; without this the far side comes out as one wall of text per minute
        gap = s["start"] - last_end
        too_long = sum(len(x) for x in buf) > 400
        if s["track"] != prev or (buf and (gap > 8 or too_long)):
            flush()
            prev = s["track"]
            buf = []
            langs = []
            start = s["start"]
        buf.append(s["text"])
        langs.append(s.get("lang", ""))
        last_end = s["end"]
    flush()

    # words and segment counts only: segment timestamps include trailing pauses, so
    # summing their durations overstates speaking time by roughly a factor of two
    words = {"out": 0, "mic": 0}
    count = {"out": 0, "mic": 0}
    for s in segs:
        words[s["track"]] += len(s["text"].split())
        count[s["track"]] += 1
    lines.append("-" * 72)
    lines.append("%s: %d words in %d segments. %s: %d words in %d segments." % (
        SPEAKER["out"], words["out"], count["out"], SPEAKER["mic"], words["mic"], count["mic"]))
    if meta.get("vad_out") or meta.get("vad_mic"):
        lines.append("Speech detected by VAD: output %.1f min, microphone %.1f min of %.1f min recorded." % (
            meta.get("vad_out", 0), meta.get("vad_mic", 0), meta["minutes"]))

    with open(path, "w", encoding="utf-8-sig") as fh:
        fh.write("\n".join(lines))
    return path


def run_tracks(model, sys_asr, mic_asr, language):
    out_segs, out_info = transcribe_track(model, sys_asr, "out", language)
    mic_segs, mic_info = ([], None)
    if mic_asr:
        mic_segs, mic_info = transcribe_track(model, mic_asr, "mic", language)
    return out_segs, out_info, mic_segs, mic_info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, help="file prefix, e.g. 261019_1420_call")
    ap.add_argument("--model", default=None, help="faster-whisper model (default: transcribe_model in config.json)")
    ap.add_argument("--language", default=None, help="force a language code (default: config.json, else auto detect)")
    ap.add_argument("--device", choices=("auto", "cuda", "cpu"), default=None,
                    help="auto = CUDA with CPU fallback (default: transcribe_device in config.json)")
    ap.add_argument("--compute-type", default=None, help="float16, int8, float32 ... (default: config.json, else by device)")
    args = ap.parse_args()

    model_name = args.model or CFG.get("transcribe_model") or "large-v3"
    language = args.language or str(CFG.get("language") or "").strip() or None
    choice = args.device or CFG.get("transcribe_device") or "auto"
    compute = args.compute_type or str(CFG.get("transcribe_compute_type") or "").strip()

    base = locate_session(args.session)
    if not base:
        print("no such session: %s" % args.session)
        return 1
    sys_src = os.path.join(base, args.session + "_system.wav")
    mic_src = os.path.join(base, args.session + "_mic.wav")
    if not os.path.exists(sys_src):
        print("no such session: %s" % sys_src)
        return 1
    os.makedirs(TECH, exist_ok=True)

    minutes = wav_seconds(sys_src) / 60.0
    log("session %s, %.1f min" % (args.session, minutes))

    sys_asr = to_asr_wav(sys_src, os.path.join(TECH, args.session + "_asr_out.wav"))
    mic_asr = to_asr_wav(mic_src, os.path.join(TECH, args.session + "_asr_mic.wav")) if os.path.exists(mic_src) else None

    log("loading %s (device setting: %s)" % (model_name, choice))
    model, device = load_whisper(model_name, choice, compute)
    log("model on %s" % device)
    if device == "cpu" and model_name.startswith("large"):
        log("hint: %s on a CPU is slow; set transcribe_model to small or base in config.json" % model_name)

    try:
        out_segs, out_info, mic_segs, mic_info = run_tracks(model, sys_asr, mic_asr, language)
    except Exception as exc:
        # CUDA can load fine and still fail on the first real batch (missing cuBLAS or
        # cuDNN libraries), so "auto" gets one more try on the CPU
        if device != "cuda" or choice != "auto":
            raise
        log("[!] CUDA failed while transcribing (%s), retrying on CPU - slower" % exc)
        model, device = load_whisper(model_name, "cpu", "")
        out_segs, out_info, mic_segs, mic_info = run_tracks(model, sys_asr, mic_asr, language)

    mic_segs, bleed = drop_bleed(mic_segs, out_segs)
    if bleed:
        log("dropped %d mic segments as speaker bleed" % bleed)

    segs = sorted(out_segs + mic_segs, key=lambda s: s["start"])
    with open(os.path.join(TECH, args.session + "_segments.json"), "w", encoding="utf-8") as fh:
        json.dump(segs, fh, ensure_ascii=False, indent=1)

    meta = {
        "sys_path": sys_src,
        "mic_path": mic_src if mic_asr else "none",
        "minutes": minutes,
        "model": model_name,
        "language": language,
        "vad_out": getattr(out_info, "duration_after_vad", 0) / 60.0,
        "vad_mic": (getattr(mic_info, "duration_after_vad", 0) / 60.0) if mic_info else 0.0,
        "bleed": bleed,
    }
    with open(os.path.join(TECH, args.session + "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    txt = write_transcript(os.path.join(base, args.session + "_transcript.txt"), args.session, segs, meta)
    log("TRANSCRIPT: %s" % txt)
    log("segments: %d (out %d, mic %d)" % (len(segs), len(out_segs), len(mic_segs)))
    # the 16 kHz copies were only an intermediate step
    for tmp, orig in ((sys_asr, sys_src), (mic_asr, mic_src)):
        if tmp and tmp != orig:
            try:
                os.remove(tmp)
            except OSError:
                pass
    # ctranslate2 tears the CUDA context down on interpreter exit and that teardown can
    # return a nonzero code even though the work finished. Leave before it runs.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
