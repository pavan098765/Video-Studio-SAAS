"""Canned artifacts for fixture jobs (no LLM)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runner.artifacts import write_artifact


def explainer_10s(topic: str = "How photosynthesis works") -> dict[str, Any]:
    brief = {
        "version": "1.0",
        "title": topic,
        "hook": "Sunlight becomes sugar.",
        "key_points": ["Light reaction", "Calvin cycle", "Oxygen as a byproduct"],
        "tone": "clear",
        "style": "clean-professional",
        "target_platform": "youtube",
        "target_duration_seconds": 10,
    }
    script = {
        "version": "1.0",
        "title": topic,
        "total_duration_seconds": 10,
        "sections": [
            {
                "id": "s1",
                "text": "Plants catch sunlight and turn it into chemical energy.",
                "start_seconds": 0,
                "end_seconds": 5,
            },
            {
                "id": "s2",
                "text": "That energy stitches carbon into sugar, and oxygen is released.",
                "start_seconds": 5,
                "end_seconds": 10,
            },
        ],
    }
    scene_plan = {
        "version": "1.0",
        "style_playbook": "clean-professional",
        "scenes": [
            {
                "id": "sc1",
                "type": "text_card",
                "description": "Title card introducing photosynthesis",
                "start_seconds": 0,
                "end_seconds": 5,
            },
            {
                "id": "sc2",
                "type": "diagram",
                "description": "Simple chloroplast diagram with labels",
                "start_seconds": 5,
                "end_seconds": 10,
                "mermaid": "flowchart LR\n  Light --> Sugar",
            },
        ],
    }
    assets = {
        "version": "1.0",
        "assets": [
            {
                "id": "a_title",
                "type": "animation",
                "path": "assets/video/title.mp4",
                "source_tool": "video_compose",
                "scene_id": "sc1",
            },
            {
                "id": "a_diagram",
                "type": "diagram",
                "path": "assets/images/diagram.png",
                "source_tool": "diagram_gen",
                "scene_id": "sc2",
            },
        ],
    }
    edit = {
        "version": "1.0",
        "render_runtime": "remotion",
        "cuts": [
            {
                "id": "c1",
                "source": "a_title",
                "in_seconds": 0,
                "out_seconds": 5,
            },
            {
                "id": "c2",
                "source": "a_diagram",
                "in_seconds": 0,
                "out_seconds": 5,
            },
        ],
        "metadata": {
            "renderer_family": "explainer-data",
            "compose_strategy": "single_runtime",
        },
    }
    return {
        "brief": brief,
        "script": script,
        "scene_plan": scene_plan,
        "asset_manifest": assets,
        "edit_decisions": edit,
    }


def documentary_10s(topic: str = "Voyager at Jupiter") -> dict[str, Any]:
    data = explainer_10s(topic)
    data["scene_plan"]["scenes"][1]["type"] = "broll"
    data["scene_plan"]["scenes"][1]["description"] = "Archive footage of Jupiter"
    data["edit_decisions"]["metadata"]["renderer_family"] = "documentary-montage"
    return data


def for_pipeline(pipeline_name: str, topic: str) -> dict[str, Any]:
    if pipeline_name == "documentary-montage":
        data = documentary_10s(topic)
        data["scene_plan"]["scenes"][1]["type"] = "map"
        data["scene_plan"]["scenes"][1]["description"] = "Map of the voyage"
        data["scene_plan"]["scenes"][1]["mermaid"] = "flowchart LR\n  Earth --> Jupiter"
        data["scene_plan"]["scenes"].append(
            {
                "id": "end_tag",
                "type": "end_tag",
                "description": "Documentary end card",
                "start_seconds": 10,
                "end_seconds": 12,
            }
        )
        return data
    data = explainer_10s(topic)
    if pipeline_name in {"animated-explainer", "hybrid"}:
        data["scene_plan"]["scenes"][1]["type"] = "map"
        data["scene_plan"]["scenes"][1]["description"] = "Etymology map of the topic"
        data["scene_plan"]["scenes"][1]["mermaid"] = "flowchart LR\n  Topic --> Map"
    if pipeline_name == "character-animation":
        data["edit_decisions"]["render_runtime"] = "hyperframes"
        data["edit_decisions"]["metadata"]["renderer_family"] = "character-animation"
    if pipeline_name in {"clip-factory", "localization-dub", "podcast-repurpose"}:
        data["edit_decisions"]["render_runtime"] = "ffmpeg"
        data["edit_decisions"]["metadata"]["renderer_family"] = pipeline_name
    if pipeline_name == "talking-head":
        data["edit_decisions"]["metadata"]["renderer_family"] = "talking-head"
    return data


def write_canned(work_dir: Path, bundle: dict[str, Any]) -> None:
    for name, payload in bundle.items():
        write_artifact(work_dir, name, payload)
