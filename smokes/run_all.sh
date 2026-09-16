#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
SMOKE_DIR="${SMOKE_DIR:-/tmp/studio-smokes}"
mkdir -p "$SMOKE_DIR"

probe() {
  local f="$1"
  ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height,codec_name,pix_fmt,avg_frame_rate \
    -of default=nw=1 "$f"
  local w h codec pix
  w=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$f")
  h=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$f")
  codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$f")
  pix=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$f")
  [[ "$w" == "1920" && "$h" == "1080" ]] || { echo "not 1080p: $f"; exit 1; }
  [[ "$codec" == "h264" ]] || { echo "not h264: $f"; exit 1; }
  [[ "$pix" == "yuv420p" ]] || { echo "not yuv420p: $f"; exit 1; }
}

bash "$ROOT/smokes/remotion_10s/render.sh" "$SMOKE_DIR/smoke-remotion.mp4"
probe "$SMOKE_DIR/smoke-remotion.mp4"

bash "$ROOT/smokes/hyperframes_10s/render.sh" "$SMOKE_DIR/smoke-hyperframes.mp4"
probe "$SMOKE_DIR/smoke-hyperframes.mp4"

bash "$ROOT/smokes/motion_canvas_10s/render.sh" "$SMOKE_DIR/smoke-motion-canvas.mp4"
probe "$SMOKE_DIR/smoke-motion-canvas.mp4"

bash "$ROOT/smokes/ffmpeg_stitch.sh" \
  "$SMOKE_DIR/smoke-remotion.mp4" \
  "$SMOKE_DIR/smoke-motion-canvas.mp4" \
  "$SMOKE_DIR/smoke-stitch.mp4"
probe "$SMOKE_DIR/smoke-stitch.mp4"

echo "ALL SMOKES OK"
