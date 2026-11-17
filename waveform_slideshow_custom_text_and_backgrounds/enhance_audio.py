#!/usr/bin/env python3
"""enhance_audio.py

Clean up a spoken-word / music track for YouTube and Spotify with FFmpeg:

  1. High-pass filter: removes rumble, handling noise and plosive thumps
     below ~80 Hz (no useful voice content lives there).
  2. Optional denoise: `afftdn` (FFT spectral subtraction, light/strong) or
     `arnndn` (RNNoise neural model for speech, needs a .rnnn model file).
  3. Two-pass EBU R128 loudness normalisation: the first pass measures the
     track (ffmpeg `ebur128`), the second applies one static gain to the
     target loudness (default -16 LUFS) plus a 4x-oversampled limiter that
     keeps true peaks under -1 dBTP. No pumping, dynamics stay untouched.

Usage:
    python3 enhance_audio.py input.wav output.wav
    python3 enhance_audio.py input.wav output.wav --target-lufs -14 --denoise light
    python3 enhance_audio.py input.wav --analyze          # only measure

Also importable: waveform_argparse.py uses `enhance()` for its --audio-enhance option.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

# afftdn noise reduction (dB) per denoise level. nf = assumed noise floor (dBFS).
AFFTDN_PRESETS = {
    "light": "afftdn=nr=8:nf=-60:tn=1",
    "strong": "afftdn=nr=16:nf=-50:tn=1",
}


@dataclass
class EnhanceOptions:
    target_lufs: float = -16.0
    true_peak: float = -1.0
    highpass_hz: float = 80.0      # 0 disables
    denoise: str = "off"           # off | light | strong | rnn
    rnn_model: str = ""            # .rnnn model file for denoise=rnn
    normalize: bool = True


def _die(msg: str):
    print(msg, file=sys.stderr)
    sys.exit(1)


def _ffmpeg_stderr(cmd: List[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        _die(f"ffmpeg failed (code {res.returncode}):\n{res.stderr[-1500:]}")
    return res.stderr


def _probe_audio(path: str):
    """Return (sample_rate, channels) of the first audio stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels", "-of", "json", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        _die(out.stderr.strip() or f"ffprobe failed for {path}")
    streams = json.loads(out.stdout).get("streams") or []
    if not streams:
        _die(f"No audio stream in {path}")
    return int(streams[0]["sample_rate"]), int(streams[0]["channels"])


def pre_filters(o: EnhanceOptions) -> List[str]:
    """Filters applied before loudness normalisation."""
    f = []
    if o.highpass_hz and o.highpass_hz > 0:
        # Two cascaded 2-pole filters = 24 dB/oct, steep enough to keep the voice intact.
        f.append(f"highpass=f={o.highpass_hz:g}:poles=2")
        f.append(f"highpass=f={o.highpass_hz:g}:poles=2")
    if o.denoise in AFFTDN_PRESETS:
        f.append(AFFTDN_PRESETS[o.denoise])
    elif o.denoise == "rnn":
        if not o.rnn_model or not os.path.isfile(o.rnn_model):
            _die("--denoise rnn needs --rnn-model pointing to a .rnnn file "
                 "(e.g. from https://github.com/GregorR/rnnoise-models)")
        model = o.rnn_model.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        f.append(f"arnndn=m='{model}'")
    elif o.denoise != "off":
        _die(f"Unknown denoise level: {o.denoise}")
    return f


def measure(path: str, filters: Optional[List[str]] = None) -> dict:
    """EBU R128 stats of `path` after `filters`: integrated loudness (i, LUFS),
    loudness range (lra, LU) and true peak (tp, dBTP)."""
    chain = list(filters or []) + ["ebur128=peak=true:framelog=quiet"]
    err = _ffmpeg_stderr(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                          "-af", ",".join(chain), "-f", "null", "-"])
    summary = err[err.rfind("Summary:"):]
    vals = {}
    for key, label in (("i", "I"), ("lra", "LRA"), ("tp", "Peak")):
        m = re.search(rf"^\s*{label}:\s*(-?[\d.]+|-inf)", summary, re.M)
        if not m:
            _die("Could not parse loudness measurement output")
        vals[key] = float(m.group(1))
    return vals


def _codec_args(out_path: str, bitrate: Optional[str]) -> List[str]:
    ext = os.path.splitext(out_path)[1].lower()
    if ext in (".wav", ".aif", ".aiff"):
        return ["-c:a", "pcm_s24le" if ext == ".wav" else "pcm_s24be"]
    if ext == ".flac":
        return ["-c:a", "flac", "-sample_fmt", "s32"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", bitrate or "320k"]
    if ext in (".m4a", ".aac", ".mp4"):
        return ["-c:a", aac_encoder(), "-b:a", bitrate or "256k"]
    return ["-b:a", bitrate] if bitrate else []


def convert(in_path: str, out_path: str, bitrate: Optional[str] = None):
    """Re-encode in_path to the format implied by out_path's extension."""
    _ffmpeg_stderr(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", in_path, "-vn",
                    "-map_metadata", "0"] + _codec_args(out_path, bitrate) + [out_path])


def gain_chain(gain_db: Optional[float], o: EnhanceOptions, sample_rate: int) -> List[str]:
    """Static gain + true-peak limiter (empty when gain_db is None)."""
    if gain_db is None:
        return []
    ceiling = 10 ** (o.true_peak / 20)
    resamp = ":resampler=soxr" if _has_soxr() else ""
    return [
        f"volume={gain_db:.2f}dB",
        # Limit at 4x the sample rate so inter-sample (true) peaks are caught too.
        f"aresample={sample_rate * 4}{resamp}",
        f"alimiter=limit={ceiling:.5f}:attack=1:release=60:level=false:latency=true",
        f"aresample={sample_rate}{resamp}",
    ]


def enhance(in_path: str, out_path: str, o: EnhanceOptions,
            bitrate: Optional[str] = None, verbose: bool = True) -> dict:
    """Process in_path -> out_path. Returns loudness stats {'before', 'after'} (empty without normalisation).

    Normalisation = one static gain to the target loudness plus a 4x-oversampled
    peak limiter that only catches the few transients that would exceed the
    true-peak ceiling. Unlike loudnorm's dynamic mode this does not pump or
    change the dynamics of the rest of the track."""
    sample_rate, _ = _probe_audio(in_path)
    chain = pre_filters(o)
    stats = {}

    if o.normalize:
        if verbose:
            print("Audio: measuring loudness (pass 1/2)...")
        before = measure(in_path, chain)
        if before["i"] == float("-inf") or before["i"] < -70:
            _die("Audio is silent; cannot normalise loudness")
        gain = o.target_lufs - before["i"]
        stats["before"] = before

    def render(gain_db: Optional[float]):
        _ffmpeg_stderr(["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", in_path,
                        "-af", ",".join(chain + gain_chain(gain_db, o, sample_rate)),
                        "-vn", "-map_metadata", "0"] + _codec_args(out_path, bitrate) + [out_path])

    if not o.normalize:
        if not chain:
            shutil.copyfile(in_path, out_path)
            return stats
        if verbose:
            print("Audio: processing...")
        render(None)
        return stats

    if verbose:
        print("Audio: processing (pass 2/2)...")
    render(gain)
    after = measure(out_path)
    # The limiter shaves a little loudness off the peaks; compensate once.
    if abs(after["i"] - o.target_lufs) > 0.3:
        gain += o.target_lufs - after["i"]
        if verbose:
            print(f"Audio: correcting gain by {o.target_lufs - after['i']:+.1f} dB (pass 3)...")
        render(gain)
        after = measure(out_path)
    stats["after"] = after
    if verbose:
        b = stats["before"]
        print(f"Audio: {b['i']:.1f} LUFS / {b['tp']:.1f} dBTP  ->  "
              f"{after['i']:.1f} LUFS / {after['tp']:.1f} dBTP  (gain {gain:+.1f} dB)")
        limited = b["tp"] + gain - o.true_peak
        if limited > 6:
            print(f"Audio: note: loudest peaks were limited by up to ~{limited:.0f} dB; "
                  "if it sounds squashed, try a lower --target-lufs.")
    return stats


_SOXR = None
_AAC = None


def aac_encoder() -> str:
    """Apple's AudioToolbox AAC encoder when available (macOS): ~6x faster than
    FFmpeg's native `aac` and at least as good at 256k; native `aac` elsewhere."""
    global _AAC
    if _AAC is None:
        res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
        _AAC = "aac_at" if re.search(r"^\s*A\S*\s+aac_at\s", res.stdout, re.M) else "aac"
    return _AAC


def _has_soxr() -> bool:
    global _SOXR
    if _SOXR is None:
        res = subprocess.run(["ffmpeg", "-hide_banner", "-buildconf"], capture_output=True, text=True)
        _SOXR = "--enable-libsoxr" in res.stdout
    return _SOXR


def add_cli_options(p: argparse.ArgumentParser, prefix: str = ""):
    """Register the shared audio options (prefix '--audio-' when embedded in another CLI)."""
    p.add_argument(f"--{prefix}target-lufs", type=float, default=-16.0,
                   help="Integrated loudness target in LUFS (default: -16; YouTube/Spotify play at about -14)")
    p.add_argument(f"--{prefix}true-peak", type=float, default=-1.0,
                   help="Maximum true peak in dBTP (default: -1)")
    p.add_argument(f"--{prefix}highpass", type=float, default=80.0,
                   help="High-pass cutoff in Hz to remove rumble (default: 80; 0 disables)")
    p.add_argument(f"--{prefix}denoise", default="off", choices=["off", "light", "strong", "rnn"],
                   help="Noise reduction: off (default), light/strong (afftdn), rnn (arnndn, needs a model)")
    p.add_argument(f"--{prefix}rnn-model", default="",
                   help="RNNoise .rnnn model file for denoise=rnn")


def options_from_args(args, prefix: str = "") -> EnhanceOptions:
    a = prefix.replace("-", "_")
    return EnhanceOptions(
        target_lufs=getattr(args, f"{a}target_lufs"),
        true_peak=getattr(args, f"{a}true_peak"),
        highpass_hz=getattr(args, f"{a}highpass"),
        denoise=getattr(args, f"{a}denoise"),
        rnn_model=getattr(args, f"{a}rnn_model"),
    )


def main():
    p = argparse.ArgumentParser(description="Denoise, clean up and loudness-normalise an audio file with FFmpeg.")
    p.add_argument("input", help="Input audio file")
    p.add_argument("output", nargs="?", help="Output file (.wav/.flac = lossless 24-bit, .mp3 = 320k, .m4a = AAC 256k)")
    add_cli_options(p)
    p.add_argument("--no-normalize", action="store_true", help="Skip loudness normalisation (filters only)")
    p.add_argument("--bitrate", default=None, help="Bitrate for lossy outputs (e.g. 320k)")
    p.add_argument("--analyze", action="store_true", help="Only print loudness stats of the input, write nothing")
    args = p.parse_args()

    for dep in ("ffmpeg", "ffprobe"):
        if shutil.which(dep) is None:
            _die(f"Required executable '{dep}' not found in PATH.")
    if not os.path.isfile(args.input):
        _die(f"Input file not found: {args.input}")

    o = options_from_args(args)
    o.normalize = not args.no_normalize

    if args.analyze:
        st = measure(args.input)
        print(f"Integrated loudness: {st['i']:.1f} LUFS")
        print(f"True peak:           {st['tp']:.1f} dBTP")
        print(f"Loudness range:      {st['lra']:.1f} LU")
        return

    if not args.output:
        _die("Output path required (or use --analyze)")
    if os.path.abspath(args.input) == os.path.abspath(args.output):
        _die("Output must differ from input")
    enhance(args.input, args.output, o, bitrate=args.bitrate)
    print("Done:", args.output)


if __name__ == "__main__":
    main()
