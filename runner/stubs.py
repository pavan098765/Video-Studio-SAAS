"""Schema-valid Python stubs for heavy pre-production artifacts."""

from __future__ import annotations

from datetime import date
from typing import Any


def research_brief(topic: str) -> dict[str, Any]:
    sources = [
        {
            "url": f"https://example.com/source/{i}",
            "title": f"Source {i} on {topic}",
            "used_for": "landscape",
            "reliability": "secondary",
        }
        for i in range(1, 6)
    ]
    return {
        "version": "1.0",
        "topic": topic,
        "research_date": date.today().isoformat(),
        "landscape": {
            "existing_content": [
                {
                    "title": f"Existing take {i} on {topic}",
                    "source": "web",
                    "angle": "overview",
                    "what_it_covers": "High-level summary",
                    "what_it_misses": "Concrete mechanism",
                }
                for i in range(1, 4)
            ],
            "saturated_angles": ["generic overview"],
            "underserved_gaps": ["mechanism-first explanation"],
        },
        "data_points": [
            {
                "claim": f"Claim {i} about {topic}",
                "source_url": f"https://example.com/data/{i}",
                "credibility": "secondary_source",
            }
            for i in range(1, 4)
        ],
        "audience_insights": {
            "common_questions": [
                f"What is {topic}?",
                f"Why does {topic} matter?",
                f"How does {topic} work?",
            ],
            "misconceptions": [{"myth": "It is magic", "reality": "It is a process"}],
            "knowledge_level": "curious beginner",
        },
        "angles_discovered": [
            {
                "name": "Mechanism first",
                "hook": f"{topic} in one chain of cause and effect.",
                "type": "evergreen",
                "why_now": "Viewers want a clear model, not slogans.",
            },
            {
                "name": "Myth bust",
                "hook": f"Most explainers of {topic} skip the hard part.",
                "type": "contrarian",
                "why_now": "Search results are saturated with intros.",
            },
            {
                "name": "Field guide",
                "hook": f"A practical walkthrough of {topic}.",
                "type": "narrative",
                "why_now": "Tutorial demand stays high.",
            },
        ],
        "sources": sources,
        "research_summary": f"SaaS stub research for {topic}; Gemini may replace this on a later pass.",
    }


def proposal_packet(topic: str, pipeline: str, render_runtime: str) -> dict[str, Any]:
    runtime = render_runtime if render_runtime in {"remotion", "hyperframes", "ffmpeg"} else "remotion"
    concepts = []
    structures = ["analogy", "problem_solution", "tutorial"]
    for i, structure in enumerate(structures, start=1):
        concepts.append(
            {
                "id": f"c{i}",
                "title": f"{topic} — concept {i}",
                "hook": f"A concrete hook for {topic}.",
                "narrative_structure": structure,
                "visual_approach": "Templated motion graphics with narration.",
                "target_duration_seconds": 10,
                "why_this_works": "Grounded in the research stub gaps.",
                "key_points": ["Hook", "Mechanism"],
            }
        )
    return {
        "version": "1.0",
        "concept_options": concepts,
        "selected_concept": {"concept_id": "c1", "rationale": "Clearest mechanism-first path for SaaS."},
        "production_plan": {
            "pipeline": pipeline,
            "playbook": "clean-professional",
            "stages": [
                {
                    "stage": "assets",
                    "tools": [{"tool_name": "tts_selector", "role": "narration", "available": True}],
                    "approach": "Python-owned TTS and stock gather",
                }
            ],
            "render_runtime": runtime,
            "renderer_family": "explainer-data",
            "composition_mode": "templated",
        },
        "cost_estimate": {
            "total_estimated_usd": 0,
            "line_items": [{"tool": "tts_selector", "operation": "speak", "estimated_usd": 0}],
            "budget_verdict": "no_budget_set",
        },
        "approval": {"status": "approved"},
    }


def brief(topic: str, duration: int = 10) -> dict[str, Any]:
    return {
        "version": "1.0",
        "title": topic,
        "hook": f"{topic} in plain language.",
        "key_points": ["Hook", "How it works", "Why it matters"],
        "tone": "clear",
        "style": "clean-professional",
        "target_platform": "youtube",
        "target_duration_seconds": duration,
    }


def script(topic: str, duration: int = 10) -> dict[str, Any]:
    mid = max(1, duration // 2)
    return {
        "version": "1.0",
        "title": topic,
        "total_duration_seconds": duration,
        "sections": [
            {
                "id": "s1",
                "text": f"{topic} starts with a simple idea you can hold in one sentence.",
                "start_seconds": 0,
                "end_seconds": mid,
            },
            {
                "id": "s2",
                "text": "Then the pieces lock together, and the result is visible.",
                "start_seconds": mid,
                "end_seconds": duration,
            },
        ],
    }


def default_scenes(pipeline: str, topic: str, duration: int = 10) -> list[dict[str, Any]]:
    mid = max(1, duration // 2)
    if pipeline == "screen-demo":
        return [
            {
                "id": "sc1",
                "type": "screen_recording",
                "description": f"Terminal walkthrough for {topic}",
                "start_seconds": 0,
                "end_seconds": duration,
            }
        ]
    if pipeline == "documentary-montage":
        return [
            {
                "id": "sc1",
                "type": "broll",
                "description": f"Archive footage for {topic}",
                "start_seconds": 0,
                "end_seconds": mid,
            },
            {
                "id": "sc2",
                "type": "map",
                "description": f"Map of {topic}",
                "start_seconds": mid,
                "end_seconds": duration,
                "mermaid": "flowchart LR\n  A --> B",
            },
        ]
    if pipeline == "character-animation":
        return [
            {
                "id": "sc1",
                "type": "character_scene",
                "description": f"Lead character acts {topic}",
                "start_seconds": 0,
                "end_seconds": duration,
            }
        ]
    if pipeline == "talking-head":
        return [
            {
                "id": "sc1",
                "type": "talking_head",
                "description": f"Talking head for {topic}",
                "start_seconds": 0,
                "end_seconds": duration,
            }
        ]
    if pipeline in {"clip-factory", "podcast-repurpose", "localization-dub"}:
        return [
            {
                "id": "sc1",
                "type": "broll",
                "description": f"Source clip for {topic}",
                "start_seconds": 0,
                "end_seconds": duration,
            }
        ]
    scenes = [
        {
            "id": "sc1",
            "type": "text_card",
            "description": f"Title for {topic}",
            "start_seconds": 0,
            "end_seconds": mid,
        },
        {
            "id": "sc2",
            "type": "diagram",
            "description": f"Diagram of {topic}",
            "start_seconds": mid,
            "end_seconds": duration,
            "mermaid": "flowchart LR\n  A --> B",
        },
    ]
    if pipeline in {"animated-explainer", "hybrid"}:
        scenes[1]["type"] = "map"
    return scenes


def render_report(path: str = "renders/final.mp4", duration: int = 10) -> dict[str, Any]:
    return {
        "version": "1.0",
        "outputs": [
            {
                "path": path,
                "format": "mp4",
                "resolution": "1920x1080",
                "duration_seconds": duration,
            }
        ],
    }


def publish_log(path: str = "renders/final.mp4") -> dict[str, Any]:
    from datetime import datetime, timezone

    return {
        "version": "1.0",
        "entries": [
            {
                "platform": "local",
                "status": "exported",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "export_path": path,
            }
        ],
    }


VALID_FAMILIES = {
    "explainer-data",
    "explainer-teacher",
    "cinematic-trailer",
    "documentary-montage",
    "product-reveal",
    "screen-demo",
    "presenter",
    "animation-first",
}


def scene_plan(pipeline: str, topic: str, duration: int = 10) -> dict[str, Any]:
    return {
        "version": "1.0",
        "style_playbook": "clean-professional",
        "scenes": default_scenes(pipeline, topic, duration),
    }


def edit_decisions(
    *,
    runtime: str,
    family: str,
    scenes: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    locked = runtime if runtime in {"remotion", "hyperframes", "ffmpeg", "motion_canvas"} else "remotion"
    cuts = []
    by_scene = {a.get("scene_id"): a.get("id") for a in assets if a.get("id")}
    for i, scene in enumerate(scenes):
        sid = scene.get("id") or f"sc{i+1}"
        dur = max(0.1, float(scene.get("end_seconds") or 1) - float(scene.get("start_seconds") or 0))
        cuts.append(
            {
                "id": f"c{i+1}",
                "source": by_scene.get(sid) or sid,
                "in_seconds": 0,
                "out_seconds": dur,
            }
        )
    payload: dict[str, Any] = {
        "version": "1.0",
        "render_runtime": locked if locked != "motion_canvas" else "remotion",
        "cuts": cuts,
        "metadata": {"renderer_family": family, "locked_runtime": runtime},
    }
    if family in VALID_FAMILIES:
        payload["renderer_family"] = family
    return payload
