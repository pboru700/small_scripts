#!/bin/bash

export bg_image_path=$1
export audio_file=$2
export output_file=$3

ffmpeg -i $audio_file -i $bg_image_path \
-filter_complex "[0:a]showwaves=colors=0xff1646@0.3\
 :scale=sqrt:mode=cline,format=yuva420p[v];\
 [1:v]scale=400:400[bg];\
 [bg][v]overlay=(W-w)/2:(H-h)/2[outv]"\
  -map "[outv]" -map 0:a -c:v libx264 -c:a aac \
$output_file