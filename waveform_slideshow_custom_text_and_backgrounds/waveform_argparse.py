#!/usr/bin/env python3
"""waveform_slideshow.py

Generate a video slideshow from a folder of images and an MP3 audio track with:
 - Fixed target resolution (width x height)
 - Each image scaled to "contain" within target while preserving aspect ratio (upscale if smaller, downscale if larger)
 - Blurred stretched background of each image filling full frame
 - Crossfade transitions between images
 - Bottom semi-transparent bar with centered text
 - Waveform visualization of audio in foreground (same base color, opaque)
 - Optional logo overlay
 - Loop images if audio is longer than total image display time

Usage example:
    python3 waveform_argparse.py \
            --images imgs \
            --audio track.mp3 \
            --output out.mp4 \
            --width 1920 --height 1080 \
            --seconds-per-image 10 \
            --fade-duration 0.5 \
            --bar-color "#000000" \
            --bar-alpha 0.7 \
            --text "Sample Title" \
            --text-size 42 \
            --font /Library/Fonts/Arial.ttf \
            --logo logo.png

"""
import argparse
import os
import sys
import tempfile
import shutil
import subprocess
from math import ceil
from typing import Optional

SUPPORTED_EXTS = {'.jpg', '.jpeg', '.png', '.gif'}


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def check_dep(name: str):
    if shutil.which(name) is None:
        die(f"Required executable '{name}' not found in PATH.")


def gather_images(images_dir: str):
    files = []
    for entry in os.listdir(images_dir):
        full = os.path.join(images_dir, entry)
        if not os.path.isfile(full):
            continue
        _, ext = os.path.splitext(entry)
        if ext.lower() in SUPPORTED_EXTS:
            files.append(full)
    files.sort(key=lambda p: os.path.basename(p).lower())
    return files


def ffprobe_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        die(out.stderr.strip() or "ffprobe failed")
    s = out.stdout.strip()
    if not s:
        die("ffprobe returned empty duration")
    return float(s)


def escape_drawtext_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("%", "%%")


def build_drawtext_options(text: str, size: int, fontfile: str, y_pos: float):
    parts = []
    if fontfile:
        escaped_font = fontfile.replace("'", "\\'")
        parts.append(f"fontfile='{escaped_font}'")
    parts.append(f"text='{escape_drawtext_text(text)}'")
    parts.append("fontcolor=white")
    parts.append(f"fontsize={int(size)}")
    parts.append("x=(w-text_w)/2")
    parts.append(f"y={y_pos:.2f}")
    parts.append("box=0")
    return ":".join(parts)


def ffmpeg_wave_color(base_color: str) -> str:
    """Return opaque waveform color derived from hex base color (ignore alpha)."""
    c = (base_color or '#ffffff').strip()
    if c.startswith('#') and len(c) == 7:
        return '0x' + c[1:]
    # Accept already in 0xRRGGBB or named color
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
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            die(f"Image conversion failed for {src}: {res.stderr.strip()}")
        conv_map[src] = outname
        converted.append(outname)
    return converted


def build_filter_complex(converted_inputs, audio_index, logo_index, seconds_per_image, transition_dur,
                          width, height, wave_h, bar_h, bar_color, text, text_size, fontfile,
                          xfade_name, overlay_y, drawbox_y, drawtext_y, logo_file,
                          blur_strength: int, wave_scale: float, force_rgba: bool):
    step = seconds_per_image - transition_dur
    fc_parts = []

    target_ar_expr = f"{width}/{height}"  # For readability

    for i in range(len(converted_inputs)):
        # Foreground: contain scaling + optional upscale so one dimension matches target.
        scale_expr = (
            f"scale='if(gte(iw/ih,{target_ar_expr}),{width},-1)':'if(gte(iw/ih,{target_ar_expr}),-1,{height})'"
        )
        fg_format = "rgba" if force_rgba else "yuva420p"
        fc_parts.append(
            f"[{i}:v]format={fg_format},setsar=1,{scale_expr},trim=duration={seconds_per_image:.6f},setpts=PTS-STARTPTS[fg{i}]"
        )
        # Background: stretch + adjustable blur (lower blur_strength improves speed)
        bg_format = "rgba" if force_rgba else "yuva420p"
        fc_parts.append(
            f"[{i}:v]format={bg_format},setsar=1,scale={width}:{height},boxblur={blur_strength}:1,trim=duration={seconds_per_image:.6f},setpts=PTS-STARTPTS[bg{i}]"
        )
        # Overlay fg centered on blurred bg
        # Overlay; if alpha path not forced we stay in yuv420p to avoid RGBA conversions
        fc_parts.append(f"[bg{i}][fg{i}]overlay=x=(W-w)/2:y=(H-h)/2,format=yuva420p[v{i}]")

    # Build slideshow with xfade transitions
    if len(converted_inputs) == 1:
        fc_parts.append("[v0]format=yuva420p[slide]")
    else:
        for k in range(len(converted_inputs) - 1):
            in1 = "[v0]" if k == 0 else f"[xf{k}]"
            in2 = f"[v{k+1}]"
            offset = step * (k + 1)
            fc_parts.append(
                f"{in1}{in2}xfade=transition={xfade_name}:duration={transition_dur:.6f}:offset={offset:.6f},format=yuv420p[xf{k+1}]"
            )
        last = len(converted_inputs) - 1
        fc_parts.append(f"[xf{last}]format=yuva420p[slide]")

    # Bar + optional text
    if bar_h > 0:
        if text:
            drawtext_opts = build_drawtext_options(text, text_size, fontfile, drawtext_y)
            fc_parts.append(
                f"[slide]drawbox=x=0:y={int(drawbox_y)}:w=iw:h={bar_h}:color={bar_color}:t=fill,drawtext={drawtext_opts}[slide_bar]"
            )
        else:
            fc_parts.append(
                f"[slide]drawbox=x=0:y={int(drawbox_y)}:w=iw:h={bar_h}:color={bar_color}:t=fill[slide_bar]"
            )
    else:
        fc_parts.append("[slide]copy[slide_bar]")

    # Waveform (optionally generated at reduced horizontal resolution then scaled up to save CPU)
    wave_color = ffmpeg_wave_color(bar_color)
    gen_wave_w = max(2, int(width * max(0.05, min(wave_scale, 1.0))))
    wave_fmt = "yuva420p" if force_rgba else "yuva420p"
    fc_parts.append(
        f"[{audio_index}:a]showwaves=s={gen_wave_w}x{wave_h}:mode=cline:colors={wave_color},format={wave_fmt}[wave_raw]"
    )
    if gen_wave_w != width:
        fc_parts.append(f"[wave_raw]scale={width}:{wave_h}:flags=bilinear[wave]")
    else:
        fc_parts.append(f"[wave_raw]copy[wave]")
    fc_parts.append(
        f"[slide_bar][wave]overlay=x=(W-w)/2:y={overlay_y:.2f}:format=auto[mid]"
    )
    fc_parts.append("[mid]setsar=1[mid_fixed]")

    # Logo
    if logo_file:
        logo_fmt = "rgba" if force_rgba else "yuva420p"
        fc_parts.append(
            f"[{logo_index}:v]format={logo_fmt},setsar=1,scale=105:105[logo_scaled]"
        )
        fc_parts.append(
            "[mid_fixed][logo_scaled]overlay=x=10:y=H-h-10:format=auto[outv]"
        )
    else:
        fc_parts.append("[mid_fixed]copy[outv]")

    return ";".join(fc_parts)


# def build_ffmpeg_command(converted_inputs, audio_file, logo_file, filter_complex,
#                           seconds_per_image, output_file, audio_index):
#     ff_args = ["ffmpeg", "-y"]
#     for img in converted_inputs:
#         ff_args += ["-loop", "1", "-t", f"{seconds_per_image:.6f}", "-i", img]
#     ff_args += ["-i", audio_file]
#     if logo_file:
#         ff_args += ["-i", logo_file]

#     ff_args += [
#         "-filter_complex", filter_complex,
#         "-map", "[outv]", "-map", f"{audio_index}:a",
#         "-c:v", "h264_videotoolbox",
#         "-b:v", "8M",
#         "-maxrate", "10M",
#         "-bufsize", "20M",
#         "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
#         "-c:a", "aac",
#         "-pix_fmt", "yuv420p",
#         "-hwaccel", "videotoolbox",
#         "-hwaccel_output_format", "videotoolbox",
#         "-shortest",
#         output_file
#     ]
#     return ff_args
def build_ffmpeg_command(converted_inputs, audio_file, logo_file, filter_complex,
                          seconds_per_image, output_file, audio_index,
                          encoder: str, bitrate: Optional[str], crf: Optional[int], threads: Optional[int]):
    ff_args = ["ffmpeg", "-y"]
    # inputs: one looping image per converted input
    for img in converted_inputs:
        ff_args += ["-loop", "1", "-t", f"{seconds_per_image:.6f}", "-i", img]
    # audio input
    ff_args += ["-i", audio_file]
    # optional logo input
    if logo_file:
        ff_args += ["-i", logo_file]

    # filtergraph and mapping then encoder options
    ff_args += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", f"{audio_index}:a"]

    # Video encoding strategy
    if encoder == "x264":
        ff_args += ["-c:v", "libx264"]
        if crf is not None:
            ff_args += ["-crf", str(crf), "-preset", "veryfast"]
        if bitrate:
            ff_args += ["-b:v", bitrate]
    elif encoder == "vt_h264":
        # macOS VideoToolbox H.264
        ff_args += ["-c:v", "h264_videotoolbox"]
        if bitrate:
            ff_args += ["-b:v", bitrate]
        else:
            # Use quality scale (lower is better); map CRF-like number roughly
            if crf is not None:
                q = min(100, max(0, 35 + (crf - 18) * 3))  # heuristic mapping
                ff_args += ["-q:v", str(q)]
    elif encoder == "vt_hevc":
        ff_args += ["-c:v", "hevc_videotoolbox"]
        if bitrate:
            ff_args += ["-b:v", bitrate]
    else:
        # Fallback
        ff_args += ["-c:v", "libx264", "-crf", str(crf or 18), "-preset", "veryfast"]

    if threads and encoder == "x264":
        ff_args += ["-threads", str(threads)]

    ff_args += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output_file]
    return ff_args


def parse_args():
    p = argparse.ArgumentParser(description="Generate a waveform slideshow video from images and an MP3.")
    p.add_argument("--images", required=True, help="Directory containing input images")
    p.add_argument("--audio", required=True, help="MP3 audio file path")
    p.add_argument("--output", required=True, help="Output video file path (e.g. out.mp4)")
    p.add_argument("--width", type=int, required=True, help="Target video width")
    p.add_argument("--height", type=int, required=True, help="Target video height")
    p.add_argument("--seconds-per-image", type=float, default=20.0, help="Seconds each image remains before transition")
    p.add_argument("--fade-duration", type=float, default=1.0, help="Crossfade transition duration in seconds")
    p.add_argument("--wave-height", type=int, default=240, help="Height of waveform visualization in pixels")
    p.add_argument("--bar-height", type=int, default=120, help="Height of bottom bar")
    p.add_argument("--bar-color", default="#000000", help="Bottom bar base color (hex, e.g. #000000)")
    p.add_argument("--bar-alpha", type=float, default=0.7, help="Bottom bar alpha (0.0 - 1.0)")
    # Legacy combined color flag (hex@alpha). If provided, overrides bar-color/bar-alpha.
    p.add_argument("--color", help="(Deprecated) Combined bar color hex@alpha, e.g. #000000@0.7")
    p.add_argument("--text", default="", help="Text to display centered in bottom bar")
    p.add_argument("--text-size", type=int, default=42, help="Font size for bottom bar text")
    p.add_argument("--font", default="", help="Optional font file path for drawtext")
    p.add_argument("--fade-name", default="fade", help="FFmpeg xfade transition name (e.g. fade, wipeleft, circleopen)")
    p.add_argument("--logo", default="", help="Optional logo image path")
    p.add_argument("--wave-y", type=float, default=None, help="Optional Y position (pixels) for waveform overlay (overrides auto-centering)")
    p.add_argument("--encoder", default="auto", choices=["auto", "x264", "vt_h264", "vt_hevc"], help="Video encoder: auto tries h264_videotoolbox if available on macOS")
    p.add_argument("--bitrate", help="Target video bitrate (e.g. 6M). If set, used instead of CRF for chosen encoder where applicable.")
    p.add_argument("--crf", type=int, default=18, help="CRF value for x264 (quality). Ignored if --bitrate provided or hardware encoder without CRF.")
    p.add_argument("--threads", type=int, help="Threads for libx264 encoding (ignored for VideoToolbox).")
    p.add_argument("--blur", type=int, default=10, help="Background blur strength (boxblur radius). Lower improves speed.")
    p.add_argument("--wave-scale", type=float, default=1.0, help="Horizontal scale factor (0.1-1.0) to generate waveform at reduced width for performance, then upscale.")
    p.add_argument("--force-rgba", action="store_true", help="Force RGBA intermediate pixel format (may slow down; use only if transparency processing explicitly needed).")
    return p.parse_args()


def main():
    args = parse_args()

    check_dep("ffmpeg")
    check_dep("ffprobe")

    if not os.path.isdir(args.images):
        die(f"Images directory not found: {args.images}")
    if not os.path.isfile(args.audio):
        die(f"Audio file not found: {args.audio}")
    if args.logo and not os.path.isfile(args.logo):
        die(f"Logo file not found: {args.logo}")

    images = gather_images(args.images)
    if not images:
        die("No images found (supported: jpg/jpeg/png/gif)")

    try:
        audio_duration = ffprobe_duration(args.audio)
    except Exception as e:
        die(f"Could not determine audio duration: {e}")

    needed = int(ceil(audio_duration / args.seconds_per_image))
    needed = max(1, needed)
    chosen = [images[i % len(images)] for i in range(needed)]

    tmpdir = tempfile.mkdtemp(prefix="wave_jpeg_")
    try:
        converted = convert_images_to_jpeg(chosen, tmpdir, quality=2)
        # Adjust fade if too large
        fade_dur = args.fade_duration
        if fade_dur >= args.seconds_per_image:
            fade_dur = args.seconds_per_image / 2.0
            print(f"Warning: fade-duration >= seconds-per-image; reduced to {fade_dur}")

        drawbox_y = args.height - args.bar_height
        drawtext_y = drawbox_y + (args.bar_height - args.text_size) / 2.0
        overlay_y = (args.height - args.wave_height) / 2.0

        # If user provided --wave-y, override the computed overlay_y
        if args.wave_y is not None:
            overlay_y = float(args.wave_y)

        audio_index = len(converted)
        logo_index = audio_index + 1 if args.logo else None

        # Compose bar color with alpha for drawbox, but waveform uses opaque base color.
        # Handle deprecated --color flag if present.
        if getattr(args, 'color', None):
            legacy = args.color.strip()
            if '@' in legacy:
                base, alpha = legacy.split('@', 1)
                try:
                    alpha_f = float(alpha)
                    if 0 <= alpha_f <= 1:
                        args.bar_color = base
                        args.bar_alpha = alpha_f
                except ValueError:
                    print("Warning: invalid alpha in --color; falling back to provided --bar-alpha.")
            else:
                # No alpha specified; keep existing bar-alpha
                args.bar_color = legacy
        bar_color = f"{args.bar_color}@{args.bar_alpha:.3f}" if args.bar_color else f"#000000@{args.bar_alpha:.3f}"

        filter_complex = build_filter_complex(
            converted, audio_index, logo_index, args.seconds_per_image, fade_dur,
            args.width, args.height, args.wave_height, args.bar_height, bar_color,
            args.text, args.text_size, args.font, args.fade_name,
            overlay_y, drawbox_y, drawtext_y, args.logo,
            blur_strength=args.blur, wave_scale=args.wave_scale, force_rgba=args.force_rgba
        )

        # Determine encoder auto mode
        encoder_choice = args.encoder
        if encoder_choice == "auto":
            # Prefer VideoToolbox on macOS if available
            if sys.platform == "darwin":
                encoder_choice = "vt_h264"
            else:
                encoder_choice = "x264"

        ff_cmd = build_ffmpeg_command(
            converted, args.audio, args.logo, filter_complex,
            args.seconds_per_image, args.output, audio_index,
            encoder=encoder_choice, bitrate=args.bitrate, crf=None if args.bitrate else args.crf, threads=args.threads
        )

        print("Running ffmpeg (truncated):")
        print(" ".join(ff_cmd[:10]) + " ... " + " ".join(ff_cmd[-10:]))

        proc = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                print(line, end="")
            proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            die("Interrupted by user")

        if proc.returncode != 0:
            die(f"ffmpeg failed with code {proc.returncode}")

        print("Done:", args.output)
        print(f"Audio length: {audio_duration:.2f}s, images used: {len(converted)}, resolution: {args.width}x{args.height}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception as e:
            print("Warning: could not remove temp dir", tmpdir, e, file=sys.stderr)


if __name__ == "__main__":
    main()
