#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-/tmp/smoke-hyperframes.mp4}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<PY
from pathlib import Path
from tools.video.hyperframes_compose import HyperFramesCompose

root = Path(r"$ROOT")
out = Path(r"$OUT")
out.parent.mkdir(parents=True, exist_ok=True)
hf = HyperFramesCompose()
result = hf.execute({
    "operation": "render_existing",
    "workspace_path": str(root / "smokes" / "hyperframes_10s"),
    "output_path": str(out),
    "skip_contrast": True,
})
if not result.success:
    raise SystemExit(result.error or "hyperframes smoke failed")
print("wrote", result.data.get("output", out))
PY
