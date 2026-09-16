"""Split long jobs into 2–4 minute chapters. Never dump 10 minutes into one composition."""

from __future__ import annotations

from typing import Any

CHAPTER_MIN = 120
CHAPTER_MAX = 240
LONG_FORM_SECONDS = 480
MAX_DURATION = 1200
MC_TYPES = {"diagram", "map", "etymology", "code", "math", "timeline"}


def clamp_duration(seconds: int) -> int:
    return max(1, min(int(seconds), MAX_DURATION))


def should_chapter(duration: int, prefs: dict[str, Any] | None, pipeline: str) -> bool:
    prefs = prefs or {}
    if pipeline in {"clip-factory", "localization-dub", "podcast-repurpose"}:
        return False
    duration = clamp_duration(duration)
    if prefs.get("long_video"):
        return duration >= CHAPTER_MIN
    return duration >= LONG_FORM_SECONDS


def plan(duration: int, prefs: dict[str, Any] | None = None, pipeline: str = "") -> list[dict[str, Any]]:
    """Return chapter windows covering [0, duration). One chapter when short."""
    prefs = prefs or {}
    duration = clamp_duration(duration)
    if not should_chapter(duration, prefs, pipeline):
        return [
            {
                "id": "ch1",
                "index": 0,
                "start_seconds": 0,
                "end_seconds": duration,
                "target_seconds": duration,
            }
        ]
    n = max(2, (duration + CHAPTER_MAX - 1) // CHAPTER_MAX)
    target = duration / n
    if target < CHAPTER_MIN:
        n = max(2, duration // CHAPTER_MIN) if duration >= CHAPTER_MIN * 2 else 2
        target = duration / n
    chapters: list[dict[str, Any]] = []
    cursor = 0
    for i in range(n):
        end = duration if i == n - 1 else int(round((i + 1) * target))
        if end <= cursor:
            end = min(duration, cursor + CHAPTER_MIN)
        chapters.append(
            {
                "id": f"ch{i + 1}",
                "index": i,
                "start_seconds": cursor,
                "end_seconds": end,
                "target_seconds": max(1, end - cursor),
            }
        )
        cursor = end
    return chapters


def slice_scenes(scenes: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    """Scenes overlapping [start, end). Rebase times to chapter-local 0."""
    out: list[dict[str, Any]] = []
    for scene in scenes:
        s = float(scene.get("start_seconds") or 0)
        e = float(scene.get("end_seconds") or s)
        if e <= start or s >= end:
            continue
        clipped = dict(scene)
        local_start = max(0.0, s - start)
        local_end = max(local_start + 0.5, min(e, end) - start)
        clipped["start_seconds"] = local_start
        clipped["end_seconds"] = local_end
        clipped["chapter_start_seconds"] = start
        out.append(clipped)
    return out


def picture_gaps(scene_plan: dict[str, Any] | None) -> list[str]:
    """Diagram-class scenes that still have no mermaid/code/tex — send-back to scene_plan."""
    issues: list[str] = []
    for scene in (scene_plan or {}).get("scenes") or []:
        stype = str(scene.get("type") or "").lower()
        sid = str(scene.get("id") or "?")
        if stype in {"diagram", "map", "etymology", "timeline"} and not str(scene.get("mermaid") or "").strip():
            issues.append(f"{sid} ({stype}) missing mermaid")
        if stype == "code" and not str(scene.get("code_snippet") or scene.get("code") or "").strip():
            issues.append(f"{sid} (code) missing code_snippet")
        if stype == "math" and not str(scene.get("formula_tex") or scene.get("formula") or "").strip():
            issues.append(f"{sid} (math) missing formula_tex")
    return issues
