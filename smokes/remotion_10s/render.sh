#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-/tmp/smoke-remotion.mp4}"
cd "$ROOT/remotion-composer"
npx remotion render src/index.tsx HeroTitle "$OUT" \
  --frames=0-299 \
  --codec=h264 \
  --pixel-format=yuv420p \
  --props="$(cat "$ROOT/smokes/remotion_10s/props.json")"
echo "wrote $OUT"
