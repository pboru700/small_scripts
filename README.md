# 🎵 Waveform Slideshow Video Generator

`waveform_slideshow.sh` is a Bash script that creates an **MP4 video slideshow** synchronized with an **audio waveform visualization**.  
It cycles through a folder of images, changing the background every few seconds, adds **smooth transitions**, and overlays a **colored bar with custom text** at the bottom.

Perfect for generating music videos, lofi visuals, or background animations for YouTube or social posts.

---

## 🧩 Features

✅ Uses all images in a specified folder  
✅ Smooth crossfade transitions between images  
✅ Synchronized audio waveform animation  
✅ Customizable resolution, duration, colors, and text  
✅ Compatible with macOS, Linux, and ffmpeg ≥ 5.0  
✅ No external dependencies besides **ffmpeg**

---

## ⚙️ Requirements

- **ffmpeg** (must support `xfade`, `showwaves`, `drawbox`, `drawtext`)
- **bash** (v4 or higher recommended)

You can check your ffmpeg version with:
```bash
ffmpeg -version
```

If missing, install via:
- macOS: brew install ffmpeg
- Ubuntu/Debian: sudo apt install ffmpeg
- Fedora: sudo dnf install ffmpeg

## 🚀 Usage

```bash
./waveform_slideshow.sh \
  <images_dir> <audio_file> <output_file> \
  [seconds_per_image] [transition_dur] [width] [height] \
  [wave_height] [bar_height] [bar_color] \
  [text] [text_size] [fontfile] [xfade_transition]
```
### Example

```bash
./waveform_slideshow_with_bar_fixed_numbers.sh \
  ./imgs lofi.mp3 output.mp4 \
  20 1.0 1280 720 120 60 "#003366@0.9" \
  "Now playing — Lofi Beats" 36 /Library/Fonts/Arial.ttf fade
```

This example will:
- Create a 1280x720 video
- Cycle images from ./imgs/ every 20 seconds
- Add a 1s crossfade between slides
- Overlay a waveform (height = 120px) above a blue bar (60px tall)
- Render the text “Now playing — Lofi Beats” in white, centered on the bar
- Use the fade transition style

## 🎛️ Parameters

| Parameter           | Default       | Description                                                       |
| ------------------- | ------------- | ----------------------------------------------------------------- |
| `<images_dir>`      | *(required)*  | Directory containing `.jpg`, `.jpeg`, `.png`, or `.gif` files     |
| `<audio_file>`      | *(required)*  | Path to input audio file (MP3, WAV, etc.)                         |
| `<output_file>`     | *(required)*  | Path for output MP4 video                                         |
| `seconds_per_image` | 20            | Duration each image stays on screen                               |
| `transition_dur`    | 1.0           | Duration of crossfade transition                                  |
| `width`             | 400           | Video width in pixels                                             |
| `height`            | 400           | Video height in pixels                                            |
| `wave_height`       | 120           | Height of waveform visualization                                  |
| `bar_height`        | 60            | Height of the colored bar at bottom                               |
| `bar_color`         | "#000000@0.7" | Color of bar (supports hex + alpha, e.g., `#003366@0.9`)          |
| `text`              | *(empty)*     | Text rendered over the bottom bar                                 |
| `text_size`         | 24            | Font size in pixels                                               |
| `fontfile`          | *(empty)*     | Path to a TTF/OTF font (optional; system default if not provided) |
| `xfade_transition`  | fade          | ffmpeg’s xfade style (`fade`, `wipeleft`, `circleopen`, etc.)     |

## 🖼️ Output Layers

```bash
 ┌──────────────────────────────┐
 │ Background image slideshow   │ ← transitions every N seconds
 │ with xfade animation         │
 ├──────────────────────────────┤
 │ Waveform visualization       │ ← matches audio length
 │ (centered horizontally)      │
 ├──────────────────────────────┤
 │ Colored bar with text        │ ← spans full width, at bottom
 └──────────────────────────────┘
```

## 🎨 Transition Types

You can choose from any of ffmpeg’s built-in transitions for xfade_transition, e.g.:
- fade
- wipeleft, wiperight, slideup, slidedown
- circleopen, circleclose
- smoothleft, smoothright, smoothup, smoothdown

### Example

```bash
xfade_transition=circleopen
```

## 💡 Tips
Keep transition_dur < seconds_per_image for smooth transitions
To make text always visible, use a semi-transparent bar color like #000000@0.6
You can install additional fonts (e.g. brew install fontconfig and place TTFs in /Library/Fonts)

## 🧰 Example Output

```bash
./waveform_slideshow_with_bar_fixed_numbers.sh \
  ./images lofi.mp3 lofi_output.mp4 \
  15 0.8 1920 1080 160 80 "#222244@0.8" \
  "LoFi Vibes — Study & Chill" 40 /Library/Fonts/Arial.ttf smoothleft
```

Output layout:
- 1080p widescreen video
- 15s per image with smooth horizontal transition
- Waveform centered above a dark semi-transparent bar
- White text overlay at bottom: “LoFi Vibes — Study & Chill”

## 🪲 Troubleshooting
❌ Error: Undefined constant or missing '('
→ Your ffmpeg doesn’t like inline math expressions — this version precomputes them in shell, so it’s already fixed.
❌ Error: fontfile not found
→ Provide full path to a .ttf or .otf file. Example: /Library/Fonts/Arial.ttf
❌ Video has only one image
→ Make sure image filenames have proper extensions (.jpg, .png, etc.) and directory path doesn’t contain trailing / typos.

## 📄 License
MIT License — use freely, modify, and share.
