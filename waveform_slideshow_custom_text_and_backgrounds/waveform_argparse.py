#!/usr/bin/env python3
"""waveform_slideshow.py

Generate a video slideshow from a folder of images and an MP3 audio track with:
 - Fixed target resolution (width x height)
 - Each image scaled to "contain" within target while preserving aspect ratio (upscale if smaller, downscale if larger)
 - Blurred stretched background of each image filling full frame
 - Crossfade transitions between images
 - Bottom semi-transparent bar with centered text
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
            --bar-color "#24d0b8" \
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from typing import Optional

SUPPORTED_EXTS = {'.jpg', '.jpeg', '.png', '.gif'}


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def check_dep(name: str):
    if shutil.which(name) is None:
        die(f"Required executable '{name}' not found in PATH.")


def _has_ffmpeg_encoder(name: str) -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return f" {name} " in res.stdout


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
    parts.append("fontcolor=#ffb400")
    parts.append(f"fontsize={size}")
    parts.append("x=(w-text_w)/2")
    parts.append(f"y={y_pos:.2f}")
    parts.append("box=0")
    return ":".join(parts)


def slides_needed_for_audio(audio_duration: float, seconds_per_image: float, fade_duration: float) -> int:
    """
    Number of image inputs needed so the xfade chain lasts at least audio_duration.

    Total duration for n slides:
        D + (n - 1) * (D - T)
    where:
        D = seconds_per_image
        T = fade_duration
    """
    if seconds_per_image <= 0:
        return 1

    step = seconds_per_image - fade_duration
    if step <= 0:
        return max(1, int(ceil(audio_duration / seconds_per_image)))

    if audio_duration <= seconds_per_image:
        return 1

    return max(1, 1 + int(ceil((audio_duration - seconds_per_image) / step)))


def slideshow_duration(num_items: int, seconds_per_image: float, transition_dur: float) -> float:
    if num_items <= 0:
        return 0.0
    if num_items == 1:
        return float(seconds_per_image)
    return float(seconds_per_image + (num_items - 1) * (seconds_per_image - transition_dur))


def build_slideshow_only_filter_complex(
    fg_inputs,
    bg_offset: int,
    seconds_per_image: float,
    transition_dur: float,
    width: int,
    height: int,
    xfade_name: str,
    force_rgba: bool,
    out_label: str = "slide",
):
    """Build filtergraph that outputs a slideshow video only (no bar/logo).

    bg inputs start at index bg_offset and are pre-rendered at target dimensions,
    so no scale or boxblur is needed in the filtergraph.
    """
    step = seconds_per_image - transition_dur
    fc_parts = []
    pix = "rgba" if force_rgba else "yuva420p"

    for i in range(len(fg_inputs)):
        fc_parts.append(
            f"[{i}:v]format={pix},setsar=1,trim=duration={seconds_per_image:.6f},setpts=PTS-STARTPTS[fg{i}]"
        )
        # bg is pre-blurred and pre-scaled; just set timing
        fc_parts.append(
            f"[{bg_offset + i}:v]format=yuv420p,setsar=1,trim=duration={seconds_per_image:.6f},setpts=PTS-STARTPTS[bg{i}]"
        )
        fc_parts.append(f"[bg{i}][fg{i}]overlay=x=(W-w)/2:y=(H-h)/2,format=yuv420p[v{i}]")

    if len(fg_inputs) == 1:
        fc_parts.append(f"[v0]format=yuv420p[{out_label}]")
    else:
        for k in range(len(fg_inputs) - 1):
            in1 = "[v0]" if k == 0 else f"[xf{k}]"
            in2 = f"[v{k+1}]"
            offset = step * (k + 1)
            fc_parts.append(
                f"{in1}{in2}xfade=transition={xfade_name}:duration={transition_dur:.6f}:offset={offset:.6f},format=yuv420p[xf{k+1}]"
            )
        last = len(fg_inputs) - 1
        fc_parts.append(f"[xf{last}]format=yuv420p[{out_label}]")

    return ";".join(fc_parts)


def build_overlay_filter_complex(
    slide_label: str,
    logo_index,
    width: int,
    height: int,
    bar_h: int,
    bar_color: str,
    text: str,
    text_size: int,
    fontfile: str,
    drawbox_y: int,
    drawtext_y: float,
    logo_file: str,
    force_rgba: bool,
):
    fc_parts = []
    slide_in = f"[{slide_label}]"

    if bar_h > 0:
        if text:
            drawtext_opts = build_drawtext_options(text, text_size, fontfile, drawtext_y)
            fc_parts.append(
                f"{slide_in}drawbox=x=0:y={drawbox_y}:w=iw:h={bar_h}:color={bar_color}:t=fill,drawtext={drawtext_opts}[slide_bar]"
            )
        else:
            fc_parts.append(
                f"{slide_in}drawbox=x=0:y={drawbox_y}:w=iw:h={bar_h}:color={bar_color}:t=fill[slide_bar]"
            )
    else:
        fc_parts.append(f"{slide_in}copy[slide_bar]")

    if logo_file:
        logo_fmt = "rgba" if force_rgba else "yuva420p"
        fc_parts.append(f"[{logo_index}:v]format={logo_fmt},setsar=1,scale=105:105[logo_scaled]")
        fc_parts.append("[slide_bar]setsar=1[slide_fixed]")
        fc_parts.append("[slide_fixed][logo_scaled]overlay=x=10:y=H-h-10:format=auto[outv]")
    else:
        fc_parts.append("[slide_bar]setsar=1[outv]")

    return ";".join(fc_parts)


def build_filter_complex(
    fg_inputs,
    bg_offset,
    logo_index,
    seconds_per_image,
    transition_dur,
    width,
    height,
    bar_h,
    bar_color,
    text,
    text_size,
    fontfile,
    xfade_name,
    drawbox_y,
    drawtext_y,
    logo_file,
    force_rgba: bool,
):
    slide_fc = build_slideshow_only_filter_complex(
        fg_inputs, bg_offset, seconds_per_image, transition_dur, width, height,
        xfade_name, force_rgba, out_label="slide",
    )
    overlay_fc = build_overlay_filter_complex(
        "slide", logo_index, width, height, bar_h, bar_color, text, text_size,
        fontfile, drawbox_y, drawtext_y, logo_file, force_rgba,
    )
    return slide_fc + ";" + overlay_fc


def build_ffmpeg_command(
    fg_inputs,
    bg_inputs,
    audio_file,
    logo_file,
    filter_complex,
    seconds_per_image,
    output_file,
    audio_index,
    encoder: str,
    bitrate: Optional[str],
    crf: Optional[int],
    threads: Optional[int],
):
    ff_args = ["ffmpeg", "-y"]
    for img in fg_inputs:
        ff_args += ["-loop", "1", "-t", f"{seconds_per_image:.6f}", "-i", img]
    for img in bg_inputs:
        ff_args += ["-loop", "1", "-t", f"{seconds_per_image:.6f}", "-i", img]
    ff_args += ["-i", audio_file]
    if logo_file:
        ff_args += ["-i", logo_file]

    ff_args += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", f"{audio_index}:a"]

    if encoder == "x264":
        ff_args += ["-c:v", "libx264"]
        if crf is not None:
            ff_args += ["-crf", str(crf), "-preset", "veryfast"]
        if bitrate:
            ff_args += ["-b:v", bitrate]
    elif encoder == "vt_h264":
        ff_args += ["-c:v", "h264_videotoolbox"]
        if bitrate:
            ff_args += ["-b:v", bitrate]
        else:
            if crf is not None:
                q = min(100, max(0, 35 + (crf - 18) * 3))
                ff_args += ["-q:v", str(q)]
    elif encoder == "vt_hevc":
        ff_args += ["-c:v", "hevc_videotoolbox"]
        if bitrate:
            ff_args += ["-b:v", bitrate]
    else:
        ff_args += ["-c:v", "libx264", "-crf", str(crf or 18), "-preset", "veryfast"]

    if threads and encoder == "x264":
        ff_args += ["-threads", str(threads)]

    ff_args += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output_file]
    return ff_args


def run_ffmpeg_stream(cmd):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        die("Interrupted by user")
    return proc.returncode


def _render_segment(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stderr.strip()


def _available_ram_mb() -> int:
    """Return available physical RAM in MB via portable POSIX sysconf (Linux + macOS)."""
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES') // (1024 * 1024)
    except (AttributeError, ValueError, OSError):
        return 0


def _convert_image_pair(src: str, fg_out: str, bg_out: str, width: int, height: int, blur: int, quality: int):
    """Convert one source image into a fg JPEG (pre-scaled to contain) and a pre-blurred/scaled bg JPEG."""
    scale_fg = f"scale='if(gte(iw/ih,{width}/{height}),{width},-1)':'if(gte(iw/ih,{width}/{height}),-1,{height})'"
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-filter_complex",
        f"[0:v]split=2[a][b];[a]{scale_fg}[fg];[b]scale={width}:{height},boxblur={blur}:1[bg]",
        "-map", "[fg]", "-frames:v", "1", "-q:v", str(quality), fg_out,
        "-map", "[bg]", "-frames:v", "1", "-q:v", str(quality), bg_out,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stderr.strip()


def convert_images_to_jpeg(src_list, tmpdir, width: int, height: int, blur: int, quality=2):
    """Convert images to JPEG pairs (fg + pre-blurred bg) in parallel.

    Returns (fg_list, bg_list) parallel to src_list.
    Duplicate source paths share one converted pair.
    """
    unique_srcs = list(dict.fromkeys(src_list))
    fg_map = {src: os.path.join(tmpdir, f"fg_{i:04d}.jpg") for i, src in enumerate(unique_srcs)}
    bg_map = {src: os.path.join(tmpdir, f"bg_{i:04d}.jpg") for i, src in enumerate(unique_srcs)}

    # Each worker runs one ffmpeg process for bg (scale+blur): ~200 MB overhead + frame buffers.
    avail_mb = _available_ram_mb()
    mem_per_worker_mb = 200 + max(1, width * height * 5 // (1024 * 1024))
    by_mem = max(1, int(avail_mb * 0.6) // mem_per_worker_mb) if avail_mb else (os.cpu_count() or 4)
    workers = min(len(unique_srcs), os.cpu_count() or 4, by_mem)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_convert_image_pair, src, fg_map[src], bg_map[src], width, height, blur, quality): src
            for src in unique_srcs
        }
        for future in as_completed(futures):
            src = futures[future]
            rc, stderr = future.result()
            if rc != 0:
                die(f"Image conversion failed for {src}: {stderr}")

    return [fg_map[src] for src in src_list], [bg_map[src] for src in src_list]


def parse_args():
    p = argparse.ArgumentParser(description="Generate a slideshow video from images and an MP3.")
    p.add_argument("--images", required=True, help="Directory containing input images")
    p.add_argument("--audio", required=True, help="MP3 audio file path")
    p.add_argument("--output", required=True, help="Output video file path (e.g. out.mp4)")
    p.add_argument("--width", type=int, required=True, help="Target video width")
    p.add_argument("--height", type=int, required=True, help="Target video height")
    p.add_argument("--seconds-per-image", type=float, default=20.0, help="Seconds each image remains before transition")
    p.add_argument("--fade-duration", type=float, default=1.0, help="Crossfade transition duration in seconds")
    p.add_argument("--bar-height", type=int, default=120, help="Height of bottom bar")
    p.add_argument("--bar-color", default="#24d0b8", help="Bottom bar base color (hex, e.g. #000000)")
    p.add_argument("--bar-alpha", type=float, default=0.7, help="Bottom bar alpha (0.0 - 1.0)")
    p.add_argument("--color", help="(Deprecated) Combined bar color hex@alpha, e.g. #000000@0.7")
    p.add_argument("--text", default="", help="Text to display centered in bottom bar")
    p.add_argument("--text-size", type=int, default=42, help="Font size for bottom bar text")
    p.add_argument("--font", default="", help="Optional font file path for drawtext")
    p.add_argument("--fade-name", default="fade", help="FFmpeg xfade transition name (e.g. fade, wipeleft, circleopen)")
    p.add_argument("--logo", default="", help="Optional logo image path")
    p.add_argument("--encoder", default="auto", choices=["auto", "x264", "vt_h264", "vt_hevc"], help="Video encoder: auto tries h264_videotoolbox if available on macOS")
    p.add_argument("--bitrate", help="Target video bitrate (e.g. 6M). If set, used instead of CRF for chosen encoder where applicable.")
    p.add_argument("--crf", type=int, default=18, help="CRF value for x264 (quality). Ignored if --bitrate provided or hardware encoder without CRF.")
    p.add_argument("--threads", type=int, help="Threads for libx264 encoding (ignored for VideoToolbox).")
    p.add_argument("--blur", type=int, default=10, help="Background blur strength (boxblur radius). Applied once during preprocessing.")
    p.add_argument("--force-rgba", action="store_true", help="Force RGBA intermediate pixel format (may slow down; use only if transparency processing explicitly needed).")
    p.add_argument("--max-inputs-per-pass", type=int, default=15, help="Max fg images per ffmpeg pass (bg images double the total; lower to reduce per-process RAM).")
    p.add_argument("--segment-workers", type=int, default=None, help="Parallel workers for segment rendering. Default: auto-sized to fit in available RAM.")
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

    fade_dur = args.fade_duration
    if fade_dur >= args.seconds_per_image:
        fade_dur = args.seconds_per_image / 2.0
        print(f"Warning: fade-duration >= seconds-per-image; reduced to {fade_dur}")

    needed = slides_needed_for_audio(audio_duration, args.seconds_per_image, fade_dur)
    chosen = [images[i % len(images)] for i in range(needed)]

    tmpdir = tempfile.mkdtemp(prefix="wave_jpeg_")
    try:
        fg_converted, bg_converted = convert_images_to_jpeg(
            chosen, tmpdir, width=args.width, height=args.height, blur=args.blur, quality=2,
        )

        drawbox_y = int(args.height - args.bar_height)
        drawtext_y = drawbox_y + (args.bar_height - args.text_size) / 2.0

        # fg inputs: 0..N-1, bg inputs: N..2N-1, audio: 2N, logo: 2N+1
        bg_offset = len(fg_converted)
        audio_index = bg_offset + len(bg_converted)
        logo_index = audio_index + 1 if args.logo else None

        if args.color:
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
                args.bar_color = legacy

        bar_color = f"{args.bar_color}@{args.bar_alpha:.3f}" if args.bar_color else f"#000000@{args.bar_alpha:.3f}"

        filter_complex = build_filter_complex(
            fg_converted, bg_offset, logo_index, args.seconds_per_image, fade_dur,
            args.width, args.height, args.bar_height, bar_color,
            args.text, args.text_size, args.font, args.fade_name,
            drawbox_y, drawtext_y, args.logo, force_rgba=args.force_rgba,
        )

        encoder_choice = args.encoder
        if encoder_choice == "auto":
            if sys.platform == "darwin" and _has_ffmpeg_encoder("h264_videotoolbox"):
                encoder_choice = "vt_h264"
            else:
                encoder_choice = "x264"

        if args.max_inputs_per_pass and len(fg_converted) > args.max_inputs_per_pass:
            chunk = max(2, args.max_inputs_per_pass)
            print(f"Large slideshow ({len(fg_converted)} images). Rendering in chunks of {chunk} to avoid ffmpeg resource exhaustion...")

            fg_segments = [fg_converted[i:i + chunk] for i in range(0, len(fg_converted), chunk)]
            bg_segments = [bg_converted[i:i + chunk] for i in range(0, len(bg_converted), chunk)]

            # Each segment process: ~250 MB ffmpeg overhead + ~15 MB per fg+bg input pair.
            avail_mb = _available_ram_mb()
            mem_per_seg_mb = 250 + chunk * 15
            by_mem = max(1, int(avail_mb * 0.6) // mem_per_seg_mb) if avail_mb else 1
            by_cpu = max(1, (os.cpu_count() or 2) // 2)
            n_workers = min(len(fg_segments), by_cpu, by_mem)
            if args.segment_workers is not None:
                n_workers = max(1, min(len(fg_segments), args.segment_workers))
            # Divide cores evenly so parallel ffmpeg processes don't thrash each other.
            seg_threads = args.threads or max(1, (os.cpu_count() or 1) // n_workers)

            # Build all segment commands before submitting so n_workers/seg_threads are fixed.
            segment_tasks = []
            for si, (seg_fg, seg_bg) in enumerate(zip(fg_segments, bg_segments)):
                seg_out = os.path.join(tmpdir, f"segment_{si:03d}.mp4")
                seg_filter = build_slideshow_only_filter_complex(
                    seg_fg,
                    bg_offset=len(seg_fg),
                    seconds_per_image=args.seconds_per_image,
                    transition_dur=fade_dur,
                    width=args.width,
                    height=args.height,
                    xfade_name=args.fade_name,
                    force_rgba=args.force_rgba,
                    out_label="slide",
                )
                seg_cmd = ["ffmpeg", "-y"]
                for img in seg_fg:
                    seg_cmd += ["-loop", "1", "-t", f"{args.seconds_per_image:.6f}", "-i", img]
                for img in seg_bg:
                    seg_cmd += ["-loop", "1", "-t", f"{args.seconds_per_image:.6f}", "-i", img]
                seg_cmd += ["-filter_complex", seg_filter, "-map", "[slide]", "-an"]
                if encoder_choice == "vt_h264":
                    seg_cmd += ["-c:v", "h264_videotoolbox", "-b:v", args.bitrate or "8M"]
                elif encoder_choice == "vt_hevc":
                    seg_cmd += ["-c:v", "hevc_videotoolbox", "-b:v", args.bitrate or "8M"]
                else:
                    seg_cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "ultrafast",
                                "-threads", str(seg_threads)]
                seg_cmd += ["-pix_fmt", "yuv420p", seg_out]
                segment_tasks.append((si, seg_cmd, seg_out))

            print(f"Rendering {len(segment_tasks)} segments with {n_workers} parallel workers "
                  f"({seg_threads} threads each)...")
            segment_paths = [None] * len(segment_tasks)
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = {executor.submit(_render_segment, cmd): (si, out)
                           for si, cmd, out in segment_tasks}
                for future in as_completed(futures):
                    si, seg_out = futures[future]
                    rc, stderr = future.result()
                    if rc != 0:
                        die(f"ffmpeg failed while rendering segment {si} (code {rc}):\n{stderr[-500:]}")
                    print(f"Segment {si + 1}/{len(segment_tasks)} done.")
                    segment_paths[si] = seg_out

            final_cmd = ["ffmpeg", "-y"]
            for seg in segment_paths:
                final_cmd += ["-i", seg]
            final_cmd += ["-i", args.audio]
            if args.logo:
                final_cmd += ["-i", args.logo]

            num_segs = len(segment_paths)
            audio_index = num_segs
            logo_index = audio_index + 1 if args.logo else None

            fc_parts = []
            seg_durs = [slideshow_duration(len(s), args.seconds_per_image, fade_dur) for s in fg_segments]
            for i in range(num_segs):
                fc_parts.append(f"[{i}:v]setpts=PTS-STARTPTS,format=yuv420p[sv{i}]")

            if num_segs == 1:
                fc_parts.append("[sv0]copy[slide]")
            else:
                cum = seg_durs[0]
                for k in range(num_segs - 1):
                    in1 = "[sv0]" if k == 0 else f"[sxf{k}]"
                    in2 = f"[sv{k + 1}]"
                    offset = cum - fade_dur
                    fc_parts.append(
                        f"{in1}{in2}xfade=transition={args.fade_name}:duration={fade_dur:.6f}:offset={offset:.6f},format=yuv420p[sxf{k + 1}]"
                    )
                    cum += seg_durs[k + 1] - fade_dur
                fc_parts.append(f"[sxf{num_segs - 1}]copy[slide]")

            fc_parts.append(
                build_overlay_filter_complex(
                    slide_label="slide",
                    logo_index=logo_index,
                    width=args.width,
                    height=args.height,
                    bar_h=args.bar_height,
                    bar_color=bar_color,
                    text=args.text,
                    text_size=args.text_size,
                    fontfile=args.font,
                    drawbox_y=drawbox_y,
                    drawtext_y=drawtext_y,
                    logo_file=args.logo,
                    force_rgba=args.force_rgba,
                )
            )

            filter_complex2 = ";".join(fc_parts)
            final_cmd += ["-filter_complex", filter_complex2, "-map", "[outv]", "-map", f"{audio_index}:a"]

            if encoder_choice == "x264":
                final_cmd += ["-c:v", "libx264"]
                if args.bitrate:
                    final_cmd += ["-b:v", args.bitrate]
                else:
                    final_cmd += ["-crf", str(args.crf), "-preset", "veryfast"]
                if args.threads:
                    final_cmd += ["-threads", str(args.threads)]
            elif encoder_choice == "vt_h264":
                final_cmd += ["-c:v", "h264_videotoolbox"]
                if args.bitrate:
                    final_cmd += ["-b:v", args.bitrate]
            elif encoder_choice == "vt_hevc":
                final_cmd += ["-c:v", "hevc_videotoolbox"]
                if args.bitrate:
                    final_cmd += ["-b:v", args.bitrate]
            else:
                final_cmd += ["-c:v", "libx264", "-crf", str(args.crf), "-preset", "veryfast"]

            final_cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", args.output]

            print("Running ffmpeg (final pass, truncated):")
            print(" ".join(final_cmd[:10]) + " ... " + " ".join(final_cmd[-10:]))
            rc = run_ffmpeg_stream(final_cmd)
            if rc != 0:
                die(f"ffmpeg failed with code {rc}")
        else:
            ff_cmd = build_ffmpeg_command(
                fg_converted, bg_converted, args.audio, args.logo, filter_complex,
                args.seconds_per_image, args.output, audio_index,
                encoder=encoder_choice, bitrate=args.bitrate, crf=None if args.bitrate else args.crf, threads=args.threads,
            )

            print("Running ffmpeg (truncated):")
            print(" ".join(ff_cmd[:10]) + " ... " + " ".join(ff_cmd[-10:]))

            rc = run_ffmpeg_stream(ff_cmd)
            if rc != 0:
                die(f"ffmpeg failed with code {rc}")

        print("Done:", args.output)
        print(f"Audio length: {audio_duration:.2f}s, images used: {len(fg_converted)}, resolution: {args.width}x{args.height}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception as e:
            print("Warning: could not remove temp dir", tmpdir, e, file=sys.stderr)


if __name__ == "__main__":
    main()
