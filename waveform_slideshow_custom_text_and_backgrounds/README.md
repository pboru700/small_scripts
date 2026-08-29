# Waveform Slideshow Generator

`waveform_argparse.py` builds an MP4 slideshow from a folder of images and an MP3 (or compatible) audio track. Images are scaled to fit a fixed resolution while preserving aspect ratio, placed over a blurred stretched version of themselves, and transitioned with crossfades. A semi‑transparent bottom bar displays custom text and an opaque waveform visualization derived from the audio (with selectable direction and optional rounded edges). Optionally a logo can be overlaid. Performance‑oriented flags allow hardware encoding (macOS VideoToolbox), reduced intermediate sizes, and controlled blur strength.

## Features
- Fixed output resolution (e.g. 1920x1080)
- Contain + upscale/downscale image scaling (no cropping, aspect preserved)
- Blurred full‑frame background (stretched source image + adjustable blur strength)
- Crossfade transitions (`xfade` with selectable style)
- Optional Ken Burns zoom‑in toward each image's center over its display time (`--zoom`, `--zoom-end-percent`)
- Semi‑transparent bottom bar with centered text
- Waveform visualization (FFmpeg `showwaves` mode=cline)
  - Direction: centered (`both`), single‑sided `top` or `bottom`
  - Optional rounded/softened bars via gaussian blur
  - Horizontal performance scaling (`--wave-scale`) with upscale
  - Adjustable vertical position (`--wave-y`)
- Loop images if audio duration exceeds total per‑image time
- Optional logo overlay bottom‑left (preserves transparency)
- Hardware encoder auto‑selection on macOS (`--encoder auto` prefers VideoToolbox)
- Legacy `--color` flag still supported (maps to `--bar-color` + `--bar-alpha`)
- Fine‑grained encoding control: bitrate or CRF, thread count
- Optional force RGBA pipeline (`--force-rgba`) when advanced transparency workflows needed

## Requirements
- Python 3.8+
- FFmpeg / FFprobe available in `PATH`

## Installation
No extra Python dependencies are required beyond the standard library. Ensure FFmpeg is installed:

```bash
# macOS (Homebrew)
brew install ffmpeg
```

Place your images and audio in accessible paths.

## Basic Usage
```bash
python3 waveform_argparse.py \
  --images imgs \
  --audio track.mp3 \
  --output out.mp4 \
  --width 1920 --height 1080 \
  --seconds-per-image 10 \
  --fade-duration 0.5 \
  --wave-height 240 \
  --bar-height 120 \
  --bar-color "#000000" \
  --bar-alpha 0.7 \
  --text "Sample Title" \
  --text-size 42 \
  --font /Library/Fonts/Arial.ttf \
  --fade-name fade \
  --logo logo.png
```

### Legacy Flag Example
```bash
python3 waveform_argparse.py \
  --images imgs \
  --audio track.mp3 \
  --output out.mp4 \
  --width 1920 --height 1080 \
  --seconds-per-image 10 \
  --fade-duration 0.5 \
  --wave-height 480 \
  --bar-height 120 \
  --color "#035358@0.7" \
  --text "Recenzja Bonfire" \
  --text-size 42 \
  --font /Library/Fonts/Arial.ttf \
  --fade-name fade \
  --logo logo.png
```

## Argument Reference
- `--images`: Directory with input images (`.jpg`, `.jpeg`, `.png`, `.gif`).
- `--audio`: MP3 audio track (other formats often work if FFmpeg decodes them).
- `--output`: Output MP4 file path.
- `--width`, `--height`: Target video dimensions.
- `--seconds-per-image`: Duration each image is shown (excluding fade overlap portion).
- `--fade-duration`: Duration of crossfade; auto‑reduced to half if >= seconds per image.
- `--zoom`: Enable a slow zoom‑in toward the center of each image for the whole time it is on screen (off by default).
- `--zoom-end-percent`: On‑screen scale of the image on its last frame before the transition, in percent (`100` = no zoom, `110` = zoomed in 10%). Default `110`. Values `<= 100` disable the zoom. Only used with `--zoom`.
- `--zoom-supersample`: Supersample factor for the zoom stage (default `4`). The zoom crops in whole pixels, which at a slow zoom looks like a 1px stepping/jitter; rendering the crop from an enlarged frame shrinks the step to `1/factor` px. Higher = smoother but slower and more memory (try `6`–`8` for short or aggressive zooms and low resolutions; `1`–`2` to render faster). Capped so the enlarged frame stays ≤ 7680px wide. Only used with `--zoom`.
- `--wave-height`: Height of waveform overlay region.
- `--wave-direction`: Waveform style: `both` (centered, double‑sided), `top`, or `bottom` single‑sided bars.
- `--wave-round`: Gaussian blur sigma to soften/round waveform bars (0 disables).
- `--wave-y`: Override automatic vertical centering with explicit Y pixel offset.
- `--wave-scale`: Horizontal generation scaling (0.1–1.0). Lower values generate a narrower waveform then upscale → performance gain.
- `--bar-height`: Pixel height of bottom bar (0 disables bar & text but keeps waveform).
- `--bar-color`: Hex base color for bar (e.g. `#222222`).
- `--bar-alpha`: Alpha for bottom bar; waveform always opaque base color.
- `--color` (deprecated): Combined hex@alpha form; overrides `--bar-color`/`--bar-alpha` if present.
- `--text`: Text string placed centered in bar.
- `--text-size`: Font size.
- `--font`: Optional font file path for drawtext.
- `--fade-name`: FFmpeg `xfade` transition type (e.g. `fade`, `wipeleft`, `circleopen`).
- `--logo`: Optional logo image placed bottom‑left (scaled to ~105x105 preserving transparency).
- `--encoder`: `auto`, `x264`, `vt_h264`, `vt_hevc`. Auto prefers VideoToolbox on macOS else libx264.
- `--bitrate`: Target bitrate (e.g. `6M`). If set, overrides CRF mode for applicable encoders.
- `--crf`: Quality target for libx264 (ignored if `--bitrate` or hardware encoder mapping).
- `--threads`: Thread count for libx264.
- `--blur`: Background boxblur radius (lower = faster, less diffuse).
- `--force-rgba`: Force RGBA intermediate pixel format (use only for transparency manipulation; otherwise keep yuv420p for speed).

## Scaling Logic
Foreground scaling formula:
- If source aspect ≥ target aspect ("wider"), scale width to target width; height auto.
- Else scale height to target height; width auto.
- Smaller images are upscaled so one dimension always matches target; other dimension ≤ target.
- A blurred stretched copy of the original (scaled exactly to target) becomes the background.

## Waveform Rendering & Options
The waveform uses `showwaves=mode=cline` for continuous vertical bars:
- Directional modes: `both` (centered), `top`, `bottom`. Single‑sided modes internally generate double height and crop one half to retain full amplitude scale.
- Horizontal scaling (`--wave-scale`): Generate at a fraction of full width, then bilinear upscale horizontally. Values <1 reduce CPU load.
- Rounding (`--wave-round`): Applies a small gaussian blur (e.g. 0.8–1.2) for smoother, rounded bar edges. Larger values (>3) produce a soft glow effect and increase cost.
- Vertical positioning (`--wave-y`): Manually sets the overlay Y coordinate; default auto centers (or centers beneath bar if present).

Opaque color: Waveform color derived from bar base color ignoring alpha. (Potential future `--wave-color` could decouple.)

## Transitions
Crossfades chained between consecutive image segments using FFmpeg `xfade`. Offset = `(seconds_per_image - fade_duration) * index`.

## Looping Behavior
If audio duration requires more images than provided, the image list wraps (modulo indexing) until enough segments are created.

## Exit Conditions & Errors
Script exits with an error message if:
- FFmpeg/FFprobe missing
- Images/audio/logo paths invalid
- No images discovered
- `ffprobe` fails to read audio duration
- `ffmpeg` returns non‑zero (encoding error)

## Performance & Tuning Tips
- Lower `--blur` (e.g. 6–8) for faster background processing while retaining softness.
- Use `--wave-scale 0.5` or `0.3` to reduce waveform generation width and save CPU.
- Prefer hardware encoders (`vt_h264` / `vt_hevc`) on macOS for faster encoding; fall back to `x264` for consistent CRF control.
- Adjust `--crf` (libx264) for quality: 18 good, 16 high, 14 near visually lossless.
- Keep `--wave-round` modest (≤1.2) to avoid excess blur cost.
- Avoid `--force-rgba` unless needed; staying in yuv420p reduces conversion overhead.
- Larger resolutions amplify filter cost; tune scale/blur accordingly.

## Example Higher Quality Encoding
You can manually tweak after generation:
```bash
ffmpeg -i out.mp4 -c:v libx264 -crf 15 -preset slow -c:a copy out_hq.mp4
```

## Future Enhancements (Ideas)
- Separate `--wave-color` flag.
- Custom logo position (x/y) and scaling.
- Progress / ETA reporting.
- Additional transition presets bundles.
- Multi‑channel audio peak visualization variant.

---
Feel free to modify and extend the script; contributions can add configurability while keeping defaults simple.
