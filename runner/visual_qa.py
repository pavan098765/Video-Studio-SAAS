"""Per-scene still review. Gemini vision when available; luminance gate otherwise."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from runner.ffmpeg_bin import find_ffmpeg, find_ffprobe, prepend_to_path
from runner.llm_gemini import LLMError, MockGemini, StageModel


SYSTEM = (
    "You are a video QA reviewer. Look at the stills. Return ONE JSON object: "
    '{"pass": bool, "severity": "critical"|"warn"|"ok", "issues": [str], "rewrite_hint": str}. '
    "Critical if: black/empty frame, unreadable type, two circles or a bouncing dot as a diagram, "
    "a title on a dark slide when the scene should be a diagram/map/code/math, or a broken character rig. "
    "Warn if busy or generic. Otherwise pass."
)


def _duration(mp4: Path) -> float:
    probe = find_ffprobe()
    if not probe:
        return 5.0
    proc = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(0.5, float((proc.stdout or "5").strip() or 5))
    except ValueError:
        return 5.0


def extract_stills(mp4: Path, dest_dir: Path, count: int = 3) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    prepend_to_path()
    ff = find_ffmpeg()
    if not ff or not mp4.is_file():
        return []
    dur = _duration(mp4)
    out: list[Path] = []
    percents = [0.1, 0.5, 0.9][: max(1, count)]
    for i, pct in enumerate(percents):
        dest = dest_dir / f"qa_{i:02d}.png"
        subprocess.run(
            [
                ff,
                "-y",
                "-ss",
                f"{max(0.05, dur * pct):.3f}",
                "-i",
                str(mp4),
                "-frames:v",
                "1",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if dest.is_file():
            out.append(dest)
    return out


def luminance_fail(paths: list[Path]) -> bool:
    if not paths:
        return True
    try:
        from PIL import Image
    except ImportError:
        return False
    dark = 0
    for png in paths:
        try:
            im = Image.open(png).convert("L").resize((32, 18))
            pixels = list(im.getdata())
            mean = sum(pixels) / max(1, len(pixels))
            if mean < 6:
                dark += 1
        except Exception:
            dark += 1
    return dark == len(paths)


def _parse_review(blob: str) -> dict[str, Any]:
    text = (blob or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"pass": True, "severity": "ok", "issues": [], "rewrite_hint": "", "qa_status": "skipped"}
    if not isinstance(data, dict):
        return {"pass": True, "severity": "ok", "issues": [], "rewrite_hint": "", "qa_status": "skipped"}
    ok = bool(data.get("pass", True))
    severity = str(data.get("severity") or ("ok" if ok else "critical"))
    if severity not in {"critical", "warn", "ok"}:
        severity = "critical" if not ok else "ok"
    issues = [str(x) for x in (data.get("issues") or []) if x]
    status = "pass" if ok and severity == "ok" else ("warn" if severity == "warn" else "fail")
    if ok and severity == "warn":
        status = "warn"
    if not ok:
        status = "fail"
        severity = "critical" if severity == "ok" else severity
    return {
        "pass": status != "fail",
        "severity": severity,
        "issues": issues,
        "rewrite_hint": str(data.get("rewrite_hint") or ""),
        "qa_status": status,
    }


def review_scene(
    *,
    mp4: Path,
    dest_dir: Path,
    scene: dict[str, Any],
    model: StageModel | None,
    canned: bool,
    delivery_promise: str = "mixed",
) -> dict[str, Any]:
    sid = str(scene.get("id") or dest_dir.name)
    stills = extract_stills(mp4, dest_dir)
    still_paths = [str(p) for p in stills]
    if luminance_fail(stills):
        return {
            "id": sid,
            "pass": False,
            "severity": "critical",
            "qa_status": "fail",
            "issues": ["black or empty frames"],
            "rewrite_hint": "Replace the empty/black composition with visible type, motion, and contrast.",
            "stills": still_paths,
        }
    stype = str(scene.get("type") or "").lower()
    if (
        delivery_promise == "motion_led"
        and stype in {"screen_recording"}
        and "gui" in str(scene.get("description") or "").lower()
    ):
        # Still-led GUI with a motion-led promise is at least a warning; vision may upgrade.
        pass
    if canned or model is None or isinstance(model, MockGemini) or not hasattr(model, "generate_vision"):
        status = "skipped" if canned or model is None else "pass"
        issues: list[str] = []
        if (
            delivery_promise == "motion_led"
            and stype in {"screen_recording"}
            and "gui" in str(scene.get("description") or "").lower()
        ):
            status = "fail"
            issues = ["GUI screen-demo looks still-led under a motion_led delivery promise"]
        return {
            "id": sid,
            "pass": status != "fail",
            "severity": "critical" if status == "fail" else "ok",
            "qa_status": status,
            "issues": issues,
            "rewrite_hint": "Capture or synthesize actual UI motion, not a still." if status == "fail" else "",
            "stills": still_paths,
        }
    prompt = json.dumps(
        {
            "scene_id": sid,
            "type": scene.get("type"),
            "description": scene.get("description"),
            "delivery_promise": delivery_promise,
        },
        default=str,
    )
    try:
        turn = model.generate_vision(system=SYSTEM, user=prompt, images=stills)  # type: ignore[attr-defined]
        parsed = _parse_review(turn.text if hasattr(turn, "text") else str(turn))
        if getattr(turn, "artifact", None):
            parsed = _parse_review(json.dumps(turn.artifact))
    except (LLMError, OSError, TypeError):
        parsed = {
            "pass": True,
            "severity": "ok",
            "issues": [],
            "rewrite_hint": "",
            "qa_status": "skipped",
        }
    parsed["id"] = sid
    parsed["stills"] = still_paths
    return parsed


def review_scenes(
    *,
    scenes: list[dict[str, Any]],
    scene_files: list[Path],
    posters_dir: Path,
    model: StageModel | None,
    canned: bool,
    delivery_promise: str = "mixed",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    from runner import progress

    for i, (scene, mp4) in enumerate(zip(scenes, scene_files), start=1):
        sid = str(scene.get("id") or mp4.stem)
        progress.note(f"visual QA {i}/{len(scene_files)} {sid} {mp4.name}", kind="qa")
        rows.append(
            review_scene(
                mp4=mp4,
                dest_dir=posters_dir / sid / "qa",
                scene=scene,
                model=model,
                canned=canned,
                delivery_promise=delivery_promise,
            )
        )
    return {"version": "1.0", "rewrites": 0, "scenes": rows}


def failing_hints(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in report.get("scenes") or []:
        if row.get("qa_status") == "fail" or row.get("severity") == "critical":
            out.append(row)
    return out
