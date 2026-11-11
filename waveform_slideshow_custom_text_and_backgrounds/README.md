# Slideshow Video Generator

`waveform_argparse.py` builds an MP4 slideshow from a folder of images and an audio track. Each image is scaled to fit a fixed resolution (aspect preserved), placed over a blurred, stretched copy of itself, slowly zoomed toward its center, and crossfaded into the next one. An animated frosted-glass bar with custom text slides in at the bottom, and an optional logo is overlaid. The video lasts as long as the audio; images loop if needed.

## Features
- Fixed output resolution (default 1920x1080)
- "Contain" scaling: no cropping, aspect preserved, small images upscaled
- Blurred full-frame background made from the same image
- Slow Ken Burns zoom toward each image's center (on by default, `--no-zoom` to disable), rendered jitter-free via supersampling
- Crossfade transitions (`xfade`, any FFmpeg transition name)
- Bottom bar with centered text that slides up, stays visible, then slides away
  - `blur` style: frosted glass with optional color tint
  - `solid` style: flat semi-transparent color
- Optional logo, bottom-left
- HEIC/HEIF support (decoded with macOS `sips`)
- Images loop when the audio is longer than one pass through the folder
- Hardware encoding on macOS (VideoToolbox), chosen automatically
- Parallel rendering across all CPU cores

## Requirements
- Python 3.9+ (standard library only)
- FFmpeg and FFprobe in `PATH`
- macOS `sips` (built in), only for `.heic` images

```bash
brew install ffmpeg
```

## Usage
Minimal:
```bash
python3 waveform_argparse.py \
  --images imgs \
  --audio track.wav \
  --output out.mp4 \
  --text "Toy Battle"
```

More options:
```bash
python3 waveform_argparse.py \
  --images imgs \
  --audio track.mp3 \
  --output out.mp4 \
  --width 1920 --height 1080 \
  --seconds-per-image 10 \
  --fade-duration 0.5 \
  --zoom-end-percent 110 \
  --bar-style blur --bar-blur 20 --bar-tint "#00464a" --bar-tint-alpha 0.6 \
  --text "Sample Title" --text-size 96 \
  --font /Library/Fonts/Arial.ttf \
  --logo logo.png
```

## Argument Reference

### Input / output
| Flag | Default | Description |
|---|---|---|
| `--images` | required | Folder with images (`.jpg`, `.jpeg`, `.png`, `.gif`, `.heic`), shown in case-insensitive filename order. |
| `--audio` | required | Audio track (MP3, WAV, anything FFmpeg can decode). Sets the video length. |
| `--output` | required | Output MP4 path. |
| `--width`, `--height` | `1920`, `1080` | Output resolution. |

### Slides
| Flag | Default | Description |
|---|---|---|
| `--seconds-per-image` | `6` | Time each image is on screen, including its fade-out. |
| `--fade-duration` | `0.5` | Crossfade length. Reduced to half of `--seconds-per-image` if it is too long. |
| `--fade-name` | `fade` | FFmpeg `xfade` transition (`fade`, `wipeleft`, `circleopen`, …). |
| `--blur` | `10` | Background blur radius (boxblur). |

### Zoom
| Flag | Default | Description |
|---|---|---|
| `--zoom` / `--no-zoom` | on | Slow zoom toward the image center during its display time. |
| `--zoom-end-percent` | `105` | Image scale on its last frame (`100` = no zoom, `110` = 10% closer). Values `<= 100` disable the zoom. |
| `--zoom-supersample` | `6` | The zoom crops in whole pixels, which at slow speeds looks like a 1px jitter. Cropping from an enlarged frame shrinks the step to `1/factor` px. Capped so the enlarged frame is at most 7680 px wide, so at 1080p the effective value is 4. Lower (1–2) renders faster; higher means smoother motion and more memory. |

### Bottom bar and text
| Flag | Default | Description |
|---|---|---|
| `--bar-height` | `120` | Bar height in px. `0` disables the bar and text. |
| `--bar-style` | `blur` | `blur` (frosted glass) or `solid` (flat color). |
| `--bar-blur` | `20` | Blur radius for the `blur` style. |
| `--bar-tint` | `#00464a` | Tint over the blurred bar. Empty string disables it. |
| `--bar-tint-alpha` | `0.6` | Tint opacity. |
| `--bar-color` | `#00464a` | Color for the `solid` style. |
| `--bar-alpha` | `0.7` | Opacity for the `solid` style. |
| `--bar-slide-in-delay` | `3` | Seconds before the bar slides up. |
| `--bar-visible-duration` | `30` | Seconds the bar stays fully visible before sliding down. |
| `--bar-slide-duration` | `1` | Length of each slide animation. `0` keeps the bar static for the whole video. |
| `--text` | empty | Text centered in the bar (color `#ffb000`). |
| `--text-size` | `114` | Font size. |
| `--font` | `/Library/Fonts/BebasNeue-Regular.ttf` | Font file for the text. |
| `--logo` | none | Logo image, scaled to 105x105, placed bottom-left. Transparency is kept. |

### Encoding and performance
| Flag | Default | Description |
|---|---|---|
| `--encoder` | `auto` | `auto`, `x264`, `vt_h264`, `vt_hevc`. `auto` picks `vt_h264` (VideoToolbox) on macOS, otherwise libx264. |
| `--bitrate` | `6M` | Target bitrate. When set, it is used instead of CRF. Pass `--bitrate ""` to use CRF. |
| `--crf` | `18` | Quality for libx264 (lower = better). For `vt_h264` without a bitrate it is mapped to `-q:v`. |
| `--threads` | auto | libx264 threads per process. |
| `--max-inputs-per-pass` | `15` | Maximum number of images per ffmpeg process (segment). Lower it to reduce memory use. |
| `--segment-workers` | auto | Number of segments rendered in parallel. By default it is sized to the CPU cores and free RAM. |
| `--force-rgba` | off | Use RGBA for the logo overlay. Only needed for special transparency cases. |

## How it works
1. **Prepare slides (parallel).** For every unique image: a "contain"-scaled foreground and a stretched, blurred background are written as JPEGs. They are then composited once into a lossless slide still. HEIC files are first converted with `sips`.
2. **Render segments (parallel).** The timeline is split into segments, at least one per worker. Each ffmpeg process reads its slides as single frames, so decoding and zoom supersampling happen once per slide, not once per frame. It then zooms, crossfades, adds the bar, text and logo, and encodes with the final settings. Segment boundaries sit just after a crossfade has finished, so the cuts are invisible.
3. **Join.** Segments are concatenated without re-encoding (`-c copy`) and the audio is encoded to AAC. The output is cut to the audio length.

Slide timing: slide `n` starts fading in at `n × (seconds_per_image − fade_duration)`. If the audio is longer than one pass through the folder, the image list repeats.

## Performance notes (Apple Silicon)
- On a MacBook M2, a 9-minute video with 100 slides at 1080p renders in about 1.5 minutes.
- The hardware H.264/HEVC encoder is one shared unit (~200 fps at 1080p on M2). Parallel sessions split that throughput, so encoding time is the practical lower bound. This is why every frame is encoded only once.
- Each segment process needs about 1–1.3 GB of RAM with zoom at 1080p. If you have spare memory, `--segment-workers 8` can be ~10% faster than the cautious automatic choice.
- `--zoom-supersample 2` or `--no-zoom` cut CPU time a lot. `--encoder x264` uses the CPU instead of the media engine: slower, but with exact CRF control.

## Errors
The script exits with a message if FFmpeg/FFprobe (or `sips` for HEIC) is missing, an input path is invalid, no images are found, the audio duration cannot be read, or an ffmpeg step fails. In the last case the tail of ffmpeg's error output is printed.
