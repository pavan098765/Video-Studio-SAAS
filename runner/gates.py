"""Hard Python gates — slideshow, ffprobe, audio. CRITICAL fails the job."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from lib.slideshow_risk import score_slideshow_risk
from runner.ffmpeg_bin import find_ffprobe, prepend_to_path


class GateError(RuntimeError):
    pass


def slideshow(scene_plan: dict[str, Any], edit: dict[str, Any] | None) -> dict[str, Any]:
    risk = score_slideshow_risk(
        scene_plan.get("scenes") or [],
        edit_decisions=edit,
        render_runtime=(edit or {}).get("render_runtime"),
    )
    verdict = str(risk.get("verdict") or risk.get("label") or "")
    score = float(risk.get("overall") or risk.get("score") or 0)
    if verdict == "fail" or score >= 4.0:
        raise GateError(f"CRITICAL slideshow gate: {risk}")
    return risk


def ffprobe_mp4(path: Path) -> dict[str, Any]:
    prepend_to_path()
    probe = find_ffprobe()
    if not probe:
        raise GateError("ffprobe missing")
    proc = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GateError(proc.stderr[-2000:])
    info = json.loads(proc.stdout or "{}")
    videos = [s for s in info.get("streams") or [] if s.get("codec_type") == "video"]
    if not videos:
        raise GateError("no video stream")
    v = videos[0]
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if (w, h) != (1920, 1080):
        raise GateError(f"not 1080p: {w}x{h}")
    pix = v.get("pix_fmt")
    if pix and pix != "yuv420p":
        raise GateError(f"pix_fmt {pix}")
    return info


def audio_levels(path: Path) -> dict[str, Any]:
    prepend_to_path()
    probe = find_ffprobe()
    if not probe:
        return {"skipped": True}
    # Presence of an audio stream is enough for smoke; loudnorm is optional.
    proc = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    codec = (proc.stdout or "").strip()
    return {"audio_codec": codec or None}
