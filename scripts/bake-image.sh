#!/usr/bin/env bash
# Bake Ubuntu 24.04 Vultr 6 vCPU / 16 GB / 320 GB golden image.
# Run as root on a FRESH VM. Do not bake API keys.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
ENGINE_DIR="${ENGINE_DIR:-/opt/kala-studio-engine}"

apt-get update
apt-get install -y unattended-upgrades ffmpeg python3.12 python3.12-venv python3-pip \
  build-essential git curl ca-certificates \
  libnss3 libnss3-dev libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 \
  libgtk-3-0 libgbm1 libasound2t64 libxshmfence1 libxcomposite1 libxdamage1 \
  libxrandr2 libpango-1.0-0 libpangocairo-1.0-0 libx11-xcb1 libxcb-dri3-0 \
  libxss1 libxtst6 fonts-liberation fonts-noto-color-emoji fonts-dejavu-core \
  libcairo2 libatspi2.0-0 libwayland-client0 \
  pkg-config libcairo2-dev libpango1.0-dev python3-dev sox
dpkg-reconfigure -f noninteractive unattended-upgrades || true

# Node 22
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

mkdir -p "$ENGINE_DIR"
if [[ ! -f "$ENGINE_DIR/config/saas.yaml" ]]; then
  echo "Copy the engine tree to $ENGINE_DIR before baking."
  exit 1
fi
cd "$ENGINE_DIR"
export VIDEO_GEN_LOCAL_ENABLED=false
export SAAS_DISABLE_BLENDER=1

python3.12 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install piper-tts jsonschema pyyaml boto3 google-genai
# ManimCE for math_animate (CPU). Heavy but required by YAML optional_tools.
pip install 'manim==0.19.0' || pip install manim

# One English Piper voice
mkdir -p /opt/piper/voices
python3 - <<'PY'
from pathlib import Path
import urllib.request
base = Path("/opt/piper/voices")
base.mkdir(parents=True, exist_ok=True)
url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
print("downloading piper voice", url)
urllib.request.urlretrieve(url, base / "en_US-lessac-medium.onnx")
print("ok", (base / "en_US-lessac-medium.onnx").stat().st_size)
PY

cd remotion-composer
npm ci --omit=dev
# Headless Chromium for Remotion
npx remotion browser ensure || npx --yes puppeteer browsers install chrome || true
npx remotion render src/index.tsx HeroTitle /tmp/hero-title-smoke.mp4 \
  --frames=0-29 --width=1920 --height=1080 || {
  echo "ERROR: Remotion HeroTitle smoke failed"
  exit 1
}
cd "$ENGINE_DIR"

# Motion Canvas adapter packages (not the drawtext 10s smoke fallback)
npm --prefix tools/video/mc_adapter ci || (cd tools/video/mc_adapter && npm install)
test -d tools/video/mc_adapter/node_modules || { echo "ERROR: mc_adapter node_modules missing"; exit 1; }

npm --prefix smokes/motion_canvas_10s ci || (cd smokes/motion_canvas_10s && npm install)

# mermaid-cli for diagram_gen
npm install -g @mermaid-js/mermaid-cli
command -v mmdc

# HyperFrames doctor (real, not --help || true)
npx --yes hyperframes@latest doctor || {
  echo "ERROR: hyperframes doctor failed"
  exit 1
}

# CLIP / sentence-transformer weights actually downloaded into the snapshot cache
python3 - <<'PY'
from pathlib import Path
try:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("clip-ViT-B-32")
    vec = model.encode(["snapshot bake"])
    print("clip weights ok", len(vec[0]), "cache", Path.home() / ".cache" / "huggingface")
except Exception as exc:
    raise SystemExit(f"CLIP weight download failed: {exc}") from exc
PY

apt-get clean
npm cache clean --force || true
rm -rf /root/.cache/pip /root/.npm/_cacache || true
# Keep piper + sentence-transformer weights
find /tmp -maxlevel 1 -type d -name 'npm-*' -exec rm -rf {} + || true

install -m 0644 "$ENGINE_DIR/scripts/studio-agent.service" /etc/systemd/system/studio-agent.service
systemctl daemon-reload
systemctl enable studio-agent.service

{
  echo "# Snapshot size"
  echo
  echo "Measured by scripts/bake-image.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ):"
  echo
  echo "- Engine tree \`du -sh $ENGINE_DIR\`: $(du -sh "$ENGINE_DIR" | awk '{print $1}')"
  echo "- remotion-composer/node_modules: $(du -sh "$ENGINE_DIR/remotion-composer/node_modules" 2>/dev/null | awk '{print $1}')"
  echo "- mc_adapter/node_modules: $(du -sh "$ENGINE_DIR/tools/video/mc_adapter/node_modules" 2>/dev/null | awk '{print $1}')"
  echo "- huggingface cache: $(du -sh /root/.cache/huggingface 2>/dev/null | awk '{print $1}')"
  echo "- Disk: $(df -h / | tail -1)"
  echo "- Vultr compressed snapshot GB: target **12–25 GB**"
  echo "- Used on disk after prune: target **20–35 GB**"
  echo
  echo "Do not install Blender or CUDA. 320 GB disk is scratch for stock + renders, not snapshot size."
} | tee "$ENGINE_DIR/docs/SNAPSHOT_SIZE.md"

echo "bake complete. Confirm: blender --version fails; nvidia-smi fails."
command -v blender && echo "ERROR: blender present" && exit 1 || echo "blender absent OK"
command -v nvidia-smi && echo "ERROR: nvidia-smi present" && exit 1 || echo "nvidia-smi absent OK"
command -v mmdc
python3 -c "import manim; print('manim', manim.__version__)"
