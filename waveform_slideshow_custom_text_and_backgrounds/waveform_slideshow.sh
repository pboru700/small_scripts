#!/usr/bin/env bash
set -euo pipefail

# waveform_slideshow_with_bar_fixed_numbers.sh (locale-safe + valid FFmpeg formats)
#
# Usage example:
# ./waveform_slideshow_with_bar_fixed_numbers.sh ./imgs lofi.mp3 out.mp4 10 0.5 1920 1080 240 60 "#002244@0.8" "Galactic Cruise" 36 /Library/Fonts/Arial.ttf fade

if (( $# < 3 )); then
  echo "Usage: $0 IMAGES_DIR AUDIO_FILE OUTPUT_FILE [SECONDS_PER_IMAGE] [TRANSITION_DUR] [WIDTH] [HEIGHT] [WAVE_H] [BAR_H] [BAR_COLOR] [TEXT] [TEXT_SIZE] [FONTFILE] [XFADENAME]" >&2
  exit 1
fi

images_dir="$1"
audio_file="$2"
output_file="$3"

seconds_per_image="${4:-20}"
transition_dur="${5:-1.0}"
width="${6:-400}"
height="${7:-400}"
wave_h="${8:-120}"
bar_h="${9:-60}"
bar_color="${10:-#000000@0.7}"
text="${11:-}"
text_size="${12:-24}"
fontfile="${13:-}"
xfade_name="${14:-fade}"

wave_margin=10

# checks
if [[ ! -d "$images_dir" ]]; then echo "Images directory not found: $images_dir" >&2; exit 1; fi
if [[ ! -f "$audio_file" ]]; then echo "Audio file not found: $audio_file" >&2; exit 1; fi

# collect images (case-insensitive)
shopt -s nullglob
images=()
for ext in jpg jpeg png gif JPG JPEG PNG GIF; do
  for f in "$images_dir"/*."$ext"; do [[ -f "$f" ]] && images+=("$f"); done
done
shopt -u nullglob

if (( ${#images[@]} == 0 )); then echo "No images found in $images_dir" >&2; exit 1; fi

# audio duration
audio_duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$audio_file" 2>/dev/null || true)
audio_duration="$(printf '%s' "$audio_duration" | awk '{$1=$1;print}')"
if [[ -z "$audio_duration" ]]; then echo "Could not determine audio duration (ffprobe failed)." >&2; exit 1; fi

# needed images (ceil) — force C locale for numeric formatting
needed_images=$(LC_NUMERIC=C awk -v d="$audio_duration" -v s="$seconds_per_image" 'BEGIN { if (s<=0) s=1; print int((d + s - 1e-9)/s) }')
if (( needed_images < 1 )); then needed_images=1; fi

# adjust transition duration if >= per-image duration (C locale)
D="$seconds_per_image"
T="$transition_dur"
okT=$(LC_NUMERIC=C awk -v D="$D" -v T="$T" 'BEGIN { if (T >= D) { printf "%.6f", D/2 } else { printf "%.6f", T } }')
if [[ "$okT" != "$T" ]]; then
  echo "Warning: transition duration ($T) >= per-image duration ($D). Reducing to $okT"
  transition_dur="$okT"
fi

# sort images
IFS=$'\n'
sorted_images=()
for line in $(printf '%s\n' "${images[@]}" | sort); do sorted_images+=("$line"); done
unset IFS

count=${#sorted_images[@]}
inputs=()
for ((i=0; i<needed_images; i++)); do inputs+=( "${sorted_images[$(( i % count ))]}" ); done

# escape text for drawtext
escape_drawtext() {
  local s="$1"
  s="$(printf '%s' "$s" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g" -e 's/:/\\:/g' -e 's/%/%%/g')"
  printf '%s' "$s"
}
escaped_text="$(escape_drawtext "$text")"

# Compute numeric positions (all numbers, no ffmpeg variables) — use C locale for floats
drawbox_y=$(( height - bar_h ))
drawtext_y=$(LC_NUMERIC=C awk -v top="$drawbox_y" -v bh="$bar_h" -v ts="$text_size" 'BEGIN{ printf "%.2f", top + ( (bh - ts) / 2 ) }')
# center waveform vertically (as you requested)
overlay_y=$(LC_NUMERIC=C awk -v h="$height" -v wh="$wave_h" 'BEGIN{ printf "%.2f", (h - wh) / 2 }')

# build ffmpeg args
ffargs=()
for img in "${inputs[@]}"; do ffargs+=( -loop 1 -t "$D" -i "$img" ); done
audio_index=${#inputs[@]}
ffargs+=( -i "$audio_file" )

# step for xfade offsets — C locale
step=$(LC_NUMERIC=C awk -v D="$D" -v T="$transition_dur" 'BEGIN { printf "%.6f", (D - T) }')

# build filter_complex using numeric constants for y/x positions and valid format names
fc=""

# scale/prepare inputs
for ((i=0;i<${#inputs[@]};i++)); do
  fc+="[${i}:v]scale=${width}:${height},format=rgba,setsar=1,trim=duration=${D},setpts=PTS-STARTPTS[v${i}];"
done

# chain xfade (use format=yuv420p, numeric offsets)
if (( ${#inputs[@]} == 1 )); then
  fc+="[v0]format=yuv420p[slide];"
else
  for ((k=0;k<${#inputs[@]}-1;k++)); do
    if (( k == 0 )); then in1="[v0]"; else in1="[xf$k]"; fi
    in2="[v$((k+1))]"
    n=$((k+1))
    offset=$(LC_NUMERIC=C awk -v s="$step" -v n="$n" 'BEGIN { printf "%.6f", s * n }')
    out_label="xf$((k+1))"
    # use format=yuv420p for xfade output
    fc+="${in1}${in2}xfade=transition=${xfade_name}:duration=${transition_dur}:offset=${offset},format=yuv420p[${out_label}];"
  done
  last=$(( ${#inputs[@]} - 1 ))
  fc+="[xf$last]format=yuv420p[slide];"
fi

# draw the bar (use numeric drawbox_y) and text (numeric y)
if [[ -n "$escaped_text" ]]; then
  if [[ -n "$fontfile" ]]; then
    drawtext_fontpart="fontfile=${fontfile}:"
  else
    drawtext_fontpart=""
  fi
  fc+="[slide]drawbox=x=0:y=${drawbox_y}:w=iw:h=${bar_h}:color=${bar_color}:t=fill,${drawtext_fontpart}drawtext=text='${escaped_text}':fontcolor=white:fontsize=${text_size}:x=(w-text_w)/2:y=${drawtext_y}:box=0[slide_bar];"
else
  fc+="[slide]drawbox=x=0:y=${drawbox_y}:w=iw:h=${bar_h}:color=${bar_color}:t=fill[slide_bar];"
fi

# waveform (yuva420p) from audio — sized width x wave_h
fc+="[${audio_index}:a]showwaves=s=${width}x${wave_h}:mode=cline:colors=${bar_color}@0.6,format=yuva420p[wave];"

# overlay waveform (numeric overlay_y, centered horizontally)
fc+="[slide_bar][wave]overlay=x=(W-w)/2:y=${overlay_y}:format=auto[outv]"

# assemble and run ffmpeg
cmd=( ffmpeg -y )
cmd+=( "${ffargs[@]}" )
cmd+=( -filter_complex "$fc" -map "[outv]" -map "${audio_index}:a" -c:v libx264 -crf 18 -preset veryfast -c:a aac -shortest "$output_file" )

echo "Running ffmpeg: images=${#inputs[@]}, resolution=${width}x${height}, per-image=${D}s, transition=${transition_dur}s, bar_h=${bar_h}px"
"${cmd[@]}"

echo "Done: $output_file"
echo "computed positions: drawbox_y=${drawbox_y}, drawtext_y=${drawtext_y}, overlay_y=${overlay_y}"
