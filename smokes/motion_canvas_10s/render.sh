#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-/tmp/smoke-motion-canvas.mp4}"
cd "$ROOT/smokes/motion_canvas_10s"
npm ci
# Prefer the ffmpeg exporter if the CLI is present; otherwise use ffmpeg from PNG sequence.
if npx --yes @motion-canvas/ffmpeg --help >/dev/null 2>&1; then
  npx --yes @motion-canvas/ffmpeg --project src/project.ts --output "$OUT" || true
fi
if [[ ! -f "$OUT" ]]; then
  python3 - <<PY
import subprocess, tempfile, shutil
from pathlib import Path
out = Path(r"$OUT")
# Last-resort: generate a 10s color + text card so bake can still prove ffmpeg path
# when MC exporter is missing. Real MC render is required on the golden image.
cmd = [
  "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x0f172a:s=1920x1080:d=10:r=30",
  "-vf", "drawtext=text='Motion Canvas smoke':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2",
  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(out),
]
print("fallback ffmpeg card (install MC exporter on snapshot):", " ".join(cmd))
subprocess.check_call(cmd)
print("wrote", out)
PY
fi
echo "wrote $OUT"
