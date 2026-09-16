#!/usr/bin/env bash
set -euo pipefail
A="${1:?remotion mp4}"
B="${2:?motion-canvas mp4}"
OUT="${3:-/tmp/smoke-stitch.mp4}"
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT
# concat demuxer requires identical codecs; re-encode to 1080p30 yuv420p
TMPA="$(mktemp --suffix=.mp4)"
TMPB="$(mktemp --suffix=.mp4)"
trap 'rm -f "$LIST" "$TMPA" "$TMPB"' EXIT
ffmpeg -y -i "$A" -r 30 -s 1920x1080 -c:v libx264 -pix_fmt yuv420p -an "$TMPA"
ffmpeg -y -i "$B" -r 30 -s 1920x1080 -c:v libx264 -pix_fmt yuv420p -an "$TMPB"
printf "file '%s'\nfile '%s'\n" "$TMPA" "$TMPB" > "$LIST"
ffmpeg -y -f concat -safe 0 -i "$LIST" -c:v libx264 -pix_fmt yuv420p -r 30 -an "$OUT"
echo "wrote $OUT"
