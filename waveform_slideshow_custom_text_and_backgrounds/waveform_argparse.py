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
  3. Audio (optional, on by default): high-pass, optional denoise and loudness
     normalisation via enhance_audio.py.
  4. Join: segments are concatenated without re-encoding and the audio added.
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import dataclasses
from dataclasses import dataclass
from functools import lru_cache
from math import ceil, exp, hypot, pi
from typing import List, Optional, Sequence

import enhance_audio

SUPPORTED_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.heic'}

# Frame rate of the whole pipeline.
FPS = 30

# Supersampled frames for the zoom stage are capped at this width ...
MAX_SUPERSAMPLE_WIDTH = 7680
# ... except that outputs up to MAX_FLOOR_SUPERSAMPLE_WIDTH / MIN_AUTO_SUPERSAMPLE
# px wide (1440p) always get at least MIN_AUTO_SUPERSAMPLE: below 4x the
# zoom's 1/factor px steps become visible as jitter.
MIN_AUTO_SUPERSAMPLE = 4
MAX_FLOOR_SUPERSAMPLE_WIDTH = 10240

# Output colour: BT.709 limited range (what YouTube and players assume for HD).
BT709_PARAMS = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
BT709_ENCODE_ARGS = ["-colorspace", "bt709", "-color_primaries", "bt709",
                     "-color_trc", "bt709", "-color_range", "tv"]

# Pixel sizes below are tuned for 1080p and scaled by height / REFERENCE_HEIGHT.
REFERENCE_HEIGHT = 1080

# --bitrate auto: YouTube's recommended SDR upload bitrate at 24-30 fps,
# (minimum output height, bitrate), highest first.
AUTO_BITRATES = ((2160, "40M"), (1440, "16M"), (0, "8M"))

LOGO_SIZE = 105              # logo size without a bar (with a bar: bar height - 20)
LOGO_MARGIN = 10
LOGO_EFFECTS = ("glow", "pulse", "ring", "none")
# Voice-reactive logo: envelope smoothing (s) and dynamic range (dB below the
# loud speech level -- the 90th percentile of per-frame RMS -- that maps to 0).
LOGO_ATTACK, LOGO_RELEASE, LOGO_RANGE_DB = 0.10, 0.45, 24.0
LOGO_PULSE = 0.04                    # pulse: max extra scale (+4%)
LOGO_RING = (1.06, 1.16)             # ring: radius range, relative to the logo radius
LOGO_GLOW_COLOR = (255, 88, 0)       # glow and ring colour (#ff5800)
TEXT_COLOR = "#ffb000"
TEXT_BORDER = 6                       # black outline around the bar text (px)
TEXT_MIN_GAP = 6                      # min gap between the text (with outline) and the bar's top/bottom (px)

# Title card: initial size ("contain" in this fraction of the frame) and the
# fraction of the bar height it shrinks to when it lands in the bar.
TITLE_CARD_FILL = 1.0
TITLE_CARD_LANDED_HEIGHT = 0.8

# Liquid glass bar (--bar-style glass): saturation boost of the refracted
# content, light direction of the specular rim (x, y; screen y points down)
# and the soft drop shadow (spread px, y offset px, opacity).
GLASS_SATURATION = 1.35
GLASS_BRIGHTNESS = 0.03
GLASS_LIGHT = (-0.45, -0.89)
GLASS_SHADOW = (22, 5, 0.30)

# --fade-name mix: calm transitions picked per slide (never the same twice in a row).
CALM_TRANSITIONS = ("fade", "fadeblack", "smoothleft", "smoothright", "smoothup",
                    "smoothdown", "circleopen", "hblur")

# --motion mix: camera moves picked per slide (never the same twice in a row).
#   in / out   zoom toward / away from the centre
#   corner     zoom in toward one of the four corners
#   left/right slow pan across the (slightly zoomed-in) image
MOTION_POOLS = {
    "mix": ("in", "out", "corner", "left", "right"),
    "in": ("in",),
    "out": ("out",),
    "pan": ("left", "right"),
}
# Focus point of a "corner" zoom (fraction of the width/height).
CORNER_FOCUS = (0.15, 0.85)

# End screen: seconds over which the slides dim, and how much colour they lose.
END_SCREEN_FADE = 1.5
END_SCREEN_DESATURATE = 0.3


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
    transitions: tuple          # xfade transition names to pick from
    bg_blur: int
    # look (applied once per slide during preprocessing)
    contrast: float
    saturation: float
    vignette: float
    # zoom
    zoom_enabled: bool
    zoom_end_percent: float
    zoom_supersample: int
    motion: str                 # key of MOTION_POOLS
    variation_seed: int         # seed for the per-slide motion/transition choice
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
    # (start, end | None, text) per bar appearance, in seconds; the bar slides
    # in from `start` and is fully gone again at `end` (None = stays)
    bar_windows: tuple
    # (font size, y of the drawtext line inside the bar) per window: the text
    # ink (with outline) is centred on the bar and shrunk to fit if needed
    bar_text_layout: tuple
    multi_text: bool            # windows come from --multi-text (no title-card text fade)
    glass_inset: int            # glass: gap to the left/right/bottom frame edges (px)
    glass_radius: int           # glass: corner radius (px), 0 = capsule
    glass_refraction: int       # glass: max lens displacement at the edge (px)
    glass_blur: float           # glass: gblur sigma of the content seen through it
    glass_tint_alpha: float     # glass: opacity of --bar-tint over the glass
    text: str
    text_size: int
    font: str
    logo: str                   # "" = no logo
    logo_effect: str            # glow | pulse | ring | none
    logo_size: int              # longest side on screen (px)
    force_rgba: bool
    # title card
    title_card: str
    title_card_duration: float
    title_card_transition: float
    # end screen (None = off): the last seconds of the video are dimmed, for YouTube end cards
    end_start: Optional[float]
    end_dim: float
    # encoding
    encoder: str
    bitrate: Optional[str]
    crf: int
    threads: Optional[int]
    # height / REFERENCE_HEIGHT: scales the fixed 1080p-tuned pixel constants
    px_scale: float

    @property
    def bar_top(self) -> int:
        """y of the bar's top edge when fully visible."""
        return self.height - self.bar_height

    @property
    def glass_rect(self):
        """(x, y, w, h) of the glass bar at rest (y = bar_top)."""
        i = self.glass_inset
        return i, self.bar_top, self.width - 2 * i, self.bar_height - i

    @property
    def bar_center_y(self) -> float:
        """Vertical centre of the visible bar at rest (logo / title card target)."""
        if self.bar_style == "glass":
            return self.bar_top + self.glass_rect[3] / 2
        return self.height - self.bar_height / 2

    @property
    def supersample(self) -> int:
        """Effective zoom supersample factor (1 when zoom is off): capped so the
        enlarged frame is <= MAX_SUPERSAMPLE_WIDTH wide, but never below
        MIN_AUTO_SUPERSAMPLE for outputs up to 1440p (an explicit lower
        --zoom-supersample is still honoured)."""
        if not self.zoom_enabled:
            return 1
        w = max(1, self.width)
        cap = MAX_SUPERSAMPLE_WIDTH // w
        if w * MIN_AUTO_SUPERSAMPLE <= MAX_FLOOR_SUPERSAMPLE_WIDTH:
            cap = max(MIN_AUTO_SUPERSAMPLE, cap)
        return max(1, min(self.zoom_supersample, cap))

    def px(self, v: float) -> int:
        """A 1080p-tuned pixel size scaled to this output (integer, >= 1)."""
        return max(1, round(v * self.px_scale))

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
    """Render one finished slide still in a single ffmpeg run: the image
    scaled to "contain" the target frame, centred over a stretched + blurred
    copy of itself, colour-graded, stored losslessly (raw yuv420p in MKV).

    Colour handling: the source is decoded with its own tags (JPEG = full
    range BT.601), composed in RGB (rgb24 also drops alpha, so transparent
    PNG areas show their underlying colour, as before), and converted to YUV
    exactly once, explicitly, to BT.709 limited range -- the standard YouTube
    and players assume for HD. eq (look) needs YUV, so it runs after that.
    """
    if heic_tmp:
        rc, err = decode_heic_to_png(src, heic_tmp)
        if rc != 0:
            return rc, err
        src = heic_tmp

    w, h = s.width, s.height
    fits_width = f"gte(iw/ih,{w}/{h})"
    scale_fg = f"scale='if({fits_width},{w},-1)':'if({fits_width},-1,{h})'"
    return run_quiet([
        "ffmpeg", "-y", "-i", src,
        "-filter_complex",
        f"[0:v]format=rgb24,setsar=1,split=2[a][b];[a]{scale_fg},setsar=1[fg];"
        f"[b]scale={w}:{h},setsar=1,boxblur={s.bg_blur}:1[bg];"
        f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:format=gbrp,"
        f"scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuv444p{look_filters(s)},"
        f"format=yuv420p,{BT709_PARAMS}[out]",
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

    # One short single-frame ffmpeg run per worker: ~200 MB overhead + frame buffers.
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


def build_drawtext_options(text: str, size: int, fontfile: str, y_pos, alpha: str = "",
                           border: int = TEXT_BORDER) -> str:
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
        f"borderw={border}",
        "bordercolor=black",
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


def window_slide(s: Settings, a: float, b: Optional[float]) -> float:
    """Slide-in/out duration of one bar window (shortened for short windows)."""
    S = max(0.0, s.bar_slide_duration)
    return S if b is None else min(S, (b - a) / 2)


def bar_hidden_expr(s: Settings) -> str:
    """FFmpeg expression of t: 0 = bar fully up, 1 = fully below the frame.
    Each window slides in linearly from its start and is gone at its end.

    \\, escapes commas so the filter graph parser does not treat them as filter separators.
    """
    terms = []
    for a, b, _ in s.bar_windows:
        S = window_slide(s, a, b)
        if b is None:
            if S <= 0 and a <= 0:
                return "0"
            terms.append(f"clip((t-{a:.4f})/{S:.4f}\\,0\\,1)" if S > 0 else f"gte(t\\,{a:.4f})")
        elif S > 0:
            terms.append(f"clip(min(t-{a:.4f}\\,{b:.4f}-t)/{S:.4f}\\,0\\,1)")
        else:
            terms.append(f"gte(t\\,{a:.4f})*lt(t\\,{b:.4f})")
    if not terms:
        return "1"
    return "(1-(" + "+".join(terms) + "))"


def bar_y_expr(s: Settings) -> str:
    """FFmpeg y-position expression of the bar's top edge (bar_top when up,
    H when hidden), following the bar windows."""
    h = bar_hidden_expr(s)
    return str(s.bar_top) if h == "0" else f"{s.bar_top}+{s.bar_height}*{h}"


def window_text_alpha(s: Settings, k: int) -> str:
    """Text opacity of window k: only the first default-mode window waits for
    the title card to land."""
    return bar_text_alpha(s) if k == 0 and not s.multi_text else ""


def measure_text(text: str, size: int, font: str, border: int = TEXT_BORDER):
    """(top, height) of the drawn text ink including the outline, with top
    relative to drawtext's y. Rendered once by FFmpeg on a transparent canvas."""
    y0, h = size, size * 4
    w = max(1000, min(16000, len(text) * size * 2))
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", f"color=c=black@0:s={w}x{h}:d=1,format=rgba",
         "-vf", "drawtext=" + build_drawtext_options(text, size, font, y0, border=border)
         + ",alphaextract,lut=y='if(gt(val\\,8)\\,255\\,0)',bbox",
         "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True)
    boxes = [line.split("crop=")[1].split()[0] for line in res.stderr.splitlines() if "crop=" in line]
    if not boxes:
        return None
    _, bh, _, by = (int(v) for v in boxes[-1].split(":"))
    return by - y0, bh


def fit_bar_text(text: str, size: int, font: str, bar_h: int,
                 border: int = TEXT_BORDER, min_gap: int = TEXT_MIN_GAP):
    """(font size, drawtext y inside the bar): the ink (with outline) is
    centred on the bar; if it would come closer than `min_gap` to the
    bar's top or bottom edge, the font is shrunk until the gaps are `min_gap`.
    `border` = outline width (px)."""
    if not text or bar_h <= 0:
        return size, 0
    avail = bar_h - 2 * min_gap
    m = measure_text(text, size, font, border)
    if m is None:   # nothing visible (e.g. only spaces)
        return size, 0
    start = size
    for _ in range(40):
        if m[1] <= avail or size <= 4:
            break
        # outline thickness does not scale with the font, only the glyphs do
        glyphs, target = max(1, m[1] - 2 * border), max(1, avail - 2 * border)
        size = max(4, min(size - 1, int(size * target / glyphs)))
        m = measure_text(text, size, font, border) or m
    if size != start:
        print(f"Text {text!r} with outline is taller than the bar; font size {start} -> {size}")
    top, ink_h = m
    return size, round((bar_h - ink_h) / 2 - top)


def bar_drawtexts(s: Settings, y_pos: str, windows=None) -> str:
    """',drawtext=...' for each window's text (enabled only inside its window
    when there are several); `y_pos` = y of the bar's top edge in the frame
    the text is drawn on."""
    ks = range(len(s.bar_windows)) if windows is None else windows
    out = ""
    for k in ks:
        a, b, text = s.bar_windows[k]
        if not text:
            continue
        size, ty = s.bar_text_layout[k]
        opts = build_drawtext_options(text, size, s.font, f"{y_pos}+({ty})", window_text_alpha(s, k),
                                      border=s.px(TEXT_BORDER))
        if windows is None and len(s.bar_windows) > 1:
            opts += (f":enable=between(t\\,{a:.4f}\\,{b:.4f})" if b is not None
                     else f":enable=gte(t\\,{a:.4f})")
        out += ",drawtext=" + opts
    return out


@lru_cache(maxsize=None)
def _variation_sequence(pool: tuple, seed: int, length: int = 4096) -> tuple:
    rng = random.Random(seed)
    seq = []
    for _ in range(length):
        options = [x for x in pool if not seq or x != seq[-1]] or list(pool)
        seq.append(rng.choice(options))
    return tuple(seq)


def variation(pool: tuple, seed: int, j: int):
    """Deterministic pick for global slide j: the same slide gets the same
    choice in every segment process (and in every re-render with the same seed)."""
    if len(pool) == 1:
        return pool[0]
    seq = _variation_sequence(tuple(pool), seed)
    return seq[j % len(seq)]


def slide_motion(j: int, s: Settings) -> str:
    return variation(MOTION_POOLS[s.motion], s.variation_seed, j)


def transition_for(j: int, s: Settings) -> str:
    """xfade transition from slide j-1 into slide j."""
    return variation(s.transitions, s.variation_seed + 7919, j)


def zoom_filters(s: Settings, j: int = 0) -> str:
    """Filter chain that turns ONE still frame into a slide of frames_per_image
    frames with a slow camera move (slide_motion: zoom in/out, zoom toward a
    corner, or a pan). zoom_end_percent is the on-screen scale at the most
    zoomed-in point (100 = none, 110 = +10%).

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
        The factor is capped so the enlarged frame stays <= 7680 px wide,
        except that outputs up to 1440p always get >= 4x (Settings.supersample).

    Performance: the input is a single frame, so the supersampling `scale` runs
    once per slide; zoompan (d=frames) then emits every frame from it.
    """
    target = s.zoom_end_percent / 100.0
    frames = s.frames_per_image
    span = max(1, int(round(s.seconds_per_image * FPS)))
    inc = (target - 1.0) / span
    progress = f"min(on/{span},1)"
    kind = slide_motion(j, s)
    fx = fy = "0.5"
    if kind == "out":
        z = f"max({target:.6f}-{inc:.8f}*on,1)"
    elif kind in ("left", "right"):
        z = f"{target:.6f}"
        fx = f"(1-{progress})" if kind == "left" else progress
    else:  # in / corner
        z = f"min(1+{inc:.8f}*on,{target:.6f})"
        if kind == "corner":
            rng = random.Random(f"{s.variation_seed}-corner-{j}")
            fx, fy = (f"{rng.choice(CORNER_FOCUS)}" for _ in range(2))
    ss = s.supersample
    pre = f"scale=iw*{ss}:ih*{ss}:flags=bicubic," if ss > 1 else ""
    return (
        f"{pre}zoompan=z='{z}'"
        f":x='(iw-iw/({z}))*{fx}':y='(ih-ih/({z}))*{fy}'"
        f":d={frames}:s={s.width}x{s.height}:fps={FPS}"
    )


def hold_filters(s: Settings) -> str:
    """Filter chain that repeats ONE still frame for frames_per_image frames
    (no re-decoding; frames are reference-counted copies)."""
    return f"loop=loop={s.frames_per_image - 1}:size=1,settb=1/{FPS},setpts=N"


def xfade_chain(in_labels: List[str], offsets_frames: List[int], names: List[str],
                s: Settings, out_label: str) -> List[str]:
    """Chain clips (filter labels) with xfade; clip k starts fading in at
    offsets_frames[k-1] (frames from the start of the chain) with transition names[k-1].

    Performance: do NOT put `format=` between the xfades. It makes FFmpeg
    negotiate yuv444p for xfade and auto-insert yuv420p<->yuv444p scalers at
    every link, so each frame is converted once per *later* xfade in the chain
    (quadratic cost; measured ~7x slower for 8 slides). One format at the end.
    """
    parts = []
    prev = in_labels[0]
    for k, (label, off, name) in enumerate(zip(in_labels[1:], offsets_frames, names), start=1):
        out = f"xf{k}"
        parts.append(
            f"[{prev}][{label}]xfade=transition={name}:duration={s.fade_dur:.6f}"
            f":offset={off / FPS:.6f}[{out}]"
        )
        prev = out
    parts.append(f"[{prev}]format=yuv420p[{out_label}]")
    return parts


def build_slideshow_filter(first_slide: int, n_slides: int, s: Settings, out_label: str) -> str:
    """Filtergraph for slides first_slide..first_slide+n-1, fed as single-frame
    inputs 0..n-1, -> [out_label]. Local time 0 = first slide's start.

    Performance: xfade only supports 4:4:4 formats, so every frame that goes
    through it is converted yuv420p -> yuv444p -> yuv420p. Chaining whole
    clips through xfade (xfade_chain) therefore converts *every* frame of the
    video (~25% of the render CPU) although only the ~13 frames per transition
    are blended. Instead each clip is split into body and transition pieces:
    only the overlapping tail/head go through xfade, the bodies are
    concatenated untouched (no chroma round trip, so they are also sharper).
    Transition frames are bit-identical to the full-clip xfade chain."""
    L = s.frames_per_image
    origin = slide_start_frame(first_slide, s)
    starts = [slide_start_frame(first_slide + i, s) - origin for i in range(n_slides)]
    # overlap[k] = frames during which slide k-1 and slide k are blended
    overlap = [0] + [starts[k - 1] + L - starts[k] for k in range(1, n_slides)]

    clip = [
        f"[{i}:v]format=yuv420p,setsar=1,"
        f"{zoom_filters(s, first_slide + i) if s.zoom_enabled else hold_filters(s)},format=yuv420p,"
        f"trim=duration={s.seconds_per_image:.6f}"
        for i in range(n_slides)
    ]
    names = [transition_for(first_slide + k, s) for k in range(1, n_slides)]
    if n_slides > 1 and not all(1 <= ov < L // 2 for ov in overlap[1:]):
        # Degenerate fades (≈0 or longer than half a slide): use the plain chain.
        parts = [f"{c}[v{i}]" for i, c in enumerate(clip)]
        parts += xfade_chain([f"v{i}" for i in range(n_slides)], starts[1:], names, s, out_label)
        return ";".join(parts)

    def piece(src: str, a: int, b: int, out: str) -> str:
        return f"[{src}]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS[{out}]"

    parts, order = [], []
    for k in range(n_slides):
        # head: blended with the previous slide (+1 extra frame so xfade sees
        # B continue past the transition, exactly as with the full clip)
        head = overlap[k] + 1 if k > 0 else 0
        tail = overlap[k + 1] if k + 1 < n_slides else 0
        outs = (["h"] if head else []) + ["b"] + (["t"] if tail else [])
        if len(outs) == 1:
            parts.append(f"{clip[k]}[b{k}]")
        else:
            parts.append(f"{clip[k]},split={len(outs)}" + "".join(f"[{o}{k}s]" for o in outs))
            if head:
                parts.append(piece(f"h{k}s", 0, head, f"h{k}"))
            parts.append(piece(f"b{k}s", head, L - tail, f"b{k}"))
            if tail:
                parts.append(piece(f"t{k}s", L - tail, L, f"t{k}"))
        if k > 0:
            parts.append(
                f"[t{k - 1}][h{k}]xfade=transition={names[k - 1]}:duration={s.fade_dur:.6f}:offset=0,"
                f"format=yuv420p[x{k}]")
            order.append(f"[x{k}]")
        order.append(f"[b{k}]")
    if len(order) == 1:
        parts.append(f"{order[0]}copy[{out_label}]")
    else:
        # concat switches to a microsecond timebase; put the frames back on the
        # exact 1/FPS grid so `t` in the bar/title animations matches frame times.
        parts.append("".join(order) + f"concat=n={len(order)}:v=1:a=0,settb=1/{FPS},setpts=N[{out_label}]")
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
    cy_end = s.bar_center_y if s.bar_height > 0 else s.height - bar_h / 2
    cy = f"({s.height / 2:.2f}+({cy_end:.2f}-{s.height / 2:.2f})*{e})"
    fade_start, fade_dur = title_handoff(s)
    return (
        f"[{card_index}:v]format=rgba,"
        f"scale=w={cw}*{z}:h={ch}*{z}:eval=frame:flags=bicubic,"
        f"fade=t=out:st={fade_start:.4f}:d={fade_dur:.4f}:alpha=1[tcard];"
        f"[{src_label}][tcard]overlay=x=(W-w)/2:y={cy}-h/2:eval=frame:eof_action=pass:format=auto,"
        f"format=yuv420p[{out_label}]"
    )


# --------------------------------------------------------------------------- #
# Liquid glass bar
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Glass:
    """Precomputed still layers of the glass bar (generated once, read as
    single-frame inputs by every segment)."""
    inputs: tuple   # xmap, ymap, rim (rgba), mask (gray), shadow (rgba)
    region: tuple   # (x, y, w, h) of the captured background around the glass


def glass_shadow(s: Settings):
    """(spread px, y offset px, opacity) of the glass drop shadow, scaled to the output."""
    spread, off, opacity = GLASS_SHADOW
    return s.px(spread), s.px(off), opacity


def _rrect_sdf(px: float, py: float, hw: float, hh: float, r: float):
    """Signed distance of (px, py) (relative to the centre) to a rounded
    rectangle with half sizes hw x hh and corner radius r (negative inside),
    plus the outward unit normal."""
    sx, sy = (1 if px >= 0 else -1), (1 if py >= 0 else -1)
    qx, qy = abs(px) - hw + r, abs(py) - hh + r
    if qx > 0 and qy > 0:
        L = hypot(qx, qy)
        return L - r, (sx * qx / L, sy * qy / L)
    if qx > qy:
        return qx - r, (sx, 0.0)
    return qy - r, (0.0, sy)


def _write_pgm(path: str, w: int, h: int, data: bytearray):
    with open(path, "wb") as f:
        f.write(f"P5\n{w} {h}\n255\n".encode() + bytes(data))


def _write_pam_rgba(path: str, w: int, h: int, data: bytearray):
    with open(path, "wb") as f:
        f.write(f"P7\nWIDTH {w}\nHEIGHT {h}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n".encode()
                + bytes(data))


def prepare_glass(tmpdir: str, s: Settings) -> Optional[Glass]:
    """Builds the glass layers (pure Python, ~1 s at 1080p):
      - xmap/ymap for ffmpeg's displace: towards the rim the glass samples the
        background further *outside* the shape (a convex lens edge), so the
        image bends around the border like Apple's Liquid Glass
      - rim: thin specular edge (bright where it faces the light, a fainter
        counter-highlight opposite) + a soft inner glow
      - mask: antialiased rounded-rectangle alpha
      - shadow: soft drop shadow that makes the bar float"""
    if s.bar_height <= 0 or s.bar_style != "glass":
        return None
    W, H = s.width, s.height
    gx, gy, gw, gh = s.glass_rect
    if gw < 8 or gh < 8:
        die("--bar-style glass: the bar is too small for --glass-inset")
    r = min(s.glass_radius or gh / 2, gh / 2, gw / 2)
    hw, hh = gw / 2, gh / 2
    P = max(0, s.glass_refraction)

    # captured region: the glass plus up to P px around it (clipped to the frame)
    # Edges are aligned to even pixels: the yuv420 crops in glass_bar_filter
    # round odd offsets down, and an odd region (e.g. an odd refraction) made
    # its strip sizes disagree so fillborders failed.
    rx0, ry0 = max(0, gx - P) // 2 * 2, max(0, gy - P) // 2 * 2
    rx1, ry1 = min(W, (gx + gw + P + 1) // 2 * 2), min(H, (gy + gh + P + 1) // 2 * 2)
    RW, RH = rx1 - rx0, ry1 - ry0
    k = s.px_scale                          # rim geometry is tuned for 1080p
    bevel = max(6.0 * k, min(r, hh) * 0.9)
    xmap, ymap = bytearray([128]) * (RW * RH), bytearray([128]) * (RW * RH)
    for y in range(gy, gy + gh):
        py = y + 0.5 - (gy + hh)
        row = (y - ry0) * RW - rx0
        for x in range(gx, gx + gw):
            d, (nx, ny) = _rrect_sdf(x + 0.5 - (gx + hw), py, hw, hh, r)
            if d >= 0 or -d >= bevel:
                continue
            m = P * (1 + d / bevel) ** 2           # P at the rim -> 0 at the bevel's inner edge
            xmap[row + x] = max(0, min(255, round(128 + nx * m)))
            ymap[row + x] = max(0, min(255, round(128 + ny * m)))

    lx, ly = GLASS_LIGHT
    rim, mask = bytearray(gw * gh * 4), bytearray(gw * gh)
    for y in range(gh):
        py = y + 0.5 - hh
        for x in range(gw):
            d, (nx, ny) = _rrect_sdf(x + 0.5 - hw, py, hw, hh, r)
            i = y * gw + x
            mask[i] = max(0, min(255, round((0.5 - d) * 255)))
            if d > 0.5:
                continue
            u = -d                                  # depth inside the glass
            facing = nx * lx + ny * ly
            spec = 0.25 + 0.75 * max(0.0, facing) ** 2 + 0.45 * max(0.0, -facing) ** 2
            line = max(0.0, 1 - abs(u - 1.2 * k) / (1.4 * k)) * min(1.0, spec) * 0.95
            glow = exp(-u / (7 * k)) * 0.22 * spec
            sheen = 0.07 * max(0.0, 1 - (y + 0.5) / (gh * 0.55))
            a = 1 - (1 - line) * (1 - glow) * (1 - sheen)
            rim[4 * i:4 * i + 4] = b"\xff\xff\xff" + bytes([round(255 * min(1.0, a))])

    S, off, opacity = glass_shadow(s)
    sw, sh = gw + 2 * S, gh + 2 * S
    shadow = bytearray(sw * sh * 4)
    for y in range(sh):
        py = y + 0.5 - S - off - hh
        for x in range(sw):
            d, _ = _rrect_sdf(x + 0.5 - S - hw, py, hw, hh, r)
            t = min(1.0, max(0.0, d / S + 0.25))
            a = opacity * (1 - t * t * (3 - 2 * t))
            shadow[4 * (y * sw + x) + 3] = round(255 * a)

    paths = [os.path.join(tmpdir, n) for n in
             ("glass_xmap.pgm", "glass_ymap.pgm", "glass_rim.pam", "glass_mask.pgm", "glass_shadow.pam")]
    _write_pgm(paths[0], RW, RH, xmap)
    _write_pgm(paths[1], RW, RH, ymap)
    _write_pam_rgba(paths[2], gw, gh, rim)
    _write_pgm(paths[3], gw, gh, mask)
    _write_pam_rgba(paths[4], sw, sh, shadow)
    return Glass(inputs=tuple(paths), region=(rx0, ry0, RW, RH))


def bar_frame_windows(s: Settings):
    """Global frames [a, b) of each bar window (b None = until the end).
    Outside them bar_y_expr puts the bar at y = H, fully hidden."""
    if s.bar_height <= 0:
        return []
    return [(ceil(a * FPS - 1e-9), None if b is None else ceil(b * FPS - 1e-9))
            for a, b, _ in s.bar_windows]


def glass_parts(f0: int, n: Optional[int], s: Settings):
    """Splits a segment's global frames [f0, f0 + n) (n None = open end) into
    (start, end, window index | None) runs, so the glass is only rendered
    while the bar is visible (and each run draws its window's text)."""
    wins = bar_frame_windows(s)
    f1 = f0 + n if n is not None else None
    cuts = sorted({c for w in wins for c in w
                   if c is not None and c > f0 and (f1 is None or c < f1)})
    bounds = [f0] + cuts + [f1]

    def which(lo: int):
        for k, (a, b) in enumerate(wins):
            if lo >= a and (b is None or lo < b):
                return k
        return None
    return [(lo, hi, which(lo)) for lo, hi in zip(bounds, bounds[1:])]


def glass_bar_filter(src: str, s: Settings, glass: Glass, i0: int, out: str, tag: str = "",
                     win: Optional[int] = None) -> List[str]:
    """[src] -> [out] with the liquid glass bar. The background seen through
    the glass is captured at the bar's *current* position every frame (also
    while it slides), with the frame's bottom row smeared below the frame for
    the part that is already off screen. Labels are suffixed with `tag`;
    `win` = the bar window whose text is drawn (None = all of them)."""
    gx, gy, gw, gh = s.glass_rect
    rx, ry, RW, RH = glass.region
    S, off, _ = glass_shadow(s)
    bar_y = bar_y_expr(s)
    L = lambda name: f"[{name}{tag}]"
    # bottom strip from the capture region's resting top down, padded by one
    # region height so the moving crop window never leaves it; both the crop
    # and the overlays below round y down to even (yuv420), so they stay aligned
    chain = (f"{L('glsrc')}crop=iw:{s.height - ry}:0:{ry},pad=iw:ih+{RH}:0:0,"
             f"fillborders=bottom={RH}:mode=smear,crop={RW}:{RH}:{rx}:{bar_y}-{gy},format=gbrp")
    if s.glass_blur > 0:
        chain += f",gblur=sigma={s.glass_blur:.2f}"
    chain += (f"{L('glbg')};[{i0}:v]format=gbrp,loop=-1:1{L('gx')};[{i0 + 1}:v]format=gbrp,loop=-1:1{L('gy')};"
              f"{L('glbg')}{L('gx')}{L('gy')}displace=edge=smear,crop={gw}:{gh}:{gx - rx}:{gy - ry},"
              f"eq=saturation={GLASS_SATURATION}:brightness={GLASS_BRIGHTNESS},format=rgba")
    if s.bar_tint and s.glass_tint_alpha > 0:
        chain += f",drawbox=x=0:y=0:w=iw:h=ih:color={s.bar_tint}@{s.glass_tint_alpha:.3f}:t=fill"
    chain += f"{L('gltint')};[{i0 + 2}:v]format=rgba,loop=-1:1{L('glrim')};{L('gltint')}{L('glrim')}overlay=format=auto"
    chain += bar_drawtexts(s, "0", None if win is None else [win])
    chain += (f",format=rgba{L('gltxt')};[{i0 + 3}:v]format=gray,loop=-1:1{L('glmask')};"
              f"{L('gltxt')}{L('glmask')}alphamerge{L('glass')};"
              f"[{i0 + 4}:v]format=rgba,loop=-1:1{L('glshadow')}")
    return [
        f"{src}split=2{L('base')}{L('glsrc')}",
        chain,
        f"{L('base')}{L('glshadow')}overlay=x={gx - S}:y={bar_y}-{S - off}:format=auto:shortest=1{L('glbase')}",
        f"{L('glbase')}{L('glass')}overlay=x={gx}:y={bar_y}:format=auto:shortest=1,format=yuv420p[{out}]",
    ]


def glass_segment_filter(src: str, s: Settings, glass: Glass, i0: int, f0: int, n: Optional[int]) -> List[str]:
    """Glass bar for one segment ([src] carries global frame pts from f0):
    runs with the bar hidden bypass the whole glass chain; the runs are cut
    with trim and joined again with concat (back on global frame pts)."""
    parts = glass_parts(f0, n, s)
    if len(parts) == 1:
        if parts[0][2] is not None:
            return glass_bar_filter(src, s, glass, i0, "slide_bar", win=parts[0][2])
        return [f"{src}copy[slide_bar]"]
    out = [f"{src}split={len(parts)}" + "".join(f"[gp{k}]" for k in range(len(parts)))]
    for k, (lo, hi, win) in enumerate(parts):
        trim = f"trim=start_pts={lo}" + (f":end_pts={hi}" if hi is not None else "")
        if win is not None:
            out.append(f"[gp{k}]{trim}[gpt{k}]")
            out += glass_bar_filter(f"[gpt{k}]", s, glass, i0, f"gpo{k}", tag=f"_{k}", win=win)
        else:
            out.append(f"[gp{k}]{trim},format=yuv420p[gpo{k}]")
    out.append("".join(f"[gpo{k}]" for k in range(len(parts)))
               + f"concat=n={len(parts)}:v=1:a=0,settb=1/{FPS},setpts=N+{f0}[slide_bar]")
    return out


def segment_needs_glass(s: Settings, f0: int, n: Optional[int]) -> bool:
    return s.bar_style == "glass" and s.bar_height > 0 and any(w is not None for _, _, w in glass_parts(f0, n, s))


def build_overlay_filter(slide_label: str, s: Settings, glass: Optional[Glass] = None,
                         glass_input: int = 0, seg_frames=(0, None)) -> str:
    """Adds the animated bottom bar (+text) to [slide_label] -> [slide_bar]."""
    parts = []
    src = f"[{slide_label}]"
    bar_y = bar_y_expr(s)
    bar_h = s.bar_height

    if bar_h <= 0:
        parts.append(f"{src}copy[slide_bar]")
    elif s.bar_style == "glass":
        parts += glass_segment_filter(src, s, glass, glass_input, *seg_frames)
    elif s.bar_style == "solid":
        # A translucent colour strip slid in with overlay: drawbox cannot move
        # (its x/y are evaluated once, and `t` there means box thickness).
        # The strip is put on the segment's global frame pts so `t` matches.
        color = f"{s.bar_color or '#000000'}@{s.bar_alpha:.3f}"
        strip = (f"color=c={color}:s={s.width}x{bar_h}:r={FPS},format=rgba,"
                 f"settb=1/{FPS},setpts=N+{seg_frames[0]}")
        strip += bar_drawtexts(s, "0")
        parts += [
            f"{strip}[solidbar]",
            f"{src}[solidbar]overlay=x=0:y={bar_y}:format=auto:shortest=1,format=yuv420p[slide_bar]",
        ]
    else:  # blur: frosted strip cut from the bar's resting position, slid in with overlay
        blur_chain = f"crop=iw:{bar_h}:0:{s.bar_top},boxblur={s.bar_blur}:5"
        if s.bar_tint:
            blur_chain += f",drawbox=x=0:y=0:w=iw:h=ih:color={s.bar_tint}@{s.bar_tint_alpha:.3f}:t=fill"
        blur_chain += bar_drawtexts(s, "0")
        parts += [
            f"{src}split=2[base][blursrc]",
            f"[blursrc]{blur_chain}[blurredbar]",
            f"[base][blurredbar]overlay=x=0:y={bar_y}[slide_bar]",
        ]


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


# --------------------------------------------------------------------------- #
# Logo (optionally reacting to the voice)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Logo:
    """Prepared logo images (trimmed to the artwork, sized for the screen)."""
    inputs: tuple            # image files fed to every segment, in input order
    w: int                   # logo size on screen
    h: int
    cx: float                # logo centre on screen
    cy: float
    env: tuple               # per-frame voice envelope 0..1 (empty for effect "none")
    cmd_dir: str             # where the per-segment sendcmd files go


def _alpha_bbox(path: str):
    """(w, h, x, y) of the visible artwork (alpha > 40) -- logo files often
    carry a wide transparent margin."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vf",
                          "format=rgba,alphaextract,lut=y='if(gt(val\\,40)\\,255\\,0)',bbox",
                          "-f", "null", "-"], capture_output=True, text=True)
    boxes = [line.split("crop=")[1].split()[0] for line in res.stderr.splitlines() if "crop=" in line]
    if not boxes:
        return None
    w, h, x, y = (int(v) for v in boxes[-1].split(":"))
    return w, h, x, y


def voice_envelope(audio: str, n_frames: int) -> tuple:
    """Per-video-frame loudness of `audio`, smoothed (fast attack, slow release)
    and mapped to 0..1. Levels are relative to the track's own loud-speech level,
    so the result does not depend on the audio gain (the clean-up may run later)."""
    path = os.path.join(tempfile.gettempdir(), f"logo_env_{os.getpid()}.txt")
    rate = 48000
    rc, err = run_quiet([
        "ffmpeg", "-y", "-i", audio, "-vn", "-af",
        f"aresample={rate},asetnsamples=n={rate // FPS}:p=0,"
        "astats=metadata=1:reset=1:measure_overall=RMS_level:measure_perchannel=none,"
        f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={path}",
        "-f", "null", "-"])
    if rc != 0:
        die(f"Could not analyse the audio for the logo effect: {err[-500:]}")
    levels = []
    with open(path) as f:
        for line in f:
            if "RMS_level=" in line:
                v = line.split("=", 1)[1].strip()
                levels.append(-120.0 if "inf" in v else float(v))
    os.remove(path)
    voiced = sorted(v for v in levels if v > -70) or [-20.0]
    hi = voiced[int(len(voiced) * 0.9)]
    lo = hi - LOGO_RANGE_DB
    att, rel = (pow(2.718281828, -1 / (tau * FPS)) for tau in (LOGO_ATTACK, LOGO_RELEASE))
    env, e = [], 0.0
    for db in levels[:n_frames] + [-120.0] * max(0, n_frames - len(levels)):
        x = min(1.0, max(0.0, (db - lo) / (hi - lo)))
        c = att if x > e else rel
        e = c * e + (1 - c) * x
        env.append(round(e, 3))
    return tuple(env)


def prepare_logo(tmpdir: str, s: Settings, audio: str, n_frames: int) -> Optional[Logo]:
    if not s.logo:
        return None
    src = s.logo
    if src.lower().endswith(".heic"):
        check_dep("sips")
        src = os.path.join(tmpdir, "logo_heic.png")
        rc, err = decode_heic_to_png(s.logo, src)
        if rc != 0:
            die(f"Logo conversion failed: {err}")
    box = _alpha_bbox(src)
    crop = f"crop={box[0]}:{box[1]}:{box[2]}:{box[3]}," if box else ""
    bw, bh = box[:2] if box else probe_image_size(src)
    k = s.logo_size / max(bw, bh)
    w, h = max(2, round(bw * k)), max(2, round(bh * k))
    # Centre: half the bar height (or bottom-left corner without a bar), with
    # room around it for the ring (1.2x the logo).
    room = max(w, h) * 0.6
    margin = s.px(LOGO_MARGIN)
    cx = margin + room
    if s.bar_height > 0 and s.bar_style == "glass":
        # centre of the capsule's rounded left end
        gx, _, gw, gh = s.glass_rect
        cx = max(cx, gx + min(gh / 2, gw / 2))
    cy = s.bar_center_y if s.bar_height > 0 else s.height - margin - room

    def make(name: str, vf: str, inp: Optional[List[str]] = None) -> str:
        out = os.path.join(tmpdir, name)
        rc, err = run_quiet(["ffmpeg", "-y"] + (inp or ["-i", src]) + ["-vf", vf, "-frames:v", "1", out])
        if rc != 0:
            die(f"Logo preparation failed: {err[-500:]}")
        return out

    logo = make("logo.png", f"{crop}format=rgba,scale={w}:{h}:flags=lanczos")
    if s.logo_effect == "pulse":
        inputs = (make("logo4x.png", f"{crop}format=rgba,scale={w * 4}:{h * 4}:flags=lanczos"),)
    elif s.logo_effect == "glow":
        m = round(max(w, h) * 0.2)
        glow = make("logo_glow.png",
                    f"{crop}format=rgba,scale={w}:{h}:flags=lanczos,pad=iw+{2 * m}:ih+{2 * m}:{m}:{m}:color=black@0,"
                    f"lutrgb=r={LOGO_GLOW_COLOR[0]}:g={LOGO_GLOW_COLOR[1]}:b={LOGO_GLOW_COLOR[2]},gblur=sigma={max(w, h) * 0.06:.2f}:planes=15,"
                    "lutrgb=a='min(255\\,val*2.4)'")
        inputs = (glow, logo)
    elif s.logo_effect == "ring":
        ring = make("logo_ring.png",
                    f"geq=r={LOGO_GLOW_COLOR[0]}:g={LOGO_GLOW_COLOR[1]}:b={LOGO_GLOW_COLOR[2]}:a='255*clip(1-abs(hypot(X-200\\,Y-200)-150)/6\\,0\\,1)'",
                    ["-f", "lavfi", "-i", "color=black@0:s=400x400,format=rgba"])
        inputs = (ring, logo)
    else:
        inputs = (logo,)
    env = voice_envelope(audio, n_frames) if s.logo_effect != "none" else ()
    return Logo(inputs=inputs, w=w, h=h, cx=cx, cy=cy, env=env, cmd_dir=tmpdir)


def _filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def logo_filter(seg: Segment, logo: Logo, first_input: int, s: Settings) -> str:
    """[slide_bar] + logo -> [outv]. Effects are driven per frame by a sendcmd
    file holding this segment's slice of the voice envelope (global times, the
    logo stream is shifted to global time like the slides)."""
    n = (seg.trim_end - seg.trim_start) if seg.trim_end is not None else 1
    i0 = first_input
    x, y = round(logo.cx - logo.w / 2), round(logo.cy - logo.h / 2)
    # Lowest top edge of all logo layers (glow/ring stick out further than the
    # logo); sliding everything down by H - top puts every layer off screen.
    if s.logo_effect == "glow":
        top = y - round(max(logo.w, logo.h) * 0.2)
    elif s.logo_effect == "ring":
        top = min(y, round(logo.cy - ring_canvas(logo) / 2))
    elif s.logo_effect == "pulse":
        top = round(logo.cy - logo.h * (1 + LOGO_PULSE) / 2)
    else:
        top = y

    hidden = bar_hidden_expr(s)
    if s.bar_height > 0 and hidden != "0":
        # Slides in and out together with the bar (same timing, linear motion).
        # Layers sticking out above the bar travel a bit further so they are
        # off screen whenever the bar is.
        dist = max(s.bar_height, s.height - top)
        motion = f"{dist}*{hidden}"
    elif s.end_start is not None:   # no moving bar: slides off with the end screen
        p = f"clip((t-{s.end_start:.4f})/{END_SCREEN_FADE}\\,0\\,1)"
        motion = f"{s.height - top}*{p}*{p}*(3-2*{p})"
    else:
        motion = ""

    def ly(base: int) -> str:   # overlay y
        return f"{base}+{motion}" if motion else str(base)

    if s.logo_effect == "none" or not logo.env:
        return (f"[{i0}:v]format=rgba,setsar=1[logo];"
                f"[slide_bar]setsar=1[sb];[sb][logo]overlay=x={x}:y={ly(y)}:format=auto[outv]")

    def stream(idx: int) -> str:   # one image -> n frames on the global timeline
        return (f"[{idx}:v]format=rgba,setsar=1,loop=loop={n - 1}:size=1,"
                f"settb=1/{FPS},setpts=N+{seg.start_frame}")

    cmds = []
    for i in range(seg.start_frame, seg.start_frame + n):
        e = logo.env[i] if i < len(logo.env) else 0.0
        t = max(0.0, (i - 0.5) / FPS)
        if s.logo_effect == "pulse":
            sc = 1 + LOGO_PULSE * e
            cmds.append(f"{t:.4f} crop@lg w {round(logo.w * 4 * (1 + LOGO_PULSE) / sc)}, "
                        f"crop@lg h {round(logo.h * 4 * (1 + LOGO_PULSE) / sc)};")
        elif s.logo_effect == "glow":
            cmds.append(f"{t:.4f} colorchannelmixer@gl aa {0.08 + 0.92 * e:.3f};")
        else:
            rr = max(logo.w, logo.h) / 2 * (LOGO_RING[0] + (LOGO_RING[1] - LOGO_RING[0]) * e)
            cs = round(150 * ring_canvas(logo) / rr)
            cmds.append(f"{t:.4f} crop@rg w {cs}, crop@rg h {cs}, colorchannelmixer@rg aa {0.15 + 0.85 * e:.3f};")
    cmd_file = os.path.join(logo.cmd_dir, f"logo_seg{seg.index:03d}.txt")
    with open(cmd_file, "w") as f:
        f.write("\n".join(cmds) + "\n")
    send = f"sendcmd=f='{_filter_path(cmd_file)}'"

    if s.logo_effect == "pulse":
        # Fixed-size canvas; growing = cropping a smaller window from a 4x
        # master and scaling it back (smooth to 1/4 px, overlay size constant).
        cw4, ch4 = round(logo.w * 4 * (1 + LOGO_PULSE)), round(logo.h * 4 * (1 + LOGO_PULSE))
        pw, ph = (cw4 - logo.w * 4) // 2, (ch4 - logo.h * 4) // 2
        cw, ch = round(logo.w * (1 + LOGO_PULSE)), round(logo.h * (1 + LOGO_PULSE))
        # Pad to exactly cw4 x ch4: with an odd margin, iw + 2 * (margin // 2)
        # came out 1 px smaller than the crop window and the filter failed.
        return (f"{stream(i0)},pad={cw4}:{ch4}:{pw}:{ph}:color=black@0,{send},"
                f"crop@lg=w={cw4}:h={ch4},scale={cw}:{ch}:flags=bicubic[logo];"
                f"[slide_bar]setsar=1[sb];"
                f"[sb][logo]overlay=x={round(logo.cx - cw / 2)}:y={ly(round(logo.cy - ch / 2))}:format=auto[outv]")
    if s.logo_effect == "glow":
        m = round(max(logo.w, logo.h) * 0.2)
        return (f"{stream(i0)},{send},colorchannelmixer@gl=aa=0.08[glow];"
                f"[{i0 + 1}:v]format=rgba,setsar=1[logo];[slide_bar]setsar=1[sb];"
                f"[sb][glow]overlay=x={x - m}:y={ly(y - m)}:format=auto[sg];"
                f"[sg][logo]overlay=x={x}:y={ly(y)}:format=auto[outv]")
    d = ring_canvas(logo)
    return (f"{stream(i0)},{send},crop@rg=w=400:h=400,scale={d}:{d}:flags=bicubic,"
            f"colorchannelmixer@rg=aa=0.15[ring];"
            f"[{i0 + 1}:v]format=rgba,setsar=1[logo];[slide_bar]setsar=1[sb];"
            f"[sb][ring]overlay=x={round(logo.cx - d / 2)}:y={ly(round(logo.cy - d / 2))}:format=auto[sr];"
            f"[sr][logo]overlay=x={x}:y={ly(y)}:format=auto[outv]")


def ring_canvas(logo: Logo) -> int:
    """Size of the ring layer: just fits the largest ring (+ its line width)."""
    return round(max(logo.w, logo.h) * 1.2)


def end_screen_filter(seg: Segment, s: Settings) -> str:
    """Returns ",eq=..." that dims and desaturates the slides from s.end_start on
    (global time t), or "" when the segment ends before the end screen.

    eq with contrast=k and brightness=-(1-k)/2 scales luma by k (a true dim
    toward black, not a lift/crush); saturation scales chroma by k as well so
    colours darken with the image, then loses END_SCREEN_DESATURATE more."""
    if s.end_start is None or seg.trim_end is None:
        return ""
    if seg.start_frame + seg.trim_end - seg.trim_start <= s.end_start * FPS:
        return ""
    T, D = s.end_start, s.end_dim
    p = f"clip((t-{T:.4f})/{END_SCREEN_FADE}\\,0\\,1)"
    return (f",eq=contrast=1-{D:.3f}*{p}:brightness=-0.5*{D:.3f}*{p}"
            f":saturation=(1-{D:.3f}*{p})*(1-{END_SCREEN_DESATURATE}*{p})"
            f":eval=frame:enable=gte(t\\,{T:.4f})")


def segment_command(seg: Segment, slides: List[str], out_path: str, s: Settings,
                    x264_threads: Optional[int], title_card: Optional[str] = None,
                    title_card_size=None, logo: Optional[Logo] = None,
                    glass: Optional[Glass] = None) -> List[str]:
    seg_slides = slides[seg.first_slide:seg.last_slide + 1]
    n = len(seg_slides)
    logo_inputs = list(logo.inputs) if logo else []
    # The card only goes into segments that start before it has disappeared.
    use_card = bool(title_card) and seg.start_frame < s.title_card_end * FPS
    card_index = n + len(logo_inputs)
    glass_index = card_index + (1 if use_card else 0)
    seg_frames = (seg.start_frame, seg.trim_end - seg.trim_start if seg.trim_end is not None else None)
    if glass and not segment_needs_glass(s, *seg_frames):
        glass = None    # bar hidden for the whole segment: no glass inputs at all

    trim = f"trim=start_frame={seg.trim_start}"
    if seg.trim_end is not None:
        trim += f":end_frame={seg.trim_end}"
    graph = ";".join([
        build_slideshow_filter(seg.first_slide, n, s, out_label="chain"),
        # Shift to global time so the bar animation (which uses t) lines up across segments.
        f"[chain]{trim},setpts=PTS-STARTPTS+{seg.start_frame}/({FPS}*TB){end_screen_filter(seg, s)}[slide]",
        build_overlay_filter("slide", s, glass, glass_index, seg_frames)
        if glass or s.bar_style != "glass" else "[slide]copy[slide_bar]",
        logo_filter(seg, logo, n, s) if logo else "[slide_bar]setsar=1[outv]",
    ] + ([title_card_filter("outv", card_index, title_card_size, s, "titled")] if use_card else []) + [
        # Re-tag at the end: the encoder writes the frames' colour tags (ffmpeg 8
        # ignores -color_primaries/-color_trc when the frames say "unknown"),
        # and some filters in the graph drop primaries/transfer.
        f"[{'titled' if use_card else 'outv'}]setpts=PTS-STARTPTS,{BT709_PARAMS}[seg]",
    ])
    cmd = ["ffmpeg", "-y"] + still_inputs(seg_slides + logo_inputs)
    if use_card:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{s.title_card_end + 1 / FPS:.4f}",
                "-i", title_card]
    if glass:
        cmd += still_inputs(glass.inputs)
    cmd += ["-filter_complex", graph, "-map", "[seg]", "-an"]
    cmd += encoder_args(s, x264_threads) + ["-pix_fmt", "yuv420p"] + BT709_ENCODE_ARGS + [out_path]
    return cmd


def render_segments(segments: List[Segment], slides: List[str], tmpdir: str, s: Settings,
                    n_workers: int, title_card: Optional[str] = None,
                    logo: Optional[Logo] = None, glass: Optional[Glass] = None) -> List[str]:
    x264_threads = s.threads or max(1, (os.cpu_count() or 1) // n_workers)
    card_size = probe_image_size(title_card) if title_card else None
    tasks = []
    for seg in segments:
        out = os.path.join(tmpdir, f"segment_{seg.index:03d}.mp4")
        cmd = segment_command(seg, slides, out, s, x264_threads, title_card, card_size, logo, glass)
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
    """Concatenate encoded segments and the (already AAC-encoded) audio track
    without re-encoding anything."""
    list_file = os.path.join(tmpdir, "segments.txt")
    with open(list_file, "w") as f:
        for p in seg_paths:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-i", audio,
           "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest", out_path]
    print_cmd("Joining segments + audio", cmd)
    rc = run_streaming(cmd)
    if rc != 0:
        die(f"ffmpeg failed with code {rc}")


def render_video(slides: List[str], duration: float, tmpdir: str, s: Settings,
                 max_per_segment: int, workers_override: Optional[int],
                 title_card: Optional[str] = None, logo: Optional[Logo] = None,
                 glass: Optional[Glass] = None) -> List[str]:
    """Render all segments; returns their paths in order."""
    n = len(slides)
    cpus = os.cpu_count() or 2

    # Memory per process: ~250 MB ffmpeg overhead + per slide one (possibly
    # supersampled) yuv420p frame held by zoompan + buffers (measured ~1.3 GB
    # for 15 slides at 1080p with 4x supersampling).
    frame_mb = max(1, s.width * s.height * s.supersample ** 2 * 3 // 2 // (1024 * 1024))
    slides_per_seg = min(n, max_per_segment, max(MIN_SLIDES_PER_SEGMENT, ceil(n / cpus))) + 1
    if workers_override:  # 0/None = auto
        n_workers = max(1, workers_override)
    else:
        n_workers = workers_for(cpus, 250 + slides_per_seg * (40 + frame_mb), cpus)

    segments = plan_segments(n, segment_count(n, max_per_segment, n_workers),
                             ceil(duration * FPS - 1e-9), s)
    n_workers = min(n_workers, len(segments))

    return render_segments(segments, slides, tmpdir, s, n_workers, title_card, logo, glass)


def prepare_audio(args, tmpdir: str) -> str:
    """Enhance (optional), save the processed copy and encode the video's AAC
    track. Runs in a background thread while the video renders: the render is
    limited by the hardware video encoder, so the CPU has room for this."""
    audio = args.audio
    if args.audio_enhance:
        # The saved copy (podcast / Spotify) is normalised to --audio-target-lufs;
        # the video's track to --audio-video-lufs (YouTube plays at about -14 and
        # only turns loud tracks down, so a quieter upload just sounds quiet).
        opts = enhance_audio.options_from_args(args, prefix="audio-")
        saved = os.path.join(tmpdir, "audio_enhanced.wav")
        enhance_audio.enhance(args.audio, saved, opts)
        save_path = args.audio_save or enhanced_audio_path(args.audio)
        if save_path.lower().endswith(".wav"):
            shutil.copyfile(saved, save_path)
        else:
            enhance_audio.convert(saved, save_path)
        print("Processed audio saved:", save_path)
        audio = saved
        if opts.normalize and args.audio_video_lufs != opts.target_lufs:
            audio = os.path.join(tmpdir, "audio_video.wav")
            enhance_audio.enhance(args.audio, audio,
                                  dataclasses.replace(opts, target_lufs=args.audio_video_lufs))
    elif args.audio_save:
        print("Warning: --audio-save ignored with --no-audio-enhance")
    track = os.path.join(tmpdir, "audio_track.m4a")
    enhance_audio.convert(audio, track, bitrate=args.audio_bitrate)
    return track


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args():
    p = argparse.ArgumentParser(description="Generate a slideshow video from images and an MP3.")
    p.add_argument("--images", required=True, help="Directory containing input images")
    p.add_argument("--audio", required=True, help="Audio file path (WAV, MP3, anything FFmpeg can decode)")
    p.add_argument("--output", default=None, help="Output video file path (e.g. out.mp4). Default: the --audio path with _<width>x<height> appended and the extension changed to .mp4 (song.wav -> song_2560x1440.mp4)")
    p.add_argument("--width", type=int, default=2560, help="Target video width (default: 2560; YouTube serves its better VP9/AV1 encodes for 1440p+ uploads)")
    p.add_argument("--height", type=int, default=1440, help="Target video height (default: 1440)")
    p.add_argument("--seconds-per-image", type=float, default=6.0, help="Seconds each image remains before transition")
    p.add_argument("--fade-duration", type=float, default=0.5, help="Crossfade transition duration in seconds")
    p.add_argument("--bar-height", type=int, default=None, help="Height of bottom bar in px, 0 disables bar and text (default: 120 at 1080p; Scaled with the output height (value given is for 1080p))")
    p.add_argument("--bar-style", default="glass", choices=["blur", "glass", "solid"], help="Bottom bar style: 'blur' (frosted glass with optional tint), 'glass' (floating Liquid Glass capsule: edge refraction, specular rim, shadow) or 'solid' (flat alpha color)")
    p.add_argument("--bar-color", default="#00464a", help="Bar color for --bar-style solid (hex, e.g. #000000)")
    p.add_argument("--bar-alpha", type=float, default=0.7, help="Bar opacity for --bar-style solid (0.0 - 1.0)")
    p.add_argument("--bar-blur", type=int, default=None, help="Blur radius for --bar-style blur (boxblur radius; default: 20 at 1080p; Scaled with the output height (value given is for 1080p))")
    p.add_argument("--bar-tint", default="#00464a", help="Tint color overlaid on the blurred bar for --bar-style blur (hex). Empty string disables tint.")
    p.add_argument("--bar-tint-alpha", type=float, default=0.75, help="Opacity of the bar tint for --bar-style blur (0.0 - 1.0, default: 0.75)")
    p.add_argument("--glass-inset", type=int, default=None, help="--bar-style glass: gap between the glass and the left/right/bottom frame edges in px (default: 16 at 1080p; Scaled with the output height (value given is for 1080p))")
    p.add_argument("--glass-radius", type=int, default=0, help="--bar-style glass: corner radius in px (default: 0 = capsule, fully rounded ends)")
    p.add_argument("--glass-refraction", type=int, default=None, help="--bar-style glass: lens strength = max displacement at the rim in px, 0 disables, max 127 (default: 28 at 1080p; Scaled with the output height (value given is for 1080p))")
    p.add_argument("--glass-blur", type=float, default=None, help="--bar-style glass: blur (gaussian sigma) of the content seen through the glass; Liquid Glass is mostly clear (default: 4 at 1080p; Scaled with the output height (value given is for 1080p))")
    p.add_argument("--glass-tint-alpha", type=float, default=0.6, help="--bar-style glass: opacity of --bar-tint over the glass (default: 0.6)")
    p.add_argument("--bar-slide-in-delay", type=float, default=None, help="Seconds before the bar slides up into view (default: 3, or --title-card-duration when a title card is used)")
    p.add_argument("--bar-visible-duration", type=float, default=30.0, help="Seconds the bar remains visible before sliding back down (default: 30)")
    p.add_argument("--bar-slide-duration", type=float, default=1.0, help="Duration of each slide animation in seconds, both in and out (default: 1)")
    p.add_argument("--zoom", action=argparse.BooleanOptionalAction, default=True, help="Slow Ken Burns zoom-in toward the center of each image during its display time (default: on; --no-zoom disables)")
    p.add_argument("--zoom-end-percent", type=float, default=105.0, help="On-screen scale of the image on its last frame before the transition, in percent (100 = no zoom, 110 = zoomed in 10%%). Only used with --zoom.")
    p.add_argument("--zoom-supersample", type=int, default=6, help="Supersample factor for the zoom stage: higher = smoother motion (less 1px stepping), but slower and more memory. Try 6-8 for short/aggressive zooms or low resolutions; 1-2 to render faster. The enlarged frame is capped at 7680px wide, but outputs up to 1440p always get at least 4x (an explicit lower value is honoured). Only used with --zoom. (default: 6)")
    p.add_argument("--text", default="", help="Text to display centered in bottom bar")
    p.add_argument("--multi-text", default="", help="JSON file with several bar appearances, each with its own text: [{\"text\": \"...\", \"start\": \"44s\", \"end\": \"74s\"}, ...]. The bar slides in at start and is gone again at end (times: seconds, \"44s\", \"1m14s\" or \"1:14\"). Replaces --text, --bar-slide-in-delay and --bar-visible-duration")
    p.add_argument("--text-size", type=int, default=None, help="Font size for bottom bar text (default: 114 at 1080p; Scaled with the output height (value given is for 1080p); shrunk to fit the bar if needed)")
    p.add_argument("--font", default="/Library/Fonts/BebasNeue-Regular.ttf", help="Optional font file path for drawtext")
    p.add_argument("--fade-name", default="mix", help="Transition between slides: 'mix' (default; a calm transition picked per slide: " + ", ".join(CALM_TRANSITIONS) + "), one FFmpeg xfade name (e.g. fade, wipeleft, circleopen) or a comma-separated list to pick from")
    p.add_argument("--motion", default="mix", choices=list(MOTION_POOLS), help="Camera move per slide with --zoom: 'mix' (default; zoom in, zoom out, zoom toward a corner, pan left/right, picked per slide), 'in' (always zoom toward the centre), 'out', 'pan'")
    p.add_argument("--variation-seed", type=int, default=1, help="Seed for the per-slide motion/transition choice; change it for a different arrangement (default: 1)")
    p.add_argument("--end-screen", type=float, default=10.0, help="Dim the last N seconds of the video (same length, nothing added) as a calmer background for YouTube end screens (default: 10; 0 disables)")
    p.add_argument("--end-screen-dim", type=float, default=0.45, help="How much the end-screen slides are darkened, 0-1 (default: 0.45)")
    p.add_argument("--logo", default="", help="Logo image, shown at the left end of the bar (vertically centred on it). Transparent margins are trimmed automatically. Empty (default) or a missing file = no logo")
    p.add_argument("--logo-effect", default="glow", choices=list(LOGO_EFFECTS), help="Logo reaction to the voice: glow (default; soft orange #ff5800 glow behind the logo), pulse (logo grows up to 4%%), ring (orange #ff5800 ring around the logo widens and brightens), none (static logo)")
    p.add_argument("--logo-size", type=int, default=0, help="Logo size in px (longest side). Default: bar height - 20 (glass: capsule height - 20; 20 px at 1080p, scaled with the output height), so the logo and its effect stay inside the bar; 105 px at 1080p without a bar")
    p.add_argument("--title-card", default="", help="Optional title card image (PNG; transparency recommended) shown centred at the start, then shrinking down into the bottom bar")
    p.add_argument("--title-card-duration", type=float, default=6.0, help="Seconds the title card stays fully visible before moving into the bar (default: 6)")
    p.add_argument("--title-card-transition", type=float, default=None, help="Seconds the title card takes to glide into the bar (default: --bar-slide-duration, or 1 if that is 0)")
    p.add_argument("--contrast", type=float, default=1.05, help="Slide contrast (1.0 = unchanged, default: 1.05)")
    p.add_argument("--saturation", type=float, default=1.15, help="Slide saturation (1.0 = unchanged, default: 1.15)")
    p.add_argument("--vignette", type=float, default=0.3, help="Vignette strength 0-1 (0 = off, default: 0.3; 0.4 = FFmpeg's default vignette)")
    p.add_argument("--encoder", default="auto", choices=["auto", "x264", "vt_h264", "vt_hevc"], help="Video encoder: auto tries h264_videotoolbox if available on macOS")
    p.add_argument("--bitrate", default="auto", help="Target video bitrate (e.g. 12M). 'auto' (default) = YouTube's recommended upload bitrate for the output height: 40M at 2160p+, 16M at 1440p+, otherwise 8M. Pass '' to use CRF instead.")
    p.add_argument("--crf", type=int, default=18, help="CRF value for x264 (quality). Ignored if --bitrate provided or hardware encoder without CRF.")
    p.add_argument("--threads", type=int, help="Threads for libx264 encoding (ignored for VideoToolbox).")
    p.add_argument("--blur", type=int, default=None, help="Background blur strength (boxblur radius). Applied once during preprocessing. (default: 10 at 1080p; Scaled with the output height (value given is for 1080p))")
    p.add_argument("--force-rgba", action="store_true", help="No effect; kept so existing commands keep working (slides are now composed in RGB).")
    p.add_argument("--max-inputs-per-pass", type=int, default=15, help="Max images per ffmpeg process. The video is always rendered as parallel segments (at least one per CPU core when there are enough images).")
    p.add_argument("--segment-workers", type=int, default=0, help="Parallel workers for segment rendering (default: 0 = auto-size to CPU cores and available RAM).")
    a = p.add_argument_group("audio", "Audio clean-up before muxing (see enhance_audio.py)")
    a.add_argument("--audio-enhance", action=argparse.BooleanOptionalAction, default=True,
                   help="High-pass + loudness normalisation (+ optional denoise) of the audio track (default: on; --no-audio-enhance uses the file as is)")
    enhance_audio.add_cli_options(a, prefix="audio-")
    a.add_argument("--audio-video-lufs", type=float, default=-14.0,
                   help="Loudness target of the video's audio track (default: -14, YouTube's playback level); "
                        "the saved copy (--audio-save) uses --audio-target-lufs")
    a.add_argument("--audio-bitrate", default="256k", help="AAC bitrate of the video's audio track (default: 256k)")
    a.add_argument("--audio-save", default="", help="Where to save the processed audio (e.g. for Spotify; .wav/.flac/.mp3/.m4a). Default: next to --audio as <name>_enhanced.<ext>")
    return p.parse_args()


def enhanced_audio_path(audio: str) -> str:
    """song.wav -> song_enhanced.wav (same folder, same extension)."""
    base, ext = os.path.splitext(audio)
    return f"{base}_enhanced{ext}"


def default_output_path(audio: str, width: int, height: int) -> str:
    """song.wav -> song_2560x1440.mp4 (same folder as the audio)."""
    return f"{os.path.splitext(audio)[0]}_{width}x{height}.mp4"


def validate_inputs(args):
    check_dep("ffmpeg")
    check_dep("ffprobe")
    if not os.path.isdir(args.images):
        die(f"Images directory not found: {args.images}")
    if not os.path.isfile(args.audio):
        die(f"Audio file not found: {args.audio}")
    if not args.output:
        args.output = default_output_path(args.audio, args.width, args.height)
        print(f"No --output given; writing to {args.output}")
    if args.logo and not os.path.isfile(os.path.expanduser(args.logo)):
        print(f"Warning: logo file not found ({args.logo}); rendering without a logo")
        args.logo = ""
    args.logo = os.path.expanduser(args.logo) if args.logo else ""
    if args.title_card and not os.path.isfile(args.title_card):
        die(f"Title card file not found: {args.title_card}")
    if args.multi_text:
        args.multi_text = os.path.expanduser(args.multi_text)
        if not os.path.isfile(args.multi_text):
            die(f"--multi-text file not found: {args.multi_text}")


def parse_seconds(v, where: str) -> float:
    """44 / 44.5 / "44" / "44s" / "1m14s" / "1h2m" / "1:14" / "1:02:03.5" -> seconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        t = v.strip().lower().replace(" ", "")
        try:
            if ":" in t:
                sec = 0.0
                for part in t.split(":"):
                    sec = sec * 60 + float(part)
                return sec
            m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?", t)
            if t and m:
                h, mi, se = (float(g) if g else 0.0 for g in m.groups())
                return h * 3600 + mi * 60 + se
        except ValueError:
            pass
    die(f"--multi-text: invalid time {v!r} in {where} (use e.g. 44, \"44s\", \"1m14s\" or \"1:14\")")


def load_bar_windows(path: str, audio_duration: float) -> tuple:
    """--multi-text JSON -> sorted ((start, end, text), ...), clipped to the audio."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        die(f"--multi-text: {path} is not valid JSON ({e}). Expected a list: "
            '[{"text": "one", "start": "44s", "end": "74s"}, ...]')
    if isinstance(data, dict) and isinstance(data.get("bars"), list):
        data = data["bars"]
    if not isinstance(data, list):
        die(f"--multi-text: {path} must contain a list of {{\"text\", \"start\", \"end\"}} objects")
    windows = []
    for i, item in enumerate(data, 1):
        where = f"entry {i}"
        if not isinstance(item, dict) or "start" not in item or "end" not in item:
            die(f"--multi-text: {where} needs \"start\" and \"end\"")
        a, b = parse_seconds(item["start"], where), parse_seconds(item["end"], where)
        if a < 0 or b <= a:
            die(f"--multi-text: {where}: end ({b:g}s) must be after start ({a:g}s) and start >= 0")
        if a >= audio_duration:
            print(f"Warning: --multi-text {where} starts after the end of the video ({audio_duration:.1f}s); skipped")
            continue
        if b > audio_duration:
            print(f"Warning: --multi-text {where} ends after the video; shortened to {audio_duration:.1f}s")
            b = audio_duration
        windows.append((a, b, str(item.get("text", ""))))
    windows.sort(key=lambda w: w[0])
    for (a0, b0, _), (a1, _, _) in zip(windows, windows[1:]):
        if a1 < b0:
            die(f"--multi-text: bar windows overlap ({a0:g}-{b0:g}s and from {a1:g}s)")
    if not windows:
        print("Warning: --multi-text has no entries inside the video; the bar never appears")
    return tuple(windows)


def default_logo_size(args, k: float) -> int:
    """Bar height - 20 (the glass capsule is shorter by --glass-inset), so the
    logo and its effect stay inside the bar; LOGO_SIZE without a bar. The
    1080p-tuned 20 / 40 / LOGO_SIZE are scaled by k = height / 1080."""
    inner = args.bar_height - (max(0, args.glass_inset) if args.bar_style == "glass" else 0)
    return inner - round(20 * k) if inner > 40 * k else round(LOGO_SIZE * k)


# Flags whose 1080p-tuned defaults (in px) follow the output height when the
# user does not set them: dest -> value at REFERENCE_HEIGHT.
SCALED_DEFAULTS = {
    "bar_height": 120, "text_size": 114, "glass_inset": 16, "glass_refraction": 28,
    "bar_blur": 20, "blur": 10, "glass_blur": 4.0,
}


def resolve_scaled_defaults(args, k: float):
    """Fill the flags left unset (None) with their 1080p default scaled by k.
    An explicit value (including 0 = off) is used as given."""
    for name, base in SCALED_DEFAULTS.items():
        if getattr(args, name) is None:
            setattr(args, name, base * k if isinstance(base, float) else max(1, round(base * k)))


def resolve_bitrate(bitrate: Optional[str], height: int) -> Optional[str]:
    """--bitrate auto -> YouTube's recommended bitrate for the output height."""
    if bitrate != "auto":
        return bitrate
    return next(b for min_h, b in AUTO_BITRATES if height >= min_h)


def make_settings(args, audio_duration: float) -> Settings:
    px_scale = args.height / REFERENCE_HEIGHT
    resolve_scaled_defaults(args, px_scale)
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

    S, V = args.bar_slide_duration, args.bar_visible_duration
    if args.multi_text:
        if args.text:
            print("Warning: --multi-text given; --text is ignored")
        if args.bar_slide_in_delay is not None:
            print("Warning: --multi-text given; --bar-slide-in-delay is ignored")
        windows = load_bar_windows(args.multi_text, audio_duration)
    elif S > 0:
        windows = ((bar_delay, bar_delay + 2 * S + V, args.text),)
    else:   # static bar for the whole video
        windows = ((0.0, None, args.text),)

    text_bar_h = (args.bar_height - max(0, args.glass_inset) if args.bar_style == "glass"
                  else args.bar_height)
    text_border = max(1, round(TEXT_BORDER * px_scale))
    text_gap = max(1, round(TEXT_MIN_GAP * px_scale))
    layout = tuple(fit_bar_text(text, args.text_size, args.font, text_bar_h, text_border, text_gap)
                   for _, _, text in windows)

    transitions = ([t.strip() for t in args.fade_name.split(",") if t.strip()]
                   if args.fade_name != "mix" else list(CALM_TRANSITIONS))
    if not transitions:
        die("--fade-name is empty")

    return Settings(
        width=args.width, height=args.height,
        seconds_per_image=args.seconds_per_image, fade_dur=fade_dur, transitions=tuple(transitions),
        bg_blur=args.blur,
        contrast=args.contrast, saturation=args.saturation, vignette=max(0.0, min(1.0, args.vignette)),
        zoom_enabled=zoom_enabled, zoom_end_percent=args.zoom_end_percent,
        zoom_supersample=args.zoom_supersample,
        motion=args.motion, variation_seed=args.variation_seed,
        bar_height=args.bar_height, bar_style=args.bar_style, bar_color=args.bar_color,
        bar_alpha=args.bar_alpha, bar_blur=args.bar_blur, bar_tint=args.bar_tint,
        bar_tint_alpha=args.bar_tint_alpha, bar_slide_in_delay=bar_delay,
        bar_visible_duration=args.bar_visible_duration, bar_slide_duration=args.bar_slide_duration,
        bar_windows=windows, bar_text_layout=layout, multi_text=bool(args.multi_text),
        glass_inset=max(0, args.glass_inset), glass_radius=max(0, args.glass_radius),
        glass_refraction=max(0, min(127, args.glass_refraction)), glass_blur=max(0.0, args.glass_blur),
        glass_tint_alpha=max(0.0, min(1.0, args.glass_tint_alpha)),
        text=args.text, text_size=args.text_size, font=args.font, logo=args.logo,
        logo_effect=args.logo_effect,
        logo_size=args.logo_size if args.logo_size > 0 else default_logo_size(args, px_scale),
        force_rgba=args.force_rgba,
        title_card=args.title_card, title_card_duration=max(0.0, args.title_card_duration),
        title_card_transition=max(0.0, title_trans),
        end_start=max(0.0, audio_duration - args.end_screen) if args.end_screen > 0 else None,
        end_dim=max(0.0, min(1.0, args.end_screen_dim)),
        encoder=resolve_encoder(args.encoder), bitrate=resolve_bitrate(args.bitrate, args.height),
        crf=args.crf, threads=args.threads, px_scale=px_scale,
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

    s = make_settings(args, audio_duration)
    needed = slides_needed_for_audio(audio_duration, s.seconds_per_image, s.fade_dur)
    chosen = [images[i % len(images)] for i in range(needed)]

    tmpdir = tempfile.mkdtemp(prefix="wave_slides_")
    try:
        with ThreadPoolExecutor(max_workers=1) as logo_pool:
            logo_job = logo_pool.submit(prepare_logo, tmpdir, s, args.audio, ceil(audio_duration * FPS))
            slides = prepare_slides(chosen, tmpdir, s)
            title_card = prepare_title_card(tmpdir, s)
            glass = prepare_glass(tmpdir, s)
            logo = logo_job.result()

        max_per_segment = max(2, args.max_inputs_per_pass or len(slides))
        with ThreadPoolExecutor(max_workers=1) as audio_pool:
            audio_job = audio_pool.submit(prepare_audio, args, tmpdir)
            seg_paths = render_video(slides, audio_duration, tmpdir, s, max_per_segment,
                                     args.segment_workers, title_card, logo, glass)
            audio_track = audio_job.result()
        join_segments(seg_paths, audio_track, args.output, tmpdir)

        print("Done:", args.output)
        print(f"Audio length: {audio_duration:.2f}s, images used: {len(slides)}, resolution: {s.width}x{s.height}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception as e:
            print("Warning: could not remove temp dir", tmpdir, e, file=sys.stderr)


if __name__ == "__main__":
    main()
