"""OpenMontage SaaS parity: routing, chapters, EP send-back, playbooks, selectors, publish."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schemas.artifacts import validate_artifact


def test_auto_router_topic_and_footage() -> None:
    from runner import route

    assert route.resolve({"pipeline": "cinematic", "prefs": {}}) == "cinematic"
    assert route.resolve({"pipeline": "auto", "prefs": {"topic": "black holes explainer"}}) == "animated-explainer"
    assert route.resolve({"pipeline": "auto", "prefs": {"topic": "rigged cartoon character"}}) == "character-animation"
    assert route.resolve({"pipeline": "auto", "prefs": {"topic": "product trailer cinematic"}}) == "cinematic"
    job = {
        "pipeline": "auto",
        "prefs": {"topic": "highlight clips from the keynote", "duration_seconds": 600, "long_video": True},
        "asset_urls": ["https://example.com/talk.mp4"],
    }
    assert route.resolve(job) == "clip-factory"
    dub = {
        "pipeline": "auto",
        "prefs": {"topic": "dub this interview to Spanish", "target_locale": "es"},
        "asset_urls": ["https://example.com/talk.mp4"],
    }
    assert route.resolve(dub) == "localization-dub"


def test_chapter_planner_splits_ten_minutes() -> None:
    from runner import chapters

    short = chapters.plan(45, {}, "animated-explainer")
    assert len(short) == 1
    assert short[0]["target_seconds"] == 45
    long = chapters.plan(600, {"long_video": True}, "animated-explainer")
    assert len(long) >= 2
    assert all(ch["target_seconds"] <= chapters.CHAPTER_MAX + 5 for ch in long[:-1])
    assert long[-1]["end_seconds"] == 600
    assert chapters.clamp_duration(9999) == chapters.MAX_DURATION
    assert chapters.should_chapter(600, {}, "animated-explainer") is True
    assert chapters.should_chapter(600, {}, "clip-factory") is False


def test_picture_gaps_send_back_scene_plan() -> None:
    from runner import chapters, ep_loop

    plan = {
        "scenes": [
            {"id": "sc1", "type": "diagram", "start_seconds": 0, "end_seconds": 4, "mermaid": ""},
            {"id": "sc2", "type": "text_card", "start_seconds": 4, "end_seconds": 8},
        ]
    }
    assert chapters.picture_gaps(plan)
    gate = ep_loop.review_stage("scene_plan", plan)
    assert gate["verdict"] == "send_back"
    ok = ep_loop.review_stage(
        "scene_plan",
        {
            "scenes": [
                {
                    "id": "sc1",
                    "type": "diagram",
                    "start_seconds": 0,
                    "end_seconds": 4,
                    "mermaid": "flowchart LR\n  A-->B",
                }
            ]
        },
    )
    assert ok["verdict"] == "pass"


def test_decision_log_lists_both_runtimes() -> None:
    from runner import loop

    log = loop._decision_log(
        "job-1",
        "animated-explainer",
        "atelier",
        docs={"remotion": True, "hyperframes": True},
        selected_runtime="remotion",
        routed_from="auto",
    )
    validate_artifact("decision_log", log)
    cats = {d["category"] for d in log["decisions"]}
    assert "pipeline_selection" in cats
    assert "render_runtime_selection" in cats
    assert "composition_mode" in cats
    runtime = next(d for d in log["decisions"] if d["category"] == "render_runtime_selection")
    ids = {o["option_id"] for o in runtime["options_considered"]}
    assert {"remotion", "hyperframes"} <= ids
    from runner import decisions

    richer = decisions.from_assets(
        log,
        asset_manifest={
            "assets": [
                {"id": "vo", "type": "narration", "path": ".", "source_tool": "tts_selector"},
                {"id": "img", "type": "image", "path": ".", "source_tool": "image_selector"},
            ]
        },
        playbook="clean-professional",
        project_id="job-1",
    )
    validate_artifact("decision_log", richer)


def test_playbook_loader_injects_yaml() -> None:
    from runner import skills

    text = skills.load_playbook("clean-professional")
    assert "Playbook YAML" in text
    assert "clean-professional" in text.lower() or "palette" in text.lower() or "colors" in text.lower()


def test_overlay_forbids_shell_only() -> None:
    text = (ROOT / "skills" / "meta" / "api-llm-runner.md").read_text(encoding="utf-8")
    assert "ignore this file" in text.lower()
    assert "shell" in text.lower()
    assert "director" in text.lower()


def test_api_extra_skills_include_taste_and_reviewer() -> None:
    from runner import loop

    proposal = loop._api_extra_skills("proposal", "animated-explainer")
    assert proposal[0] == "meta/api-llm-runner"
    assert "meta/taste-direction" in proposal
    assert "meta/reviewer" in proposal
    assert "meta/checkpoint-protocol" in proposal
    long = loop._api_extra_skills("script", "animated-explainer", duration=600, long_form=True)
    assert "creative/long-form" in long


def test_image_selector_not_locked_to_pexels() -> None:
    src = (ROOT / "runner" / "assets.py").read_text(encoding="utf-8")
    assert "preferred_provider" not in src or "preferred_provider\": \"pexels\"" not in src.replace(" ", "")
    assert '"pexels", "pixabay", "wikimedia"' not in src


def test_live_agent_does_not_author_shots_or_characters() -> None:
    src = (ROOT / "runner" / "om_agent.py").read_text(encoding="utf-8")
    assert "generate_shots" not in src
    assert "run_character_chain" not in src
    assert "try_real_capture" not in src
    from runner import directors

    assert callable(directors.generate_shots)
    assert callable(directors.run_character_design)
    assert callable(directors.run_character_rig)
    assert callable(directors.try_real_capture)


def test_reference_calls_video_analyzer() -> None:
    src = (ROOT / "runner" / "reference.py").read_text(encoding="utf-8")
    assert "video_analyzer" in src


def test_pipeline_load_rejects_unresolved_auto() -> None:
    import pytest
    from runner import pipeline

    with pytest.raises(ValueError, match="auto"):
        pipeline.load("auto")


def test_ep_send_back_invalidates_downstream() -> None:
    from runner import ep_loop

    assert ep_loop.invalidate_after(["research", "proposal", "script"], "proposal") == ["research"]
    assert ep_loop.max_send_backs({"orchestration": {"max_send_backs": 2}}) == 2


def test_publish_skipped_for_documentary_only() -> None:
    from pathlib import Path
    from runner import publish

    assert publish.run(Path("."), pipeline="documentary-montage", final_mp4=Path("x.mp4"), title="t") is None


PIPELINES = [
    "animated-explainer",
    "animation",
    "character-animation",
    "cinematic",
    "documentary-montage",
    "hybrid",
    "talking-head",
    "screen-demo",
    "avatar-spokesperson",
    "clip-factory",
    "podcast-repurpose",
    "localization-dub",
]


def test_twelve_pipelines_have_ep_yaml() -> None:
    from runner import pipeline

    for name in PIPELINES:
        manifest = pipeline.load(name)
        orch = manifest.get("orchestration") or {}
        assert orch.get("mode") == "executive-producer", name
        assert pipeline.stages(manifest), name
