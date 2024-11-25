# Description 
This script will generate video from mp3 audio file. Script will use given jpg file as background and generate waveform on top of it based on audio file.

# Requirements
- ffmpeg installed - run in terminal `brew install ffmpeg` on mac

# Usage
Script requires 3 arguments in correct order:
- background_image - path to jpg file
- audio_file - path to mp3 audio file
- output file - path to store mp4 output file

Example:
```
~/generate_video.sh ~/background.jpg ~/audio_file.mp3 ~/output_video_file.mp4
```
