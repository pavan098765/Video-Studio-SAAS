# Snapshot size

Fill-in happens on the first Vultr bake (`scripts/bake-image.sh` overwrites this file with `du`). Local Windows demo does not replace it.

Expected layout after bake (Ubuntu 24.04, 6 vCPU / 16 GB, no Blender, no CUDA):

| Path | Role | Typical size |
| --- | --- | --- |
| `/opt/kala-studio-engine` | Engine tree + venv + node_modules | 8–18 GB |
| `remotion-composer/node_modules` | Remotion + Chromium deps | 1.5–3 GB |
| `tools/video/mc_adapter/node_modules` | Motion Canvas adapter | 200–600 MB |
| `~/.cache/huggingface` | CLIP `clip-ViT-B-32` weights | 0.5–2 GB |
| Piper `en_US-lessac-medium.onnx` | Local TTS | ~60 MB |
| mermaid-cli (`mmdc`) + ManimCE | diagram_gen / math_animate | 0.5–2 GB |

Targets:

- Engine tree `du -sh /opt/kala-studio-engine`: _pending bake_ (script writes the real figure)
- Vultr compressed snapshot GB: target **12–25 GB**
- Used on disk after prune: target **20–35 GB**

Doctors the bake must leave green: ffmpeg, Remotion (`npx remotion render HeroTitle`), HyperFrames (`npx hyperframes doctor`), Motion Canvas adapter `npm ci`, `mmdc`, Manim import, CLIP encode smoke.

Do not install Blender or CUDA. 320 GB disk is scratch for stock + renders, not snapshot size.
