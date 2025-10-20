#!/usr/bin/env python3
"""
waveform_slideshow.py

Usage:
  python3 waveform_slideshow.py IMAGES_DIR AUDIO_FILE OUTPUT_FILE
        [seconds_per_image] [transition_dur] [width] [height] [wave_h]
        [bar_h] [bar_color] [text] [text_size] [fontfile] [xfade_name]

Example:
  python3 waveform_slideshow.py ./imgs lofi.mp3 out.mp4 10 0.5 1920 1080 240 60 "#002244@0.8" "Galactic Cruise" 36 /Library/Fonts/Arial.ttf fade
"""

import os
import sys
import subprocess
import shutil
import tempfile
from math import ceil

# VideoToolbox example (macOS)
hw_opts = [
    "-c:v", "h264_videotoolbox",
    "-b:v", "4000k",
    "-pix_fmt", "yuv420p"
]


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)

def check_dep(name):
    if shutil.which(name) is None:
        die(f"Required executable '{name}' not found in PATH. Please install it.")

def ffprobe_duration(path):
    # returns duration in seconds as float or raises
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "csv=p=0", path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    s = out.stdout.strip()
    if not s:
        raise RuntimeError("ffprobe returned empty duration")
    return float(s)

def gather_images(images_dir):
    exts = ("jpg","jpeg","png","gif","JPG","JPEG","PNG","GIF")
    files = []
    for e in exts:
        files.extend(sorted([os.path.join(images_dir, f) for f in os.listdir(images_dir) if f.endswith("." + e) or f.endswith("." + e.upper())]))
    # fallback: glob-like preserving spaces
    if not files:
        for f in os.listdir(images_dir):
            full = os.path.join(images_dir, f)
            if os.path.isfile(full) and any(f.lower().endswith("." + e) for e in ("jpg","jpeg","png","gif")):
                files.append(full)
        files.sort()
    return files

def escape_drawtext_text(s):
    # escape backslash and single-quote and percent for drawtext; keep simple
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("%", "%%")

def fmtf(x, ndigits=6):
    # format float with dot decimal, fixed precision (like LC_NUMERIC=C)
    return f"{x:.{ndigits}f}"

def main(argv):
    if len(argv) < 4:
        print(__doc__)
        die("Not enough arguments.")

    check_dep("ffmpeg")
    check_dep("ffprobe")

    images_dir = argv[1]
    audio_file = argv[2]
    output_file = argv[3]

    seconds_per_image = float(argv[4]) if len(argv) > 4 else 20.0
    transition_dur = float(argv[5]) if len(argv) > 5 else 1.0
    width = int(argv[6]) if len(argv) > 6 else 400
    height = int(argv[7]) if len(argv) > 7 else 400
    wave_h = int(argv[8]) if len(argv) > 8 else 120
    bar_h = int(argv[9]) if len(argv) > 9 else 60
    bar_color = argv[10] if len(argv) > 10 else "#000000@0.7"
    text = argv[11] if len(argv) > 11 else ""
    text_size = int(argv[12]) if len(argv) > 12 else 24
    fontfile = argv[13] if len(argv) > 13 else ""
    xfade_name = argv[14] if len(argv) > 14 else "fade"

    # basic checks
    if not os.path.isdir(images_dir):
        die(f"Images directory not found: {images_dir}")
    if not os.path.isfile(audio_file):
        die(f"Audio file not found: {audio_file}")

    images = gather_images(images_dir)
    if not images:
        die("No images found in directory (supported extensions: jpg/jpeg/png/gif)")

    # audio duration
    try:
        audio_duration = ffprobe_duration(audio_file)
    except Exception as e:
        die(f"Could not determine audio duration: {e}")

    # compute needed images (ceil)
    needed_images = int(ceil(audio_duration / seconds_per_image))
    if needed_images < 1:
        needed_images = 1

    # if fewer images than needed - cycle them
    inputs = [images[i % len(images)] for i in range(needed_images)]

    # ensure transition_dur < seconds_per_image
    if transition_dur >= seconds_per_image:
        transition_dur = seconds_per_image / 2.0
        print(f"Warning: transition_dur >= seconds_per_image, reduced to {transition_dur}")

    # compute numeric positions (no expressions in ffmpeg)
    drawbox_y = height - bar_h
    # approximate vertical center of text inside bar using text_size
    drawtext_y = (drawbox_y + (bar_h - text_size) / 2.0)
    # waveform vertically centered (as requested)
    overlay_y = (height - wave_h) / 2.0

    # prepare ffmpeg command with many -i (one per image) + audio
    ff_args = ["ffmpeg", "-y"]
    for img in inputs:
        ff_args += ["-loop", "1", "-t", fmtf(seconds_per_image, 6), "-i", img]
    ff_args += ["-i", audio_file]

    audio_index = len(inputs)

    # compute step for xfade offsets (D - T)
    step = seconds_per_image - transition_dur

    # build filter_complex
    fc_parts = []

    # per input prepare
    for i in range(len(inputs)):
        # scale to target resolution and trim to exact per-image duration
        fc_parts.append(f"[{i}:v]scale={width}:{height},format=rgba,setsar=1,trim=duration={fmtf(seconds_per_image)},setpts=PTS-STARTPTS[v{i}]")

    # chain xfade
    if len(inputs) == 1:
        fc_parts.append("[v0]format=yuv420p[slide]")
    else:
        # xfade between v0..vN
        for k in range(len(inputs)-1):
            in1 = f"[v0]" if k == 0 else f"[xf{k}]"
            in2 = f"[v{k+1}]"
            n = k + 1
            offset = step * n
            offset_s = fmtf(offset, 6)
            dur_s = fmtf(transition_dur, 6)
            out_label = f"xf{n}"
            # use yuv420p output format for xfade
            fc_parts.append(f"{in1}{in2}xfade=transition={xfade_name}:duration={dur_s}:offset={offset_s},format=yuv420p[{out_label}]")
        last = len(inputs)-1
        fc_parts.append(f"[xf{last}]format=yuv420p[slide]")

    # draw bar + text: use numeric drawbox_y and numeric drawtext_y
    if text:
        text_escaped = escape_drawtext_text(text)
        fontpart = f"fontfile={fontfile}:" if fontfile else ""
        fc_parts.append(
            f"[slide]drawbox=x=0:y={int(drawbox_y)}:w=iw:h={bar_h}:color={bar_color}:t=fill,"
            f"{fontpart}drawtext=text='{text_escaped}':fontcolor=white:fontsize={text_size}:"
            f"x=(w-text_w)/2:y={fmtf(drawtext_y,2)}:box=0[slide_bar]"
        )
    else:
        fc_parts.append(f"[slide]drawbox=x=0:y={int(drawbox_y)}:w=iw:h={bar_h}:color={bar_color}:t=fill[slide_bar]")

    # waveform from audio — produce yuva420p
    fc_parts.append(f"[{audio_index}:a]showwaves=s={width}x{wave_h}:mode=cline:colors=0xff1646@0.6,format=yuva420p[wave]")

    # overlay waveform centered horizontally and at overlay_y
    fc_parts.append(f"[slide_bar][wave]overlay=x=(W-w)/2:y={fmtf(overlay_y,2)}:format=auto[outv]")

    filter_complex = ";".join(fc_parts)

    # assemble final ffmpeg invocation
    ff_args += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", f"{audio_index}:a",
                "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "aac", "-shortest", output_file]

    print("Running ffmpeg with command (truncated):")
    print(" ".join(ff_args[:6]) + " ... " + " ".join(ff_args[-6:]))

    # run and stream output
    proc = subprocess.Popen(ff_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        print(line, end="")  # forward ffmpeg progress
    proc.wait()
    if proc.returncode != 0:
        die(f"ffmpeg failed with code {proc.returncode}")

    print("Done:", output_file)
    print(f"Audio length: {audio_duration}s, images used: {len(inputs)}, resolution: {width}x{height}")

if __name__ == "__main__":
    main(sys.argv)
