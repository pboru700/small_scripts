# Waveform Slideshow Generator

`waveform_argparse.py` builds an MP4 slideshow from a folder of images and an MP3 audio track. Images are scaled to fit a fixed resolution while preserving aspect ratio, placed over a blurred stretched version of themselves, and transitioned with crossfades. A semi‑transparent bottom bar displays custom text and an opaque centered waveform visualization derived from the audio. Optionally a logo can be overlaid.

## Features
- Fixed output resolution (e.g. 1920x1080)
- Contain + upscale/downscale image scaling (no cropping, aspect preserved)
- Blurred full‑frame background (stretched source image + blur)
- Crossfade transitions (`xfade` with selectable style)
- Semi‑transparent bottom bar with centered text
- Solid centered waveform (FFmpeg `showwaves` mode=cline)
- Loop images if audio duration exceeds total per‑image time
- Optional logo overlay bottom‑left
- Legacy `--color` flag still supported (maps to `--bar-color` + `--bar-alpha`)

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
- `--audio`: MP3 audio track.
- `--output`: Output MP4 file path.
- `--width`, `--height`: Target video dimensions.
- `--seconds-per-image`: Duration each image is shown (excluding fade overlap portion).
- `--fade-duration`: Duration of crossfade; auto‑reduced to half if >= seconds per image.
- `--wave-height`: Height of waveform overlay.
- `--bar-height`: Pixel height of bottom bar (0 disables bar & text).
- `--bar-color`: Hex base color for bar (e.g. `#222222`).
- `--bar-alpha`: Alpha for bottom bar (only bar uses transparency; waveform is opaque).
- `--color` (deprecated): Combined hex@alpha form; overrides `--bar-color`/`--bar-alpha` if present.
- `--text`: Text string placed centered in bar.
- `--text-size`: Font size.
- `--font`: Optional font file path for drawtext (fallback to default if omitted).
- `--fade-name`: FFmpeg `xfade` transition type (e.g. `fade`, `wipeleft`, `circleopen`).
- `--logo`: Optional logo image placed bottom‑left (scaled to 115x96).

## Scaling Logic
Foreground scaling formula:
- If source aspect ≥ target aspect ("wider"), scale width to target width; height auto.
- Else scale height to target height; width auto.
- Smaller images are upscaled so one dimension always matches target; other dimension ≤ target.
- A blurred stretched copy of the original (scaled exactly to target) becomes the background.

## Waveform Rendering
Uses `showwaves` with `mode=cline` producing vertical bars from center for a solid wave. Color derived from bar base color (alpha ignored).

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

## Tips
- Use a lossless or high‑quality source audio; video CRF 18 is visually good. Adjust CRF (lower = higher quality, larger file).
- For darker backgrounds, choose a semi‑transparent bar color with moderate alpha (0.5–0.8) to keep text legible.
- Consider experimenting with different `--fade-name` transitions for stylistic variety.

## Example Higher Quality Encoding
You can manually tweak after generation:
```bash
ffmpeg -i out.mp4 -c:v libx264 -crf 15 -preset slow -c:a copy out_hq.mp4
```

## Future Enhancements (Ideas)
- Optional `--wave-color` separate from bar.
- Custom logo position/size flags.
- Progress percentage output.
- Support for additional audio formats.

---
Feel free to modify and extend the script; contributions can add configurability while keeping defaults simple.
