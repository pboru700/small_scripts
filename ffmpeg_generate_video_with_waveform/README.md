# Description 
This script will generate video from mp3 audio file. This simple wrapper script will use given jpg file as background and generate waveform on top of it based on audio file. I'm putting it here so that I won't lose it anytime soon.

# Requirements
- ffmpeg installed - run in terminal `brew install ffmpeg` on mac

# Usage
Script requires 3 arguments in correct order:
- background_image - path to jpg file
- audio_file - path to mp3 audio file
- output_video_file - path to store mp4 output file

# Example
Assuming all files and script are in user home directory:
```
~/generate_video.sh ~/background_image.jpg ~/audio_file.mp3 ~/output_video_file.mp4
```
This will output video file in the same directory.
