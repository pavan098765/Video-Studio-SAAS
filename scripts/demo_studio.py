#!/usr/bin/env python3
"""Local Studio form → engine runner.

Default is a real Gemini+Brave job (not canned fixtures). Footage pipelines
require --footage. Cinematic/avatar honest-fail without gen-video keys.

  python scripts/demo_studio.py --topic "How photosynthesis works"
  python scripts/demo_studio.py --keys
  python scripts/demo_studio.py --doctor
  python scripts/demo_studio.py --setup
  python scripts/demo_studio.py --topic "Voyager" --pipeline documentary-montage --open
  python scripts/demo_studio.py --topic "Clips" --pipeline talking-head --footage path/to.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.env_loader import load_env  # noqa: E402
from runner.atelier import is_scaffold, project_path, slug_for  # noqa: E402
from runner.ffmpeg_bin import find_ffmpeg, find_ffprobe  # noqa: E402
from runner.loop import run_job  # noqa: E402
from runner.mode import resolve as resolve_mode  # noqa: E402
from runner.preflight import (  # noqa: E402
    ensure_demo_machine,
    machine_ok,
    machine_report,
)
from runner import telemetry  # noqa: E402

SAFE_PIPELINES = [
    "animated-explainer",
    "animation",
    "character-animation",
    "documentary-montage",
    "screen-demo",
    "clip-factory",
    "hybrid",
    "localization-dub",
    "podcast-repurpose",
    "talking-head",
]

FAIL_WITHOUT_KEYS = ["cinematic", "avatar-spokesperson"]
NEED_FOOTAGE = ["talking-head", "clip-factory", "hybrid", "localization-dub", "podcast-repurpose"]
ALL_PIPELINES = SAFE_PIPELINES + FAIL_WITHOUT_KEYS

KEYS_TEXT = """
API keys for this engine demo
=============================

Fill repo-root .env (gitignored; copy from .env.example). Dummy values
are placeholders only -- replace dummy-* before a live run.

Required for a real demo_studio run:
  GOOGLE_API_KEY / GEMINI_API_KEY   planner + visual QA (Gemini)
  BRAVE_API_KEY                     web_search for research_brief URLs
  ANTHROPIC_API_KEY                 atelier TSX if saas.yaml atelier is Claude
  TTS                               Piper can speak on non-karaoke pipelines.
                                    Explainer / animation / hybrid karaoke needs
                                    ELEVENLABS_API_KEY (with-timestamps) or fal timestamps.
  ffmpeg + ffprobe                  1080p30 H.264+AAC (demo can download a Windows build)
  Node + Remotion + Motion Canvas   python scripts/demo_studio.py --doctor
                                    python scripts/demo_studio.py --setup
  Gemini free-tier                  engine waits on RPM/TPM and stops on RPD
                                    (15/250k/500 Lite; 5/250k/20 for 3.8-2.5 Flash).
                                    Atelier Gemini walks 3.8 -> 3.7 -> 3.6 -> 3.5 -> 3
                                    -> 2.5 Flash -> 3.5 Flash Lite when a model is spent.

Optional:
  PEXELS_API_KEY / PIXABAY_API_KEY  stock stills and B-roll
  OPENAI_API_KEY / AZURE_SPEECH_KEY Whisper API for footage pipelines (never local Whisper)
  STUDIO_LLM_ATELIER_*              swap atelier provider/model (e.g. openai_compat GLM)
  FAL_KEY / MINIMAX_API_KEY / KLING_API_KEY / RUNWAY_API_KEY / HEYGEN_API_KEY
  REPLICATE_API_TOKEN / ARK_API_KEY / XAI_API_KEY
                                    cinematic / avatar-spokesperson only

Not used:
  Local GPU / Blender               disabled in config/saas.yaml
  Gemini Google Search grounding    Brave is web_search

Default pipeline is animated-explainer (45s). Pass --pipeline safe or all to opt in.
Footage pipelines (talking-head, hybrid, clip-factory, podcast, dub) need --footage
and a Whisper API key. Cinematic/avatar honest-fail without gen-video keys.
Explainer/animation default composition_mode=atelier (hand-authored TSX). Pass
--composition-mode templated to test the Explainer catalog path.
--review stops at storyboard.json (Kala-shaped). Default local demo stitches final.mp4.
""".strip()


def _env_on(name: str) -> bool:
    return bool(os.environ.get(name) or os.environ.get(name.replace("GOOGLE", "GEMINI")))


def _print_doctor() -> bool:
    rows = machine_report()
    print("Machine doctor")
    print("==============")
    width = max(len(row["id"]) for row in rows)
    for row in rows:
        mark = "ok  " if row["status"] == "ok" else "MISS"
        print(f"  {mark}  {row['id']:<{width}}  {row['detail']}")
    print()
    if machine_ok(rows):
        print("All demo machine checks passed.")
        return True
    print("Missing pieces. Install Node.js if node/npm/npx failed.")
    print("Then:  python scripts/demo_studio.py --setup")
    return False


def _print_env() -> None:
    print("ffmpeg:", find_ffmpeg() or "MISSING")
    print("ffprobe:", find_ffprobe() or "MISSING")
    print("GOOGLE_API_KEY:", "set" if (_env_on("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")) else "missing")
    print("BRAVE_API_KEY:", "set" if os.environ.get("BRAVE_API_KEY") else "missing")
    print("ANTHROPIC_API_KEY:", "set" if os.environ.get("ANTHROPIC_API_KEY") else "missing")
    print("ELEVENLABS_API_KEY:", "set" if os.environ.get("ELEVENLABS_API_KEY") else "missing")
    print("PEXELS_API_KEY:", "set" if os.environ.get("PEXELS_API_KEY") else "missing")
    print("OPENAI_API_KEY:", "set" if os.environ.get("OPENAI_API_KEY") else "missing")
    print("AZURE_SPEECH_KEY:", "set" if os.environ.get("AZURE_SPEECH_KEY") else "missing")
    print("FAL_KEY:", "set" if (_env_on("FAL_KEY") or os.environ.get("FAL_AI_API_KEY")) else "missing")
    print("HEYGEN_API_KEY:", "set" if os.environ.get("HEYGEN_API_KEY") else "missing")


def _copy_out(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _probe(path: Path) -> dict[str, str]:
    ffprobe = find_ffprobe()
    if not ffprobe or not path.is_file():
        return {}
    raw = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name,avg_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    streams = json.loads(raw).get("streams") or []
    return streams[0] if streams else {}


def _parse_pipelines(raw: str) -> list[str]:
    if raw.strip().lower() in {"all", "safe"}:
        return list(SAFE_PIPELINES) if raw.strip().lower() == "safe" else list(ALL_PIPELINES)
    names = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = [p for p in names if p not in ALL_PIPELINES and p != "auto"]
    if unknown:
        raise SystemExit(f"Unknown pipeline(s): {unknown}\nChoose from: {', '.join(ALL_PIPELINES)}")
    return names


def _job(
    *,
    pipeline: str,
    topic: str,
    args: argparse.Namespace,
    footage_key: str | None,
) -> dict[str, Any]:
    prefs = {
        "topic": topic,
        "audience": args.audience or None,
        "duration_seconds": args.duration,
        "language": args.language,
        "platform_profile": args.platform,
        "style_playbook": args.style,
        "captions": args.captions,
        "music": args.music,
        "real_footage_only": args.real_footage_only,
        "budget_cap_usd": args.budget,
        "delivery_promise": args.delivery,
        "render_runtime": args.render_runtime,
        "talking": args.talking,
        "long_video": args.long_video,
        "composition_mode": args.composition_mode,
    }
    if args.voice:
        prefs["voice_id"] = args.voice
    keys = [footage_key] if footage_key else []
    return {
        "job_id": f"demo-{pipeline}-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        "pipeline": pipeline,
        "review_mode": args.review,
        "prefs": prefs,
        "asset_keys": keys,
    }


def _demo_fail_reason(name: str, result: dict[str, Any], work: Path, job: dict[str, Any]) -> str | None:
    if result.get("status") == "failed":
        return None
    generators = result.get("generators") or []
    if "debug_card" in generators:
        return "debug_card"
    if "remotion_fallback_from_mc" in generators:
        return "remotion_fallback_from_mc"
    manifest_path = work / "project" / "artifacts" / "asset_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for row in manifest.get("assets") or []:
            if row.get("id") == "stock_fallback" or "stock_fallback" in str(row.get("path") or ""):
                return "stock_fallback"
    if "hyperframes" in generators:
        html = work / "project" / "character" / "hyperframes" / "index.html"
        alt = work / "project" / "hyperframes" / "index.html"
        if not html.is_file() and not alt.is_file():
            return "hyperframes_missing_workspace"
    report_path = work / "project" / "artifacts" / "render_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        warns = [str(w) for w in (report.get("warnings") or [])]
        if "sine_mux" in warns:
            return "sine_mux"
    if name in {"animated-explainer", "animation"}:
        comp_mode = resolve_mode(name, job.get("prefs") or {}, None, canned=False)
        if (job.get("prefs") or {}).get("composition_mode"):
            comp_mode = job["prefs"]["composition_mode"]
        if comp_mode == "atelier":
            slug = slug_for(str(job.get("job_id") or ""), str((job.get("prefs") or {}).get("topic") or name))
            tsx = project_path(slug) / "Composition.tsx"
            if not tsx.is_file():
                return "atelier_tsx_missing"
            if is_scaffold(tsx.read_text(encoding="utf-8")):
                return "atelier_scaffold"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fill the Studio form locally and run engine pipelines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=KEYS_TEXT,
    )
    parser.add_argument("--topic", default="How photosynthesis works")
    parser.add_argument("--audience", default="curious adults")
    parser.add_argument("--duration", type=int, default=45, help="5–1200 seconds")
    parser.add_argument("--language", default="en")
    parser.add_argument("--platform", default="16:9", choices=["16:9", "9:16", "1:1", "21:9"])
    parser.add_argument("--style", default="clean-professional")
    parser.add_argument(
        "--voice",
        default=None,
        help="Provider voice id. Omit to use the selected TTS default (do not pass a Piper model name to ElevenLabs).",
    )
    parser.add_argument("--captions", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--music", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--real-footage-only", action="store_true")
    parser.add_argument("--talking", action="store_true")
    parser.add_argument("--long-video", action="store_true")
    parser.add_argument("--budget", type=float, default=2.0)
    parser.add_argument("--delivery", default="mixed", choices=["mixed", "motion_led"])
    parser.add_argument("--render-runtime", default=None, choices=["remotion", "hyperframes", "ffmpeg", "motion_canvas"])
    parser.add_argument("--composition-mode", default=None, choices=["templated", "atelier"])
    parser.add_argument("--review", action="store_true", help="Stop at storyboard (no final.mp4). Kala-shaped.")
    parser.add_argument(
        "--pipeline",
        default="animated-explainer",
        help="One name, comma list, 'safe' (no cinematic/avatar), or 'all'. Default animated-explainer.",
    )
    parser.add_argument("--footage", default="", help="Local video for footage-led pipelines")
    parser.add_argument("--out", default="", help="Output folder (default work/demo/<stamp>)")
    parser.add_argument("--open", action="store_true", help="Open the output folder when done")
    parser.add_argument("--keys", action="store_true", help="Print key requirements and exit")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Check Python, TTS, Node/npx, Remotion, Motion Canvas, ffmpeg. Exit 1 if any miss.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Download ffmpeg if needed and npm install remotion-composer + mc_adapter, then doctor.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env(ROOT)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.keys:
        print(KEYS_TEXT)
        print()
        _print_env()
        return 0

    if args.doctor and not args.setup:
        return 0 if _print_doctor() else 1

    try:
        ensure_demo_machine()
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.setup:
        return 0 if _print_doctor() else 1

    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        raise SystemExit("demo_studio needs GOOGLE_API_KEY (or GEMINI_API_KEY)")
    if not os.environ.get("BRAVE_API_KEY"):
        raise SystemExit("demo_studio needs BRAVE_API_KEY for research web_search")

    pipelines = _parse_pipelines(args.pipeline)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out) if args.out else ROOT / "work" / "demo" / stamp
    out_root.mkdir(parents=True, exist_ok=True)

    user_footage: str | None = None
    if args.footage:
        src = Path(args.footage)
        if not src.is_file():
            raise SystemExit(f"footage not found: {src}")
        user_footage = str(src)

    missing_footage = [p for p in pipelines if p in NEED_FOOTAGE and not user_footage]
    if missing_footage:
        raise SystemExit(f"{', '.join(missing_footage)} require --footage (dummy clips are not allowed)")

    def footage_for(name: str) -> str | None:
        if user_footage:
            return user_footage
        return None

    print("Studio local demo")
    print("  topic:", args.topic)
    print("  mode: real (Gemini + Brave)")
    print("  composition_mode:", args.composition_mode or "pipeline default (atelier for explainer/animation)")
    print("  out:", out_root)
    _print_env()
    print("  pipelines:", ", ".join(pipelines))
    print()

    rows: list[dict[str, Any]] = []
    demo_t0 = time.perf_counter()
    for name in pipelines:
        work = out_root / name / "work"
        job = _job(pipeline=name, topic=args.topic, args=args, footage_key=footage_for(name))
        print(f"-> {name} ...", flush=True)
        try:
            result = run_job(job, work_dir=work)
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
        status = result.get("status")
        fail = _demo_fail_reason(name, result, work, job)
        if fail:
            status = "failed"
            result["error"] = fail
            result["status"] = "failed"
        row: dict[str, Any] = {
            "pipeline": name,
            "status": status,
            "error": result.get("error"),
            "compose_strategy": result.get("compose_strategy"),
            "scene_runtimes": result.get("scene_runtimes"),
        }
        final = None
        if result.get("final_mp4"):
            src = Path(result["final_mp4"])
            if src.is_file():
                dest = out_root / name / "final.mp4"
                _copy_out(src, dest)
                final = dest
                row["mp4"] = str(dest)
                row["probe"] = _probe(dest)
        if status == "review":
            board = work / "project" / "artifacts" / "storyboard.json"
            if board.is_file():
                dest = out_root / name / "storyboard.json"
                _copy_out(board, dest)
                row["storyboard"] = str(dest)
        extra = f"  {final}" if final else ""
        err = f"  {result.get('error')}" if status == "failed" and result.get("error") else ""
        print(f"  {status}{extra}{err}", flush=True)
        snap = result.get("telemetry") or {}
        art = work / "project" / "artifacts" / "job_telemetry.json"
        if art.is_file():
            dest = out_root / name / "telemetry.json"
            _copy_out(art, dest)
            row["telemetry_path"] = str(dest)
            if not snap:
                try:
                    snap = json.loads(art.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    snap = {}
        elif snap:
            dest = out_root / name / "telemetry.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
            row["telemetry_path"] = str(dest)
        if snap:
            row["telemetry"] = telemetry.compact(snap)
            print(telemetry.format_report(snap), flush=True)
        rows.append(row)

    summary = {
        "topic": args.topic,
        "canned": False,
        "created_at": stamp,
        "elapsed_seconds": round(time.perf_counter() - demo_t0, 3),
        "results": rows,
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print()
    print("summary:", out_root / "summary.json")
    print(f"total wall: {summary['elapsed_seconds']}s")
    if args.open:
        if sys.platform == "win32":
            os.startfile(out_root)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.check_call(["open", str(out_root)])
        else:
            subprocess.check_call(["xdg-open", str(out_root)])
    failed = [r for r in rows if r["status"] not in {"done", "review"}]
    expected = [
        r
        for r in failed
        if r["pipeline"] in FAIL_WITHOUT_KEYS and r.get("error") == "delivery_promise"
    ]
    unexpected = [r for r in failed if r not in expected]
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
