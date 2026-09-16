"""SaaS OpenMontage parity contracts vs Cursor OM (same YAML, directors, tools, gates)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
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


def _manifest(name: str) -> dict:
    return yaml.safe_load((ROOT / "pipeline_defs" / f"{name}.yaml").read_text(encoding="utf-8"))


def test_twelve_pipelines_are_executive_producer() -> None:
    from runner import pipeline

    for name in PIPELINES:
        manifest = pipeline.load(name)
        orch = manifest.get("orchestration") or {}
        assert orch.get("mode") == "executive-producer", name
        assert int(orch.get("max_send_backs") or 0) >= 1, name
        stages = [s["name"] for s in manifest["stages"]]
        assert "scene_plan" in stages or name == "framework-smoke", name


def test_hero_pipelines_have_assets_edit_compose_publish() -> None:
    for name in ("animated-explainer", "animation", "character-animation", "cinematic"):
        stages = [s["name"] for s in _manifest(name)["stages"]]
        for required in ("assets", "edit", "compose", "publish"):
            assert required in stages, f"{name} missing {required}"


def test_documentary_has_no_publish_stage() -> None:
    stages = [s["name"] for s in _manifest("documentary-montage")["stages"]]
    assert "publish" not in stages


def test_character_yaml_splits_design_and_rig() -> None:
    stages = [s["name"] for s in _manifest("character-animation")["stages"]]
    assert stages.index("character_design") < stages.index("rig_plan")
    assert stages.index("rig_plan") < stages.index("scene_plan")


def test_ep_loop_covers_character_yaml_stages() -> None:
    from runner import ep_loop

    assert "character_design" in ep_loop.LLM_STAGES
    assert "rig_plan" in ep_loop.LLM_STAGES
    assert "assets" in ep_loop.LLM_STAGES
    assert "compose" in ep_loop.LLM_STAGES
    assert "assets" not in ep_loop.SKIP_LLM
    assert "compose" not in ep_loop.SKIP_LLM


def test_playbook_yaml_reaches_directors() -> None:
    from runner import skills

    text = skills.load_playbook("clean-professional")
    assert "Playbook YAML" in text


def test_overlay_only_forbids_shell() -> None:
    text = (ROOT / "skills" / "meta" / "api-llm-runner.md").read_text(encoding="utf-8")
    assert "ignore this file" in text.lower()
    assert "shell" in text.lower()
    assert "overlay wins" in text.lower()


def test_auto_router_table() -> None:
    from runner import route

    rows = [
        ({"pipeline": "auto", "prefs": {"topic": "explain black holes"}}, "animated-explainer"),
        ({"pipeline": "auto", "prefs": {"topic": "cinematic trailer"}}, "cinematic"),
        ({"pipeline": "auto", "prefs": {"topic": "rigged character walk cycle"}}, "character-animation"),
        (
            {
                "pipeline": "auto",
                "prefs": {"topic": "highlight clips", "duration_seconds": 600},
                "asset_urls": ["https://example.com/a.mp4"],
            },
            "clip-factory",
        ),
        (
            {
                "pipeline": "auto",
                "prefs": {"topic": "dub to Spanish", "target_locale": "es"},
                "asset_urls": ["https://example.com/a.mp4"],
            },
            "localization-dub",
        ),
    ]
    for job, expected in rows:
        assert route.resolve(job) == expected, job


def test_chapter_planner_ten_minutes() -> None:
    from runner import chapters

    plan = chapters.plan(600, {"long_video": True}, "animated-explainer")
    assert len(plan) >= 2
    assert plan[-1]["end_seconds"] == 600


def test_send_back_invalidates_downstream() -> None:
    from runner import ep_loop

    assert ep_loop.invalidate_after(["research", "proposal", "script"], "proposal") == ["research"]


# Side-by-side quality checklist vs Cursor OpenMontage (same brief + pipeline + duration):
# 1. animated-explainer — research tools, 3 concepts, atelier, MC mermaid, karaoke+music, publish
# 2. animation — animation-mode at proposal, picture loop, selectors not Blender
# 3. character-animation — separate design/rig LLM stages, HyperFrames acting, QA report
# 4. cinematic — video_selector shot loop, CinematicRenderer, not one clip
# 5. documentary-montage — footage-first, clip corpus, end-tag, no publish
# 6. hybrid — idea-first, transcript + scene_detect, source/support EP checks
# 7. talking-head — STT, silence_cutter, face/reframe tools, captions
# 8. screen-demo — synthetic TerminalScene vs real_capture (fail loud if requested)
# 9. avatar-spokesperson — talking_head/lip_sync via selectors, fail without keys
# 10. clip-factory — auto routes from long footage, many short deliverables
# 11. podcast-repurpose — transcribe, audiogram/quote cards, multi-deliverable
# 12. localization-dub — translate + TTS + optional lip_sync per locale
#
# Compare structure (chapters, scene types, tools called), not pixel-identical frames.
