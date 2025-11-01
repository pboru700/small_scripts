#!/usr/bin/env python3
"""
waveform_slideshow_fixed.py

Drop-in replacement that:
 - converts used source images to JPEG before rendering,
 - implements blurred-stretched background + centered aspect-preserving foreground,
 - draws a solid waveform line,
 - ensures final output is scaled to requested width x height,
 - optionally overlays a logo image in the bottom-left corner (argument at the end).

Usage:
  python3 waveform_slideshow_fixed.py IMAGES_DIR AUDIO_FILE OUTPUT_FILE
        [seconds_per_image] [transition_dur] [width] [height] [wave_h]
        [bar_h] [bar_color] [text] [text_size] [fontfile] [xfade_name] [logo_file]

Example:
  python3 waveform_slideshow_fixed.py ./imgs lofi.mp3 out.mp4 10 0.5 1920 1080 240 60 "#000000@0.7" "Title" 36 /Library/Fonts/Arial.ttf fade logo.png
"""
import os
import sys
import subprocess
import shutil
import tempfile
from math import ceil

def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)

def check_dep(name):
    if shutil.which(name) is None:
        die(f"Required executable '{name}' not found in PATH. Please install it.")

def ffprobe_duration(path):
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
    exts = {'.jpg', '.jpeg', '.png', '.gif'}
    files = []
    for entry in os.listdir(images_dir):
        full = os.path.join(images_dir, entry)
        if not os.path.isfile(full):
            continue
        _, ext = os.path.splitext(entry)
        if ext.lower() in exts:
            files.append(full)
    files.sort(key=lambda p: os.path.basename(p).lower())
    return files

def escape_drawtext_text(s):
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("%", "%%")

def fmtf(x, ndigits=6):
    return f"{x:.{ndigits}f}"

def build_drawtext_options(text, text_size, fontfile, text_y):
    parts = []
    if fontfile:
        fontfile_esc = fontfile.replace("'", "\\'")
        parts.append(f"fontfile='{fontfile_esc}'")
    parts.append(f"text='{escape_drawtext_text(text)}'")
    parts.append("fontcolor=white")
    parts.append(f"fontsize={int(text_size)}")
    parts.append("x=(w-text_w)/2")
    parts.append(f"y={fmtf(text_y,2)}")
    parts.append("box=0")
    return ":".join(parts)

def ffmpeg_color_for_showwaves(color):
    if not color:
        return '0xffffff'
    c = color.split('@')[0].strip()
    if c.startswith('#') and len(c) == 7:
        return '0x' + c.lstrip('#')
    return c

def convert_images_to_jpeg(src_list, tmpdir, quality=2):
    conv_map = {}
    converted = []
    idx = 0
    for src in src_list:
        if src in conv_map:
            converted.append(conv_map[src])
            continue
        outname = os.path.join(tmpdir, f"img_{idx:04d}.jpg")
        idx += 1
        cmd = ["ffmpeg", "-y", "-i", src, "-frames:v", "1", "-q:v", str(quality), outname]
        print("Converting image to JPEG:", src, "->", outname)
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            die(f"Image conversion failed for {src}: {res.stderr.strip()}")
        conv_map[src] = outname
        converted.append(outname)
    return converted

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
    if not bar_color:
        bar_color = "#000000@0.7"
    text = argv[11] if len(argv) > 11 else ""
    text_size = int(argv[12]) if len(argv) > 12 else 24
    logo_file = argv[13] if len(argv) > 13 else ""
    fontfile = argv[14] if len(argv) > 14 else ""
    xfade_name = argv[15] if len(argv) > 15 else "fade"

    if not os.path.isdir(images_dir):
        die(f"Images directory not found: {images_dir}")
    if not os.path.isfile(audio_file):
        die(f"Audio file not found: {audio_file}")
    if logo_file and not os.path.isfile(logo_file):
        die(f"Logo file not found: {logo_file}")

    images = gather_images(images_dir)
    if not images:
        die("No images found in directory (supported extensions: jpg/jpeg/png/gif)")

    try:
        audio_duration = ffprobe_duration(audio_file)
    except Exception as e:
        die(f"Could not determine audio duration: {e}")

    needed_images = int(ceil(audio_duration / seconds_per_image))
    needed_images = max(1, needed_images)
    inputs = [images[i % len(images)] for i in range(needed_images)]

    tmpdir = tempfile.mkdtemp(prefix="ws_jpeg_")
    try:
        converted_inputs = convert_images_to_jpeg(inputs, tmpdir, quality=2)

        if transition_dur >= seconds_per_image:
            transition_dur = seconds_per_image / 2.0
            print(f"Warning: transition_dur >= seconds_per_image, reduced to {transition_dur}")

        drawbox_y = height - bar_h
        drawtext_y = (drawbox_y + (bar_h - text_size) / 2.0)
        overlay_y = (height - wave_h) / 2.0

        ff_args = ["ffmpeg", "-y"]
        for img in converted_inputs:
            ff_args += ["-loop", "1", "-t", fmtf(seconds_per_image, 6), "-i", img]
        ff_args += ["-i", audio_file]
        if logo_file:
            ff_args += ["-i", logo_file]

        audio_index = len(converted_inputs)
        logo_index = audio_index + 1 if logo_file else None

        step = seconds_per_image - transition_dur
        fc_parts = []

        # per input: create bg (blur fullsize) + fg (aspect-preserving) and overlay -> produce yuv420p v{i}
        for i in range(len(converted_inputs)):
            # foreground: preserve aspect and fill at least one dimension
            fc_parts.append(
                f"[{i}:v]format=rgba,setsar=1,scale='if(gt(iw/ih,{width}/{height}),{width},-2)':'if(gt(iw/ih,{width}/{height}),-2,{height})',"
                f"trim=duration={fmtf(seconds_per_image)},setpts=PTS-STARTPTS[fg{i}]"
            )
            # background: scale to full size and blur
            fc_parts.append(
                f"[{i}:v]format=rgba,setsar=1,scale={width}:{height},boxblur=10:1,trim=duration={fmtf(seconds_per_image)},setpts=PTS-STARTPTS[bg{i}]"
            )
            # overlay fg on bg -> IMPORTANT: produce YUV for xfade compatibility
            fc_parts.append(
                f"[bg{i}][fg{i}]overlay=x=(W-w)/2:y=(H-h)/2,format=yuv420p[v{i}]"
            )

        # chain xfade between [v0]..[vN] (inputs are now yuv420p)
        if len(converted_inputs) == 1:
            fc_parts.append("[v0]format=yuv420p[slide]")
        else:
            for k in range(len(converted_inputs) - 1):
                in1 = f"[v0]" if k == 0 else f"[xf{k}]"
                in2 = f"[v{k+1}]"
                n = k + 1
                offset = step * n
                offset_s = fmtf(offset, 6)
                dur_s = fmtf(transition_dur, 6)
                out_label = f"xf{n}"
                # keep format=yuv420p after xfade
                fc_parts.append(f"{in1}{in2}xfade=transition={xfade_name}:duration={dur_s}:offset={offset_s},format=yuv420p[{out_label}]")
            last = len(converted_inputs) - 1
            fc_parts.append(f"[xf{last}]format=yuv420p[slide]")

        # draw bar & optional text
        if bar_h > 0:
            if text:
                drawtext_opts = build_drawtext_options(text, text_size, fontfile, drawtext_y)
                fc_parts.append(
                    f"[slide]drawbox=x=0:y={int(drawbox_y)}:w=iw:h={bar_h}:color={bar_color}:t=fill,drawtext={drawtext_opts}[slide_bar]"
                )
            else:
                fc_parts.append(f"[slide]drawbox=x=0:y={int(drawbox_y)}:w=iw:h={bar_h}:color={bar_color}:t=fill[slide_bar]")
        else:
            fc_parts.append("[slide]copy[slide_bar]")

        # waveform: solid line, produce yuva420p (has alpha)
        wave_color = ffmpeg_color_for_showwaves(bar_color)
        fc_parts.append(f"[{audio_index}:a]showwaves=s={width}x{wave_h}:mode=line:colors={wave_color},format=yuva420p[wave]")

        # overlay waveform onto slide_bar -> mid
        fc_parts.append(f"[slide_bar][wave]overlay=x=(W-w)/2:y={fmtf(overlay_y,2)}:format=auto[mid]")

        # logo overlay if provided (logo kept as input so alpha is preserved)
        if logo_file:
            max_logo_w = int(width * 0.06)
            fc_parts.append(f"[{logo_index}:v]format=rgba,setsar=1,scale='if(gt(iw,{max_logo_w}),{max_logo_w},iw)':'-2'[logo_scaled]")
            fc_parts.append(f"[mid][logo_scaled]overlay=x=10:y=H-h-10:format=auto[outv]")
            fc_parts.append(f"[outv]scale={width}:{height}[outv_final]")
            map_out = "[outv_final]"
        else:
            fc_parts.append(f"[mid]scale={width}:{height}[outv]")
            map_out = "[outv]"

        filter_complex = ";".join(fc_parts)

        ff_args += ["-filter_complex", filter_complex, "-map", map_out, "-map", f"{audio_index}:a",
                    "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "aac", "-shortest", output_file]

        print("Running ffmpeg (truncated):")
        print(" ".join(ff_args[:6]) + " ... " + " ".join(ff_args[-6:]))

        proc = subprocess.Popen(ff_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                print(line, end="")
            proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            die("Interrupted by user")

        if proc.returncode != 0:
            die(f"ffmpeg failed with code {proc.returncode}")

        print("Done:", output_file)
        print(f"Audio length: {audio_duration}s, images used: {len(converted_inputs)}, resolution: {width}x{height}")

    finally:
        try:
            if os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir)
        except Exception as e:
            print("Warning: failed to remove temp dir", tmpdir, ":", e, file=sys.stderr)

if __name__ == "__main__":
    main(sys.argv)
