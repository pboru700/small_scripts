# Slideshow Video Generator

`waveform_argparse.py` builds an MP4 slideshow from a folder of images and an audio track. Each image is scaled to fit a fixed resolution (aspect preserved), placed over a blurred, stretched copy of itself, given a slow camera move (zoom in or out, zoom toward a corner, or a pan), and blended into the next one with a varying calm transition. An animated frosted-glass bar with custom text slides in at the bottom, and an optional logo sits at its left end, glowing softly while someone speaks. The video lasts as long as the audio, with the last 20 s gently dimmed as an end screen; images loop if needed.

## Features
- Fixed output resolution (default 1920x1080)
- "Contain" scaling: no cropping, aspect preserved, small images upscaled
- Blurred full-frame background made from the same image
- Slow Ken Burns camera move per slide, varied by default: zoom in, zoom out, zoom toward a corner, pan left/right (`--motion`; `--no-zoom` disables), rendered jitter-free via supersampling
- Calm transitions picked per slide (`fade`, `fadeblack`, `smooth*`, `circleopen`, `hblur`), or any fixed FFmpeg `xfade` transition (`--fade-name`)
- End screen: the last 20 s of the video are gently dimmed and desaturated, as a background for YouTube end screens (`--end-screen`), while the corner logo slides down off the screen; the length does not change
- Bottom bar with centered text that slides up, stays visible, then slides away
  - `blur` style: frosted glass with optional color tint
  - `solid` style: flat semi-transparent color
- Optional logo (`--logo`) at the left end of the bar (vertically centred on it, sized to fit inside), reacting to the voice: soft glow (default), pulse or ring; transparent margins trimmed automatically
- Optional title card (PNG): shown at the start, then glides down and shrinks into the bar, crossfading into the bar text
- Light color grade: a bit more contrast and saturation plus a soft vignette (on by default, baked into each slide, so it costs no render time)
- HEIC/HEIF support (decoded with macOS `sips`)
- Images loop when the audio is longer than one pass through the folder
- Hardware encoding on macOS (VideoToolbox), chosen automatically
- Parallel rendering across all CPU cores
- Audio clean-up (on by default): rumble high-pass, optional denoise, loudness normalisation to -16 LUFS / -1 dBTP; also available standalone as `enhance_audio.py`

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
  --logo logo.png \
  --title-card title.png --title-card-duration 4
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
| `--fade-name` | `mix` | Transition between slides. `mix` picks a calm one per slide (never twice in a row): `fade`, `fadeblack`, `smoothleft`, `smoothright`, `smoothup`, `smoothdown`, `circleopen`, `hblur`. Can also be one FFmpeg `xfade` name (`fade`, `wipeleft`, …) or a comma-separated list to pick from (`fade,fadeblack`). |
| `--variation-seed` | `1` | Seed for the per-slide choice of transitions and camera moves. The same seed gives the same video; change it for a different arrangement. |
| `--blur` | `10` | Background blur radius (boxblur). |

### Look
| Flag | Default | Description |
|---|---|---|
| `--contrast` | `1.05` | Slide contrast (`1` = unchanged). |
| `--saturation` | `1.15` | Slide saturation (`1` = unchanged). |
| `--vignette` | `0.3` | Vignette strength, `0`–`1` (`0` = off, `0.4` = FFmpeg's default vignette). |

The look is applied to the slides only, so the bar, text, logo and title card keep their exact colors. Use `--contrast 1 --saturation 1 --vignette 0` for the untouched images.

### Zoom
| Flag | Default | Description |
|---|---|---|
| `--zoom` / `--no-zoom` | on | Slow camera move during each slide's display time. |
| `--motion` | `mix` | `mix`: per slide one of zoom in, zoom out, zoom toward a corner, pan left, pan right (never the same twice in a row). `in`: always zoom toward the center (the old behaviour). `out`: always zoom out. `pan`: only pans. |
| `--zoom-end-percent` | `105` | Image scale at the most zoomed-in point (`100` = no zoom, `110` = 10% closer). Pans run at this scale. Values `<= 100` disable the camera moves. |
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
| `--bar-slide-in-delay` | `3` | Seconds before the bar slides up. With `--title-card` the default is `--title-card-duration`, so the bar rises while the card glides into it. |
| `--bar-visible-duration` | `30` | Seconds the bar stays fully visible before sliding down. |
| `--bar-slide-duration` | `1` | Length of each slide animation. `0` keeps the bar static for the whole video. |
| `--text` | empty | Text centered in the bar (color `#ffb000` with a 6 px black outline). |
| `--text-size` | `114` | Font size. |
| `--font` | `/Library/Fonts/BebasNeue-Regular.ttf` | Font file for the text. |
| `--logo` | none | Logo image (PNG with transparency recommended). Transparent margins are trimmed automatically. It is centred vertically on the bar (at its resting position) at the left edge. Empty or a missing file: no logo (a missing file prints a warning). |
| `--logo-effect` | `glow` | How the logo reacts to the voice. `glow`: a soft orange (`#ff5800`) glow behind the logo lights up while someone speaks. `pulse`: the logo grows by up to 4%. `ring`: a thin orange (`#ff5800`) ring around the logo widens and brightens. `none`: static logo. The reaction follows phrases, not syllables (fast rise, ~0.45 s fall), and rests in pauses. |
| `--logo-size` | bar height − 20 | Longest side of the logo in px. The default (100 px for a 120 px bar) keeps the logo and its effect inside the bar. Without a bar: 105 px in the bottom-left corner. |

### Title card
| Flag | Default | Description |
|---|---|---|
| `--title-card` | none | Image (PNG recommended) shown centered at the start, scaled to fit the frame. A PNG with a transparent background (e.g. just the stylized title) works best, because the slide stays visible behind it. |
| `--title-card-duration` | `4` | Seconds the card stays fully visible. |
| `--title-card-transition` | `--bar-slide-duration` | Seconds the card takes to shrink and glide into the bar. In the last 30% of the glide it fades out while the bar text fades in. After that the card is gone for good. |

Without a bar (`--bar-height 0`) the card glides to the bottom edge and fades out.

### End screen
| Option | Default | Description |
|---|---|---|
| `--end-screen` | `20` | The last N seconds of the video are dimmed and slightly desaturated (fading in over 1.5 s), as a calmer background for YouTube end-screen elements (YouTube allows 5–20 s). While the dimming fades in, the corner logo slides down off the screen, so it does not cover end-screen elements. The video length and audio are unchanged. `0` disables (the logo then stays to the end). |
| `--end-screen-dim` | `0.45` | How much darker the slides get at the end (`0.45` = 45% darker; `0`–`1`). |

### Audio
| Flag | Default | Description |
|---|---|---|
| `--audio-enhance` / `--no-audio-enhance` | on | Process the audio before muxing (see [Audio clean-up](#audio-clean-up-enhance_audiopy)). `--no-audio-enhance` uses the file as is. |
| `--audio-target-lufs` | `-16` | Integrated loudness target. YouTube and Spotify play at about `-14`. |
| `--audio-true-peak` | `-1` | True-peak ceiling in dBTP. |
| `--audio-highpass` | `80` | High-pass cutoff in Hz (24 dB/oct). `0` disables it. |
| `--audio-denoise` | `off` | `light` / `strong` (FFmpeg `afftdn`) or `rnn` (`arnndn`, needs `--audio-rnn-model`). |
| `--audio-rnn-model` | none | RNNoise `.rnnn` model, e.g. from [rnnoise-models](https://github.com/GregorR/rnnoise-models). |
| `--audio-bitrate` | `256k` | AAC bitrate of the video's audio track. |
| `--audio-save` | `<audio>_enhanced.<ext>` | The processed audio is always saved (with `--audio-enhance`), by default next to `--audio` with an `_enhanced` suffix, e.g. `odcinek.wav` → `odcinek_enhanced.wav`, ready for a Spotify upload. Pass a path (`.wav`, `.flac`, `.mp3`, `.m4a`) to save it elsewhere or in another format. An existing file is overwritten. |

### Encoding and performance
| Flag | Default | Description |
|---|---|---|
| `--encoder` | `auto` | `auto`, `x264`, `vt_h264`, `vt_hevc`. `auto` picks `vt_h264` (VideoToolbox) on macOS, otherwise libx264. |
| `--bitrate` | `6M` | Target bitrate. When set, it is used instead of CRF. Pass `--bitrate ""` to use CRF. |
| `--crf` | `18` | Quality for libx264 (lower = better). For `vt_h264` without a bitrate it is mapped to `-q:v`. |
| `--threads` | auto | libx264 threads per process. |
| `--max-inputs-per-pass` | `15` | Maximum number of images per ffmpeg process (segment). Lower it to reduce memory use. |
| `--segment-workers` | 0 (auto) | Number of segments rendered in parallel. `0` sizes it automatically to the CPU cores and free RAM. |
| `--force-rgba` | off | Use RGBA for the logo overlay. Only needed for special transparency cases. |

## How it works
1. **Prepare slides (parallel).** For every unique image: a "contain"-scaled foreground and a stretched, blurred background are written as JPEGs. They are then composited once into a lossless slide still, with the color grade and vignette applied. HEIC files are first converted with `sips`. The title card is pre-scaled once.
2. **Render segments (parallel).** The timeline is split into segments, at least one per worker. Each ffmpeg process reads its slides as single frames, so decoding and zoom supersampling happen once per slide, not once per frame. It then zooms, crossfades, adds the bar, text, logo and (in the first segments only) the title card, and encodes with the final settings. Segment boundaries sit just after a crossfade has finished, so the cuts are invisible.
3. **Audio.** Unless `--no-audio-enhance`, the track is cleaned up with `enhance_audio.py` (see below).
4. **Join.** Segments are concatenated without re-encoding (`-c copy`) together with the audio track. The output is cut to the audio length.

The audio (clean-up, saving `_enhanced`, AAC 256 kb/s encoding) is processed in a background thread while the segments render, so it adds no time. On macOS the AAC encoder is Apple's AudioToolbox (`aac_at`), which is ~6x faster than FFmpeg's built-in `aac`.

Slide timing: slide `n` starts fading in at `n × (seconds_per_image − fade_duration)`. If the audio is longer than one pass through the folder, the image list repeats.

## Audio clean-up (`enhance_audio.py`)
Standalone script (also used by the video script) for spoken-word tracks:

```bash
python3 enhance_audio.py episode.wav --analyze                    # loudness / true peak / range only
python3 enhance_audio.py episode.wav episode_master.wav           # -16 LUFS, 24-bit WAV
python3 enhance_audio.py episode.wav episode.mp3 --target-lufs -14 --denoise light
```

1. **High-pass** at 80 Hz removes rumble and plosive thumps.
2. **Denoise** (optional): `light`/`strong` = FFT spectral subtraction (`afftdn`), `rnn` = RNNoise neural model (`arnndn`). Use it only when the background noise is audible; on clean recordings it adds metallic artifacts.
3. **Loudness**: pass 1 measures EBU R128 loudness (`ebur128`), pass 2 applies one static gain to the target plus a 4×-oversampled limiter at the true-peak ceiling. The limiter only touches the loudest transients, so there is no pumping. If the result is more than 0.3 LU off target, the gain is corrected once.

Output format follows the extension: `.wav`/`.flac` 24-bit lossless, `.mp3` 320 kb/s, `.m4a` AAC 256 kb/s (`--bitrate` overrides). Other flags match the `--audio-*` options above without the prefix, plus `--no-normalize`.

## Performance notes (Apple Silicon)
- On a MacBook M2, a 9-minute video with 100 slides at 1080p renders in about 1.5 minutes.
- The hardware H.264/HEVC encoder is one shared unit (~200 fps at 1080p on M2). Parallel sessions split that throughput, so encoding time is the practical lower bound. This is why every frame is encoded only once. Render time therefore scales with the frame count: 30 fps takes about 20% longer than 25 fps.
- Only the frames of each transition go through `xfade` (it works in 4:4:4 only, so every frame passing through it is converted 4:2:0 → 4:4:4 → 4:2:0). The rest of each slide is concatenated directly, which saves ~15–45% of the CPU and keeps the colours slightly sharper.
- Each segment process needs about 1–1.3 GB of RAM with zoom at 1080p. If you have spare memory, `--segment-workers 8` can be ~10% faster than the cautious automatic choice.
- `--zoom-supersample 2` or `--no-zoom` cut CPU time a lot. `--encoder x264` uses the CPU instead of the media engine: slower, but with exact CRF control.

## Errors
The script exits with a message if FFmpeg/FFprobe (or `sips` for HEIC) is missing, an input path is invalid, no images are found, the audio duration cannot be read, or an ffmpeg step fails. In the last case the tail of ffmpeg's error output is printed.
