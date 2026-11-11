#!/usr/bin/env python3
"""waveform_slideshow.py

Create a video slideshow from images and audio.
Create a video with a
- Fixed target resolution (width x height)
- Each image scaled to "contain" within target while preserving aspect ratio
- Blurred, full-frame background of each image
- Crossfade transitions between images

Usage example:
    python3 waveform_argparse.py \
            --images imgs \
            --audio track.mp3 \
            --output out.mp4 \
            --width 1920 --height 1080 \
            --seconds-per-image 10 \
            --fade-duration 0.5 \
            --zoom --zoom-end-percent 110 \
            --bar-blur 20 \
            --text "Sample Title" \
            --text-size 42 \
            --font /Library/Fonts/Arial.ttf \
            --logo logo.png

Pipeline:
  1. Preprocess (parallel): every unique source image becomes ONE pre-composed
     still at the target resolution -- the "contain"-scaled image centred over
     its own blurred, stretched copy.
  2. Render (parallel): the timeline is split into segments. Each ffmpeg
     process reads its slides as single frames (decode + zoom supersampling
     happen once per slide, not per frame), animates and crossfades them,
     adds the bottom bar, text and logo, and encodes with the final settings.
  3. Join: segments are concatenated without re-encoding and the audio added.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from math import ceil, pi
from typing import List, Optional, Sequence

SUPPORTED_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.heic'}

# Frame rate of the whole pipeline (matches FFmpeg's image2 default of 25 fps).
FPS = 25

# Supersampled frames for the zoom stage are capped at this width.
MAX_SUPERSAMPLE_WIDTH = 7680

# JPEG quality (-q:v, 2 = best) of the intermediate fg/bg images.
JPEG_QUALITY = 2

LOGO_SIZE = 105
LOGO_MARGIN = 10
TEXT_COLOR = "#ffb000"

# Title card: initial size ("contain" in this fraction of the frame) and the
# fraction of the bar height it shrinks to when it lands in the bar.
TITLE_CARD_FILL = 1.0
TITLE_CARD_LANDED_HEIGHT = 0.8


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Settings:
    """Validated, derived options shared by all pipeline stages."""
    width: int
    height: int
    seconds_per_image: float
    fade_dur: float
    fade_name: str
    bg_blur: int
    # look (applied once per slide during preprocessing)
    contrast: float
    saturation: float
    vignette: float
    # zoom
    zoom_enabled: bool
    zoom_end_percent: float
    zoom_supersample: int
    # bottom bar
    bar_height: int
    bar_style: str
    bar_color: str
    bar_alpha: float
    bar_blur: int
    bar_tint: str
    bar_tint_alpha: float
    bar_slide_in_delay: float
    bar_visible_duration: float
    bar_slide_duration: float
    text: str
    text_size: int
    font: str
    logo: str
    force_rgba: bool
    # title card
    title_card: str
    title_card_duration: float
    title_card_transition: float
    # encoding
    encoder: str
    bitrate: Optional[str]
    crf: int
    threads: Optional[int]

    @property
    def bar_top(self) -> int:
        """y of the bar's top edge when fully visible."""
        return self.height - self.bar_height

    @property
    def supersample(self) -> int:
        """Effective zoom supersample factor (1 when zoom is off), capped by MAX_SUPERSAMPLE_WIDTH."""
        if not self.zoom_enabled:
            return 1
        return max(1, min(self.zoom_supersample, MAX_SUPERSAMPLE_WIDTH // max(1, self.width)))

    @property
    def title_card_end(self) -> float:
        """Seconds after which the title card is gone (0 = no card)."""
        return self.title_card_duration + self.title_card_transition if self.title_card else 0.0

    @property
    def frames_per_image(self) -> int:
        """Frames per slide; matches `-loop 1 -t <seconds>` (all frames with pts < duration)."""
        return max(1, ceil(self.seconds_per_image * FPS - 1e-9))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def check_dep(name: str):
    if shutil.which(name) is None:
        die(f"Required executable '{name}' not found in PATH.")


def run_quiet(cmd: Sequence[str]):
    """Run a command, return (returncode, stderr)."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stderr.strip()


def run_streaming(cmd: Sequence[str]) -> int:
    """Run a command, echoing its combined output live."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        die("Interrupted by user")
    return proc.returncode


def print_cmd(title: str, cmd: Sequence[str]):
    print(f"{title} (truncated):")
    print(" ".join(cmd[:10]) + " ... " + " ".join(cmd[-10:]))


def has_ffmpeg_encoder(name: str) -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return f" {name} " in res.stdout


def _macos_available_ram_mb() -> int:
    """Free + reclaimable (inactive, speculative, purgeable) pages from `vm_stat`.
    macOS has no SC_AVPHYS_PAGES and keeps "free" memory deliberately low."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    except OSError:
        return 0
    page_size = 4096
    pages = 0
    for line in out.splitlines():
        if "page size of" in line:
            page_size = int(line.split("page size of")[1].split()[0])
        key, _, val = line.partition(":")
        if key.strip() in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
            pages += int(val.strip().rstrip("."))
    return pages * page_size // (1024 * 1024)


def available_ram_mb() -> int:
    """Available physical RAM in MB (0 if unknown)."""
    if sys.platform == "darwin":
        return _macos_available_ram_mb()
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES') // (1024 * 1024)
    except (AttributeError, ValueError, OSError):
        return 0


def workers_for(n_tasks: int, mem_per_worker_mb: int, cpu_limit: int) -> int:
    """Worker count bounded by task count, CPU limit and ~60% of free RAM."""
    avail_mb = available_ram_mb()
    by_mem = max(1, int(avail_mb * 0.6) // mem_per_worker_mb) if avail_mb else cpu_limit
    return max(1, min(n_tasks, cpu_limit, by_mem))


def gather_images(images_dir: str) -> List[str]:
    files = [
        os.path.join(images_dir, entry)
        for entry in os.listdir(images_dir)
        if os.path.isfile(os.path.join(images_dir, entry))
        and os.path.splitext(entry)[1].lower() in SUPPORTED_EXTS
    ]
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


# --------------------------------------------------------------------------- #
# Timing math
# --------------------------------------------------------------------------- #

def slides_needed_for_audio(audio_duration: float, seconds_per_image: float, fade_duration: float) -> int:
    """
    Number of slides needed so the xfade chain lasts at least audio_duration.

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


# --------------------------------------------------------------------------- #
# Preprocessing: source image -> one pre-composed slide still
# --------------------------------------------------------------------------- #

def decode_heic_to_png(src: str, out_path: str):
    """Decode a HEIC/HEIF image to PNG via macOS's built-in `sips`, since ffmpeg
    typically lacks a HEIF decoder."""
    return run_quiet(["sips", "-s", "format", "png", src, "--out", out_path])


def compose_slide(src: str, out_path: str, s: Settings, heic_tmp: Optional[str] = None):
    """Render one finished slide still: the image scaled to "contain" the
    target frame, centred over a stretched + blurred copy of itself.

    Step 1 writes fg/bg JPEGs (as the script always did -- this also fixes
    pixel-format/colour-range handling and drops alpha of PNG sources).
    Step 2 composites them ONCE with the same overlay chain the renderer used
    to run on every frame, and stores the result losslessly (raw yuv420p in
    MKV, which keeps the colour range/matrix tags). Slides are therefore
    pixel-identical to what the previous per-frame pipeline produced.
    """
    if heic_tmp:
        rc, err = decode_heic_to_png(src, heic_tmp)
        if rc != 0:
            return rc, err
        src = heic_tmp

    w, h = s.width, s.height
    base = os.path.splitext(out_path)[0]
    fg_jpg, bg_jpg = base + "_fg.jpg", base + "_bg.jpg"

    fits_width = f"gte(iw/ih,{w}/{h})"
    scale_fg = f"scale='if({fits_width},{w},-1)':'if({fits_width},-1,{h})'"
    rc, err = run_quiet([
        "ffmpeg", "-y", "-i", src,
        "-filter_complex", f"[0:v]split=2[a][b];[a]{scale_fg}[fg];[b]scale={w}:{h},boxblur={s.bg_blur}:1[bg]",
        "-map", "[fg]", "-frames:v", "1", "-q:v", str(JPEG_QUALITY), fg_jpg,
        "-map", "[bg]", "-frames:v", "1", "-q:v", str(JPEG_QUALITY), bg_jpg,
    ])
    if rc != 0:
        return rc, err

    fg_fmt = "rgba" if s.force_rgba else "yuva420p"
    return run_quiet([
        "ffmpeg", "-y", "-i", fg_jpg, "-i", bg_jpg,
        "-filter_complex",
        f"[0:v]format={fg_fmt},setsar=1[fg];[1:v]format=yuv420p,setsar=1[bg];"
        f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2{look_filters(s)},format=yuv420p[out]",
        "-map", "[out]", "-frames:v", "1", "-c:v", "rawvideo", out_path,
    ])


def look_filters(s: Settings) -> str:
    """Light colour grade + vignette, baked into each slide still (costs
    nothing per frame). Returns a ",..." suffix, or "" when everything is neutral."""
    parts = []
    if s.contrast != 1.0 or s.saturation != 1.0:
        parts.append(f"eq=contrast={s.contrast:.4f}:saturation={s.saturation:.4f}")
    if s.vignette > 0:
        parts.append(f"vignette=angle={s.vignette * pi / 2:.4f}")
    return "".join("," + p for p in parts)


def probe_image_size(path: str):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", path],
                         capture_output=True, text=True)
    try:
        w, h = (int(v) for v in out.stdout.strip().split(",")[:2])
    except ValueError:
        die(f"Could not read image size of {path}: {out.stderr.strip()}")
    return w, h


def prepare_title_card(tmpdir: str, s: Settings) -> Optional[str]:
    """Scale the title card once to its on-screen start size ("contain" in the
    frame, transparency kept) so the per-frame animation starts from a small image."""
    if not s.title_card:
        return None
    src = s.title_card
    if src.lower().endswith(".heic"):
        check_dep("sips")
        heic = os.path.join(tmpdir, "title_heic.png")
        rc, err = decode_heic_to_png(src, heic)
        if rc != 0:
            die(f"Title card conversion failed: {err}")
        src = heic
    iw, ih = probe_image_size(src)
    fit = min(s.width * TITLE_CARD_FILL / iw, s.height * TITLE_CARD_FILL / ih)
    w, h = max(2, int(iw * fit)), max(2, int(ih * fit))
    out = os.path.join(tmpdir, "title_card.png")
    rc, err = run_quiet(["ffmpeg", "-y", "-i", src, "-vf", f"format=rgba,scale={w}:{h}:flags=lanczos",
                         "-frames:v", "1", out])
    if rc != 0:
        die(f"Title card conversion failed: {err}")
    return out


def prepare_slides(src_list: List[str], tmpdir: str, s: Settings) -> List[str]:
    """Compose slide stills in parallel; returns paths parallel to src_list.
    Duplicate sources share one composed file."""
    unique_srcs = list(dict.fromkeys(src_list))
    out_map = {src: os.path.join(tmpdir, f"slide_{i:04d}.mkv") for i, src in enumerate(unique_srcs)}
    heic_map = {
        src: (os.path.join(tmpdir, f"heic_{i:04d}.png") if src.lower().endswith(".heic") else None)
        for i, src in enumerate(unique_srcs)
    }
    if any(heic_map.values()):
        check_dep("sips")

    # Two short single-frame ffmpeg runs per worker: ~200 MB overhead + frame buffers.
    mem_per_worker = 200 + max(1, s.width * s.height * 5 // (1024 * 1024))
    workers = workers_for(len(unique_srcs), mem_per_worker, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(compose_slide, src, out_map[src], s, heic_map[src]): src
            for src in unique_srcs
        }
        for future in as_completed(futures):
            rc, stderr = future.result()
            if rc != 0:
                die(f"Image conversion failed for {futures[future]}: {stderr}")

    return [out_map[src] for src in src_list]


# --------------------------------------------------------------------------- #
# Filtergraph builders
# --------------------------------------------------------------------------- #

def escape_drawtext_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("%", "%%")


def build_drawtext_options(text: str, size: int, fontfile: str, y_pos, alpha: str = "") -> str:
    parts = []
    if fontfile:
        escaped_font = fontfile.replace("'", "\\'")
        parts.append(f"fontfile='{escaped_font}'")
    parts += [
        f"text='{escape_drawtext_text(text)}'",
        f"fontcolor={TEXT_COLOR}",
        f"fontsize={size}",
        "x=(w-text_w)/2",
        f"y={y_pos}",
        "box=0",
    ]
    if alpha:
        parts.append(f"alpha={alpha}")
    return ":".join(parts)


# Last fraction of the title card's glide during which the card fades out and
# the bar text fades in (a crossfade, so the title seems to land in the bar).
TITLE_CARD_HANDOFF = 0.3


def title_handoff(s: Settings):
    """(start, duration) of the card -> bar-text crossfade."""
    trans = max(0.04, s.title_card_transition)
    return s.title_card_duration + trans * (1 - TITLE_CARD_HANDOFF), trans * TITLE_CARD_HANDOFF


def bar_text_alpha(s: Settings) -> str:
    """Bar text opacity: hidden until the title card lands, then fades in."""
    if not s.title_card:
        return ""
    st, d = title_handoff(s)
    return f"clip((t-{st:.4f})/{d:.4f}\\,0\\,1)"


def bar_y_expr(s: Settings) -> str:
    """FFmpeg y-position expression: bar hidden -> slides up after the delay ->
    visible for visible_duration -> slides down.

    \\, escapes commas so the filter graph parser does not treat them as filter separators.
    """
    S, D, V = s.bar_slide_duration, s.bar_slide_in_delay, s.bar_visible_duration
    if S <= 0:
        return str(s.bar_top)
    H, B, top = s.height, s.bar_height, s.bar_top
    # phases: hidden | sliding in | visible | sliding out | hidden
    return (
        f"if(lt(t\\,{D})\\,{H}\\,"
        f"if(lt(t\\,{D}+{S})\\,{H}-{B}*(t-{D})/{S}\\,"
        f"if(lt(t\\,{D}+{S}+{V})\\,{top}\\,"
        f"if(lt(t\\,{D}+{S}+{V}+{S})\\,{top}+{B}*(t-{D}-{S}-{V})/{S}\\,"
        f"{H}))))"
    )


def zoom_filters(s: Settings) -> str:
    """Filter chain that turns ONE still frame into a slide of frames_per_image
    frames slowly pushing toward the centre. zoom_end_percent is the on-screen
    scale on the last frame before the transition (100 = none, 110 = +10%).

    Anti-shake notes:
      * The zoom grows linearly with `on` (zoompan's output frame index, which
        restarts at 0 for every slide). zoompan's `zoom` accumulator is avoided.
      * The pan offsets x/y are derived from the *same* `on`-based expression as
        z, not from zoompan's internal quantized `zoom` variable. Mixing the two
        lets the crop window and the zoom level disagree by sub-pixel amounts
        each frame, which reads as a random-direction jitter.
      * zoompan snaps the crop origin to whole input pixels. At the default
        (very slow) zoom that step is a ~1px pop every few frames -- clearly
        visible. Cropping from a `supersample`x-enlarged frame shrinks the step
        to 1/supersample px; measured smooth (no direction reversals) at >=4.
        The factor is capped so the enlarged frame stays <= 7680 px wide.

    Performance: the input is a single frame, so the supersampling `scale` runs
    once per slide; zoompan (d=frames) then emits every frame from it.
    """
    target = s.zoom_end_percent / 100.0
    frames = s.frames_per_image
    inc = (target - 1.0) / max(1, int(round(s.seconds_per_image * FPS)))
    z = f"min(1+{inc:.8f}*on,{target:.6f})"
    ss = s.supersample
    pre = f"scale=iw*{ss}:ih*{ss}:flags=bicubic," if ss > 1 else ""
    return (
        f"{pre}zoompan=z='{z}'"
        f":x='(iw-iw/({z}))/2':y='(ih-ih/({z}))/2'"
        f":d={frames}:s={s.width}x{s.height}:fps={FPS}"
    )


def hold_filters(s: Settings) -> str:
    """Filter chain that repeats ONE still frame for frames_per_image frames
    (no re-decoding; frames are reference-counted copies)."""
    return f"loop=loop={s.frames_per_image - 1}:size=1,settb=1/{FPS},setpts=N"


def xfade_chain(in_labels: List[str], offsets_frames: List[int], s: Settings, out_label: str) -> List[str]:
    """Chain clips (filter labels) with xfade; clip k starts fading in at
    offsets_frames[k-1] (frames from the start of the chain).

    Performance: do NOT put `format=` between the xfades. It makes FFmpeg
    negotiate yuv444p for xfade and auto-insert yuv420p<->yuv444p scalers at
    every link, so each frame is converted once per *later* xfade in the chain
    (quadratic cost; measured ~7x slower for 8 slides). One format at the end.
    """
    parts = []
    prev = in_labels[0]
    for k, (label, off) in enumerate(zip(in_labels[1:], offsets_frames), start=1):
        out = f"xf{k}"
        parts.append(
            f"[{prev}][{label}]xfade=transition={s.fade_name}:duration={s.fade_dur:.6f}"
            f":offset={off / FPS:.6f}[{out}]"
        )
        prev = out
    parts.append(f"[{prev}]format=yuv420p[{out_label}]")
    return parts


def build_slideshow_filter(first_slide: int, n_slides: int, s: Settings, out_label: str) -> str:
    """Filtergraph for slides first_slide..first_slide+n-1, fed as single-frame
    inputs 0..n-1, -> [out_label]. Local time 0 = first slide's start."""
    motion = zoom_filters(s) if s.zoom_enabled else hold_filters(s)
    parts = [
        f"[{i}:v]format=yuv420p,setsar=1,{motion},format=yuv420p,"
        f"trim=duration={s.seconds_per_image:.6f}[v{i}]"
        for i in range(n_slides)
    ]
    origin = slide_start_frame(first_slide, s)
    offsets = [slide_start_frame(first_slide + i, s) - origin for i in range(1, n_slides)]
    parts += xfade_chain([f"v{i}" for i in range(n_slides)], offsets, s, out_label)
    return ";".join(parts)


def title_card_filter(src_label: str, card_index: int, card_size, s: Settings, out_label: str) -> str:
    """Title card over [src_label]: shown centred for title_card_duration, then
    shrinks and glides down into the bottom bar (eased) while the bar slides up
    underneath it; as it lands it crossfades into the bar text.

    `t` is global video time in both scale and overlay (the card input starts
    at 0 and segments are shifted to global time before this filter)."""
    cw, ch = card_size
    hold, trans = s.title_card_duration, max(0.04, s.title_card_transition)
    bar_h = s.bar_height if s.bar_height > 0 else s.height // 9
    end_scale = min(1.0, bar_h * TITLE_CARD_LANDED_HEIGHT / ch, s.width * 0.9 / cw)
    # eased progress 0..1 (smoothstep); \\, keeps commas inside the expressions
    p = f"clip((t-{hold:.4f})/{trans:.4f}\\,0\\,1)"
    e = f"({p})*({p})*(3-2*({p}))"
    z = f"(1-(1-{end_scale:.6f})*{e})"
    cy_end = s.height - bar_h / 2
    cy = f"({s.height / 2:.2f}+({cy_end:.2f}-{s.height / 2:.2f})*{e})"
    fade_start, fade_dur = title_handoff(s)
    return (
        f"[{card_index}:v]format=rgba,"
        f"scale=w={cw}*{z}:h={ch}*{z}:eval=frame:flags=bicubic,"
        f"fade=t=out:st={fade_start:.4f}:d={fade_dur:.4f}:alpha=1[tcard];"
        f"[{src_label}][tcard]overlay=x=(W-w)/2:y={cy}-h/2:eval=frame:eof_action=pass:format=auto,"
        f"format=yuv420p[{out_label}]"
    )


def build_overlay_filter(slide_label: str, logo_index: Optional[int], s: Settings) -> str:
    """Adds the animated bottom bar (+text) and the logo to [slide_label] -> [outv]."""
    parts = []
    src = f"[{slide_label}]"
    bar_y = bar_y_expr(s)
    bar_h = s.bar_height

    if bar_h <= 0:
        parts.append(f"{src}copy[slide_bar]")
    elif s.bar_style == "solid":
        color = f"{s.bar_color or '#000000'}@{s.bar_alpha:.3f}"
        chain = f"{src}drawbox=x=0:y={bar_y}:w=iw:h={bar_h}:color={color}:t=fill"
        if s.text:
            chain += ",drawtext=" + build_drawtext_options(
                s.text, s.text_size, s.font, f"{bar_y}+({bar_h}-text_h)/2", bar_text_alpha(s))
        parts.append(chain + "[slide_bar]")
    else:  # blur: frosted strip cut from the bar's resting position, slid in with overlay
        blur_chain = f"crop=iw:{bar_h}:0:{s.bar_top},boxblur={s.bar_blur}:5"
        if s.bar_tint:
            blur_chain += f",drawbox=x=0:y=0:w=iw:h=ih:color={s.bar_tint}@{s.bar_tint_alpha:.3f}:t=fill"
        if s.text:
            blur_chain += ",drawtext=" + build_drawtext_options(
                s.text, s.text_size, s.font, "(h-text_h)/2", bar_text_alpha(s))
        parts += [
            f"{src}split=2[base][blursrc]",
            f"[blursrc]{blur_chain}[blurredbar]",
            f"[base][blurredbar]overlay=x=0:y={bar_y}[slide_bar]",
        ]

    if s.logo:
        logo_fmt = "rgba" if s.force_rgba else "yuva420p"
        parts += [
            f"[{logo_index}:v]format={logo_fmt},setsar=1,scale={LOGO_SIZE}:{LOGO_SIZE}[logo_scaled]",
            "[slide_bar]setsar=1[slide_fixed]",
            f"[slide_fixed][logo_scaled]overlay=x={LOGO_MARGIN}:y=H-h-{LOGO_MARGIN}:format=auto[outv]",
        ]
    else:
        parts.append("[slide_bar]setsar=1[outv]")

    return ";".join(parts)


# --------------------------------------------------------------------------- #
# Encoder arguments
# --------------------------------------------------------------------------- #

def resolve_encoder(choice: str) -> str:
    if choice != "auto":
        return choice
    if sys.platform == "darwin" and has_ffmpeg_encoder("h264_videotoolbox"):
        return "vt_h264"
    return "x264"


def encoder_args(s: Settings, x264_threads: Optional[int]) -> List[str]:
    """Video encoder args. A bitrate takes precedence over CRF."""
    if s.encoder == "vt_h264":
        args = ["-c:v", "h264_videotoolbox"]
        if s.bitrate:
            args += ["-b:v", s.bitrate]
        else:
            args += ["-q:v", str(min(100, max(0, 35 + (s.crf - 18) * 3)))]
        return args
    if s.encoder == "vt_hevc":
        return ["-c:v", "hevc_videotoolbox"] + (["-b:v", s.bitrate] if s.bitrate else [])
    # x264
    args = ["-c:v", "libx264"]
    args += ["-b:v", s.bitrate] if s.bitrate else ["-crf", str(s.crf), "-preset", "veryfast"]
    if x264_threads:
        args += ["-threads", str(x264_threads)]
    return args


# --------------------------------------------------------------------------- #
# Segment planning
# --------------------------------------------------------------------------- #
#
# The final video is cut into consecutive time windows rendered by parallel
# ffmpeg processes, each encoded ONCE with the final encoder settings, then
# joined losslessly with the concat demuxer (-c copy).
#
# Why not render plain segments and re-encode them in one final pass (the old
# approach)? Apple's media engine has one shared H.264/HEVC encoder (~200 fps
# at 1080p on M2, no matter how many sessions run), so encoding everything
# twice made the encoder the bottleneck -- and cost one lossy generation.
#
# A window starts on the first frame *after* a crossfade has finished, so it
# never needs the previous slide; it renders its own slides plus the first
# slide of the next window to complete the crossfade at its end.

MIN_SLIDES_PER_SEGMENT = 3


@dataclass(frozen=True)
class Segment:
    index: int
    first_slide: int            # index into the slide list
    last_slide: int             # inclusive; overlaps the next segment's first slide
    trim_start: int             # local frame where this window starts
    trim_end: Optional[int]     # local frame where it ends (exclusive); None = to the end
    start_frame: int            # global frame index of the window's first frame


def slide_start_frame(j: int, s: Settings) -> int:
    """Global frame where slide j starts fading in (xfade offsets round to the nearest frame)."""
    return int(j * (s.seconds_per_image - s.fade_dur) * FPS + 0.5)


def segment_count(n_slides: int, max_per_segment: int, n_workers: int) -> int:
    """Enough segments to respect max_per_segment and keep every worker busy,
    rounded up to a multiple of n_workers so no worker idles in the last round."""
    required = ceil(n_slides / max_per_segment)
    useful_max = max(1, n_slides // MIN_SLIDES_PER_SEGMENT)  # more segments = tiny ones
    n_seg = max(required, min(n_workers, useful_max))
    n_seg = min(ceil(n_seg / n_workers) * n_workers, useful_max)
    return max(1, required, min(n_seg, n_slides))


def plan_segments(n_slides: int, n_seg: int, total_frames: int, s: Settings) -> List[Segment]:
    bounds = [round(k * n_slides / n_seg) for k in range(n_seg + 1)]
    fade_frames = ceil(s.fade_dur * FPS - 1e-9)

    def window_start(first: int) -> int:
        return 0 if first == 0 else slide_start_frame(first, s) + fade_frames

    segments = []
    for k in range(n_seg):
        a, b = bounds[k], bounds[k + 1]
        last = k == n_seg - 1
        origin = slide_start_frame(a, s)  # global frame of local frame 0
        start = window_start(a)
        end = total_frames if last else window_start(b)
        segments.append(Segment(
            index=k,
            first_slide=a,
            last_slide=n_slides - 1 if last else b,
            trim_start=start - origin,
            trim_end=end - origin,
            start_frame=start,
        ))
    return segments


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def still_inputs(paths: Sequence[str]) -> List[str]:
    """Each slide is read as a single frame (no -loop): decoded exactly once."""
    args = []
    for p in paths:
        args += ["-i", p]
    return args


def segment_command(seg: Segment, slides: List[str], out_path: str, s: Settings,
                    x264_threads: Optional[int], title_card: Optional[str] = None,
                    title_card_size=None) -> List[str]:
    seg_slides = slides[seg.first_slide:seg.last_slide + 1]
    n = len(seg_slides)
    logo_index = n if s.logo else None
    # The card only goes into segments that start before it has disappeared.
    use_card = bool(title_card) and seg.start_frame < s.title_card_end * FPS
    card_index = n + (1 if s.logo else 0)

    trim = f"trim=start_frame={seg.trim_start}"
    if seg.trim_end is not None:
        trim += f":end_frame={seg.trim_end}"
    graph = ";".join([
        build_slideshow_filter(seg.first_slide, n, s, out_label="chain"),
        # Shift to global time so the bar animation (which uses t) lines up across segments.
        f"[chain]{trim},setpts=PTS-STARTPTS+{seg.start_frame}/({FPS}*TB)[slide]",
        build_overlay_filter("slide", logo_index, s),
    ] + ([title_card_filter("outv", card_index, title_card_size, s, "titled")] if use_card else []) + [
        f"[{'titled' if use_card else 'outv'}]setpts=PTS-STARTPTS[seg]",
    ])
    cmd = ["ffmpeg", "-y"] + still_inputs(seg_slides)
    if s.logo:
        cmd += ["-i", s.logo]
    if use_card:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{s.title_card_end + 1 / FPS:.4f}",
                "-i", title_card]
    cmd += ["-filter_complex", graph, "-map", "[seg]", "-an"]
    cmd += encoder_args(s, x264_threads) + ["-pix_fmt", "yuv420p", out_path]
    return cmd


def render_segments(segments: List[Segment], slides: List[str], tmpdir: str, s: Settings,
                    n_workers: int, title_card: Optional[str] = None) -> List[str]:
    x264_threads = s.threads or max(1, (os.cpu_count() or 1) // n_workers)
    card_size = probe_image_size(title_card) if title_card else None
    tasks = []
    for seg in segments:
        out = os.path.join(tmpdir, f"segment_{seg.index:03d}.mp4")
        cmd = segment_command(seg, slides, out, s, x264_threads, title_card, card_size)
        tasks.append((seg.index, cmd, out))

    print(f"Rendering {len(tasks)} segments with {n_workers} parallel workers...")
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(run_quiet, cmd): si for si, cmd, _ in tasks}
        for done, future in enumerate(as_completed(futures), start=1):
            si = futures[future]
            rc, stderr = future.result()
            if rc != 0:
                die(f"ffmpeg failed while rendering segment {si} (code {rc}):\n{stderr[-500:]}")
            print(f"Segment {si + 1} done ({done}/{len(tasks)}).")
    return [out for _, _, out in tasks]


def join_segments(seg_paths: List[str], audio: str, out_path: str, tmpdir: str):
    """Concatenate encoded segments without re-encoding and add the audio track."""
    list_file = os.path.join(tmpdir, "segments.txt")
    with open(list_file, "w") as f:
        for p in seg_paths:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-i", audio,
           "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", out_path]
    print_cmd("Joining segments + audio", cmd)
    rc = run_streaming(cmd)
    if rc != 0:
        die(f"ffmpeg failed with code {rc}")


def render_video(slides: List[str], audio: str, audio_duration: float, out_path: str,
                 tmpdir: str, s: Settings, max_per_segment: int, workers_override: Optional[int],
                 title_card: Optional[str] = None):
    n = len(slides)
    cpus = os.cpu_count() or 2

    # Memory per process: ~250 MB ffmpeg overhead + per slide one (possibly
    # supersampled) yuv420p frame held by zoompan + buffers (measured ~1.3 GB
    # for 15 slides at 1080p with 4x supersampling).
    frame_mb = max(1, s.width * s.height * s.supersample ** 2 * 3 // 2 // (1024 * 1024))
    slides_per_seg = min(n, max_per_segment, max(MIN_SLIDES_PER_SEGMENT, ceil(n / cpus))) + 1
    if workers_override is not None:
        n_workers = max(1, workers_override)
    else:
        n_workers = workers_for(cpus, 250 + slides_per_seg * (40 + frame_mb), cpus)

    segments = plan_segments(n, segment_count(n, max_per_segment, n_workers),
                             ceil(audio_duration * FPS), s)
    n_workers = min(n_workers, len(segments))

    seg_paths = render_segments(segments, slides, tmpdir, s, n_workers, title_card)
    join_segments(seg_paths, audio, out_path, tmpdir)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args():
    p = argparse.ArgumentParser(description="Generate a slideshow video from images and an MP3.")
    p.add_argument("--images", required=True, help="Directory containing input images")
    p.add_argument("--audio", required=True, help="MP3 audio file path")
    p.add_argument("--output", required=True, help="Output video file path (e.g. out.mp4)")
    p.add_argument("--width", type=int, default=1920, help="Target video width (default: 1920)")
    p.add_argument("--height", type=int, default=1080, help="Target video height (default: 1080)")
    p.add_argument("--seconds-per-image", type=float, default=6.0, help="Seconds each image remains before transition")
    p.add_argument("--fade-duration", type=float, default=0.5, help="Crossfade transition duration in seconds")
    p.add_argument("--bar-height", type=int, default=120, help="Height of bottom bar")
    p.add_argument("--bar-style", default="blur", choices=["blur", "solid"], help="Bottom bar style: 'blur' (frosted glass with optional tint) or 'solid' (flat alpha color)")
    p.add_argument("--bar-color", default="#00464a", help="Bar color for --bar-style solid (hex, e.g. #000000)")
    p.add_argument("--bar-alpha", type=float, default=0.7, help="Bar opacity for --bar-style solid (0.0 - 1.0)")
    p.add_argument("--bar-blur", type=int, default=20, help="Blur radius for --bar-style blur (boxblur radius)")
    p.add_argument("--bar-tint", default="#00464a", help="Tint color overlaid on the blurred bar for --bar-style blur (hex). Empty string disables tint.")
    p.add_argument("--bar-tint-alpha", type=float, default=0.6, help="Opacity of the bar tint for --bar-style blur (0.0 - 1.0)")
    p.add_argument("--bar-slide-in-delay", type=float, default=None, help="Seconds before the bar slides up into view (default: 3, or --title-card-duration when a title card is used)")
    p.add_argument("--bar-visible-duration", type=float, default=30.0, help="Seconds the bar remains visible before sliding back down (default: 30)")
    p.add_argument("--bar-slide-duration", type=float, default=1.0, help="Duration of each slide animation in seconds, both in and out (default: 1)")
    p.add_argument("--zoom", action=argparse.BooleanOptionalAction, default=True, help="Slow Ken Burns zoom-in toward the center of each image during its display time (default: on; --no-zoom disables)")
    p.add_argument("--zoom-end-percent", type=float, default=105.0, help="On-screen scale of the image on its last frame before the transition, in percent (100 = no zoom, 110 = zoomed in 10%%). Only used with --zoom.")
    p.add_argument("--zoom-supersample", type=int, default=6, help="Supersample factor for the zoom stage: higher = smoother motion (less 1px stepping), but slower and more memory. Try 6-8 for short/aggressive zooms or low resolutions; 1-2 to render faster. Capped so the enlarged frame stays <=7680px wide. Only used with --zoom.")
    p.add_argument("--text", default="", help="Text to display centered in bottom bar")
    p.add_argument("--text-size", type=int, default=114, help="Font size for bottom bar text")
    p.add_argument("--font", default="/Library/Fonts/BebasNeue-Regular.ttf", help="Optional font file path for drawtext")
    p.add_argument("--fade-name", default="fade", help="FFmpeg xfade transition name (e.g. fade, wipeleft, circleopen)")
    p.add_argument("--logo", default="", help="Optional logo image path")
    p.add_argument("--title-card", default="", help="Optional title card image (PNG; transparency recommended) shown centred at the start, then shrinking down into the bottom bar")
    p.add_argument("--title-card-duration", type=float, default=6.0, help="Seconds the title card stays fully visible before moving into the bar (default: 4)")
    p.add_argument("--title-card-transition", type=float, default=None, help="Seconds the title card takes to glide into the bar (default: --bar-slide-duration, or 1 if that is 0)")
    p.add_argument("--contrast", type=float, default=1.05, help="Slide contrast (1.0 = unchanged, default: 1.05)")
    p.add_argument("--saturation", type=float, default=1.15, help="Slide saturation (1.0 = unchanged, default: 1.15)")
    p.add_argument("--vignette", type=float, default=0.3, help="Vignette strength 0-1 (0 = off, default: 0.3; 0.4 = FFmpeg's default vignette)")
    p.add_argument("--encoder", default="auto", choices=["auto", "x264", "vt_h264", "vt_hevc"], help="Video encoder: auto tries h264_videotoolbox if available on macOS")
    p.add_argument("--bitrate", default="6M", help="Target video bitrate (e.g. 6M). If set, used instead of CRF for chosen encoder where applicable.")
    p.add_argument("--crf", type=int, default=18, help="CRF value for x264 (quality). Ignored if --bitrate provided or hardware encoder without CRF.")
    p.add_argument("--threads", type=int, help="Threads for libx264 encoding (ignored for VideoToolbox).")
    p.add_argument("--blur", type=int, default=10, help="Background blur strength (boxblur radius). Applied once during preprocessing.")
    p.add_argument("--force-rgba", action="store_true", help="Force RGBA pixel format for the logo overlay (may slow down; use only if transparency processing explicitly needed).")
    p.add_argument("--max-inputs-per-pass", type=int, default=15, help="Max images per ffmpeg process. The video is always rendered as parallel segments (at least one per CPU core when there are enough images).")
    p.add_argument("--segment-workers", type=int, default=None, help="Parallel workers for segment rendering. Default: auto-sized to CPU cores and available RAM.")
    return p.parse_args()


def validate_inputs(args):
    check_dep("ffmpeg")
    check_dep("ffprobe")
    if not os.path.isdir(args.images):
        die(f"Images directory not found: {args.images}")
    if not os.path.isfile(args.audio):
        die(f"Audio file not found: {args.audio}")
    if args.logo and not os.path.isfile(args.logo):
        die(f"Logo file not found: {args.logo}")
    if args.title_card and not os.path.isfile(args.title_card):
        die(f"Title card file not found: {args.title_card}")


def make_settings(args) -> Settings:
    fade_dur = args.fade_duration
    if fade_dur >= args.seconds_per_image:
        fade_dur = args.seconds_per_image / 2.0
        print(f"Warning: fade-duration >= seconds-per-image; reduced to {fade_dur}")

    zoom_enabled = args.zoom
    if zoom_enabled and args.zoom_end_percent <= 100.0:
        print(f"Warning: --zoom-end-percent {args.zoom_end_percent} <= 100; zoom disabled (nothing to zoom into).")
        zoom_enabled = False

    title_trans = args.title_card_transition
    if title_trans is None:
        title_trans = args.bar_slide_duration if args.bar_slide_duration > 0 else 1.0
    bar_delay = args.bar_slide_in_delay
    if bar_delay is None:
        # With a title card the bar rises exactly while the card glides into it.
        bar_delay = args.title_card_duration if args.title_card else 3.0

    return Settings(
        width=args.width, height=args.height,
        seconds_per_image=args.seconds_per_image, fade_dur=fade_dur, fade_name=args.fade_name,
        bg_blur=args.blur,
        contrast=args.contrast, saturation=args.saturation, vignette=max(0.0, min(1.0, args.vignette)),
        zoom_enabled=zoom_enabled, zoom_end_percent=args.zoom_end_percent,
        zoom_supersample=args.zoom_supersample,
        bar_height=args.bar_height, bar_style=args.bar_style, bar_color=args.bar_color,
        bar_alpha=args.bar_alpha, bar_blur=args.bar_blur, bar_tint=args.bar_tint,
        bar_tint_alpha=args.bar_tint_alpha, bar_slide_in_delay=bar_delay,
        bar_visible_duration=args.bar_visible_duration, bar_slide_duration=args.bar_slide_duration,
        text=args.text, text_size=args.text_size, font=args.font, logo=args.logo,
        force_rgba=args.force_rgba,
        title_card=args.title_card, title_card_duration=max(0.0, args.title_card_duration),
        title_card_transition=max(0.0, title_trans),
        encoder=resolve_encoder(args.encoder), bitrate=args.bitrate, crf=args.crf, threads=args.threads,
    )


def main():
    args = parse_args()
    validate_inputs(args)

    images = gather_images(args.images)
    if not images:
        die(f"No images found (supported: {', '.join(sorted(SUPPORTED_EXTS))})")

    try:
        audio_duration = ffprobe_duration(args.audio)
    except Exception as e:
        die(f"Could not determine audio duration: {e}")

    s = make_settings(args)
    needed = slides_needed_for_audio(audio_duration, s.seconds_per_image, s.fade_dur)
    chosen = [images[i % len(images)] for i in range(needed)]

    tmpdir = tempfile.mkdtemp(prefix="wave_jpeg_")
    try:
        slides = prepare_slides(chosen, tmpdir, s)
        title_card = prepare_title_card(tmpdir, s)

        max_per_segment = max(2, args.max_inputs_per_pass or len(slides))
        render_video(slides, args.audio, audio_duration, args.output, tmpdir, s,
                     max_per_segment, args.segment_workers, title_card)

        print("Done:", args.output)
        print(f"Audio length: {audio_duration:.2f}s, images used: {len(slides)}, resolution: {s.width}x{s.height}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception as e:
            print("Warning: could not remove temp dir", tmpdir, e, file=sys.stderr)


if __name__ == "__main__":
    main()
