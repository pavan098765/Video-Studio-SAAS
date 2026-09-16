"""Python EDL compiler: scene_plan + assets + research → Explainer cuts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from runner.directors import terminal_steps

TYPE_MAP = {
    "text_card": "callout",
    "talking_head": "text_card",
    "diagram": "picture",
    "map": "picture",
    "etymology": "picture",
    "timeline": "picture",
    "code": "terminal_scene",
    "math": "stat_card",
    "animation": "picture",
    "generated": "text_card",
    "broll": "text_card",
    "screen_recording": "terminal_scene",
    "transition": "text_card",
    "end_tag": "hero_title",
    "character_scene": "picture",
}

PICTURE_SCENE_TYPES = {
    "diagram",
    "map",
    "etymology",
    "timeline",
    "code",
    "math",
    "character_scene",
    "animation",
}

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}


def _num(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def captions_from_timestamps(words: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor_ms = 0.0
    for i, row in enumerate(words or []):
        if not isinstance(row, dict):
            continue
        word = str(row.get("word") or row.get("text") or row.get("token") or "").strip()
        if not word:
            continue
        start = _num(row, "startMs", "start_ms")
        end = _num(row, "endMs", "end_ms")
        if start is None:
            sec = _num(row, "start", "start_seconds", "t")
            start = (sec * 1000.0) if sec is not None else cursor_ms
        if end is None:
            sec = _num(row, "end", "end_seconds")
            end = (sec * 1000.0) if sec is not None else start + 250.0
        start = max(0.0, float(start))
        end = max(start + 40.0, float(end))
        out.append({"word": word, "startMs": start, "endMs": end})
        cursor_ms = end
    return out


def load_word_timestamps(asset_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    meta = asset_manifest.get("metadata") or {}
    path = Path(str(meta.get("timestamps") or ""))
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    words = payload.get("words") if isinstance(payload, dict) else payload
    return words if isinstance(words, list) else []


def _asset_path(assets: list[dict[str, Any]], *, types: set[str], scene_id: str | None = None) -> str:
    shared = {None, "", "all", "*"}
    for row in assets:
        if row.get("type") not in types:
            continue
        asset_sid = row.get("scene_id")
        if scene_id and asset_sid not in {scene_id, *shared}:
            continue
        path = Path(str(row.get("path") or ""))
        if path.is_file():
            return str(path)
    if scene_id:
        return ""
    for row in assets:
        if row.get("type") in types:
            path = Path(str(row.get("path") or ""))
            if path.is_file():
                return str(path)
    return ""


def _chart_from_research(research: dict[str, Any] | None) -> list[dict[str, Any]]:
    points = []
    for i, row in enumerate((research or {}).get("data_points") or []):
        if not isinstance(row, dict):
            continue
        claim = str(row.get("claim") or "")
        nums = re.findall(r"(\d+(?:\.\d+)?)\s*%?", claim)
        value = float(nums[0]) if nums else float(20 + i * 15)
        label = (row.get("source_name") or claim)[:28]
        points.append({"name": label or f"p{i+1}", "value": value})
        if len(points) >= 6:
            break
    return points


def compile_edit(
    *,
    runtime: str,
    family: str,
    composition_mode: str,
    scenes: list[dict[str, Any]],
    asset_manifest: dict[str, Any],
    research: dict[str, Any] | None = None,
    script: dict[str, Any] | None = None,
    topic: str = "",
    playbook: str | None = None,
    canned: bool = False,
) -> dict[str, Any]:
    assets = list(asset_manifest.get("assets") or [])
    captions = captions_from_timestamps(load_word_timestamps(asset_manifest))
    narration = _asset_path(assets, types={"narration", "audio"})
    music = _asset_path(assets, types={"music"})
    chart = _chart_from_research(research)
    points = [p for p in ((research or {}).get("data_points") or []) if isinstance(p, dict)]
    cuts: list[dict[str, Any]] = []
    used_chart = False
    for scene in scenes:
        sid = str(scene.get("id") or f"c{len(cuts)+1}")
        stype = str(scene.get("type") or "text_card").lower()
        start = float(scene.get("start_seconds") or 0)
        end = float(scene.get("end_seconds") or (start + 2))
        desc = str(scene.get("description") or topic or sid)
        cut_type = TYPE_MAP.get(stype, "text_card")
        video = _asset_path(assets, types={"video"}, scene_id=sid)
        image = _asset_path(assets, types={"image", "diagram"}, scene_id=sid)
        mc = _asset_path(assets, types={"animation"}, scene_id=sid)
        picture_clip = mc or video
        source = picture_clip or image or "generated"
        cut: dict[str, Any] = {
            "id": sid,
            "source": source,
            "in_seconds": start,
            "out_seconds": end,
            "layer": "primary",
            "type": cut_type,
            "text": desc[:180],
            "title": (topic or desc)[:80],
            "reason": desc[:200],
        }
        if stype in PICTURE_SCENE_TYPES:
            if picture_clip:
                cut["type"] = "picture"
                cut["source"] = picture_clip
                cut["text"] = ""
                cut["title"] = ""
                cut.pop("heroSubtitle", None)
            elif stype == "code":
                steps = _real_terminal_steps(script)
                if steps:
                    cut["type"] = "terminal_scene"
                    cut["steps"] = steps
                    cut["text"] = desc[:180]
                else:
                    cut["type"] = "picture"
                    cut["text"] = ""
                    cut["title"] = ""
                    cut["picture_missing"] = True
            elif stype == "math":
                claim = str((points[0].get("claim") if points else "") or "")
                formula = str(scene.get("formula_tex") or "")
                if claim or formula:
                    cut["type"] = "stat_card"
                    cut["stat"] = (claim or formula)[:48]
                    cut["text"] = (claim or formula or desc)[:160]
                else:
                    cut["type"] = "picture"
                    cut["text"] = ""
                    cut["title"] = ""
                    cut["picture_missing"] = True
            else:
                cut["type"] = "picture"
                cut["text"] = ""
                cut["title"] = ""
                if image:
                    cut["source"] = image
                else:
                    cut["picture_missing"] = True
        can_chart = (
            not used_chart
            and len(chart) >= 3
            and not mc
            and not video
            and stype in {"text_card"}
        )
        if can_chart:
            cut["type"] = "bar_chart"
            cut["chartData"] = chart
            cut["xLabel"] = "item"
            cut["yLabel"] = "value"
            used_chart = True
        if stype == "end_tag":
            cut["type"] = "hero_title"
            cut["text"] = desc.upper()[:80]
            cut["heroSubtitle"] = topic[:80]
        if video and stype in {"broll", "talking_head", "generated"}:
            cut["backgroundVideo"] = video
            cut["type"] = "text_card"
        if image and "backgroundImage" not in cut:
            cut["backgroundImage"] = image
        cuts.append(cut)

    if not used_chart and cuts and len(chart) >= 3:
        for cut in cuts:
            if cut.get("backgroundVideo"):
                continue
            if str(cut.get("id") or "").lower() == "end_tag":
                continue
            if cut.get("type") in {"hero_title", "end_tag"}:
                continue
            if cut.get("type") in {"text_card", "callout"}:
                cut["type"] = "bar_chart"
                cut["chartData"] = chart
                break

    audio: dict[str, Any] = {}
    if narration:
        audio["narration"] = {"src": narration, "volume": 1.0, "segments": []}
    if music:
        audio["music"] = {"src": music, "volume": 0.18, "fadeInSeconds": 0.4, "fadeOutSeconds": 0.8}

    edit: dict[str, Any] = {
        "version": "1.0",
        "cuts": cuts,
        "render_runtime": runtime if runtime in {"remotion", "hyperframes", "ffmpeg", "motion_canvas"} else "remotion",
        "renderer_family": family if family in {
            "explainer-data",
            "explainer-teacher",
            "cinematic-trailer",
            "documentary-montage",
            "product-reveal",
            "screen-demo",
            "presenter",
            "animation-first",
        } else "explainer-data",
        "composition_mode": composition_mode if composition_mode in {"templated", "atelier"} else "templated",
        "captions": captions,
        "subtitles": {"enabled": bool(captions), "style": "karaoke"},
        "metadata": {"playbook": playbook or "clean-professional", "topic": topic},
    }
    if audio:
        edit["audio"] = audio
    return edit


def _real_terminal_steps(script: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for section in (script or {}).get("sections") or []:
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        label = str(section.get("label") or section.get("id") or "beat")
        steps.append({"kind": "cmd", "text": f"# {label}", "holdSeconds": 0.25})
        steps.append({"kind": "out", "text": text[:110], "holdSeconds": 0.55})
    return steps


def terminal_steps_from_script(script: dict[str, Any] | None, topic: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for section in (script or {}).get("sections") or []:
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        label = str(section.get("label") or section.get("id") or "beat")
        steps.append({"kind": "cmd", "text": f"# {label}", "holdSeconds": 0.25})
        steps.append({"kind": "out", "text": text[:110], "holdSeconds": 0.55})
    if not steps:
        return terminal_steps(topic)
    steps.append({"kind": "pill", "text": "screen demo", "color": "#22D3EE"})
    return steps


def cinematic_edit(
    *,
    clips: list[Path],
    topic: str,
    duration: int,
    captions: list[dict[str, Any]] | None = None,
    music: str = "",
) -> dict[str, Any]:
    cuts = []
    cursor = 0.0
    for i, clip in enumerate(clips):
        hold = max(2.0, duration / max(1, len(clips)))
        cuts.append(
            {
                "id": f"cin{i+1}",
                "source": str(clip),
                "in_seconds": cursor,
                "out_seconds": cursor + hold,
                "type": "hero_title" if i == 0 else "text_card",
                "text": topic[:80],
                "layer": "primary",
            }
        )
        cursor += hold
    if not cuts:
        cuts.append(
            {
                "id": "cin1",
                "source": "generated",
                "in_seconds": 0,
                "out_seconds": max(4, duration),
                "type": "hero_title",
                "text": topic[:80],
                "layer": "primary",
            }
        )
    edit: dict[str, Any] = {
        "version": "1.0",
        "cuts": cuts,
        "render_runtime": "remotion",
        "renderer_family": "cinematic-trailer",
        "composition_mode": "templated",
        "captions": captions or [],
        "metadata": {"topic": topic},
    }
    if music:
        edit["audio"] = {"music": {"src": music, "volume": 0.35}}
    return edit
