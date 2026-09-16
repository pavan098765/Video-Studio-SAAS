"""List B picture-completeness: per-role LLM, skill packs, vision-patch salvage, EDL."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_mixed_planner_gemini_atelier_anthropic(monkeypatch) -> None:
    monkeypatch.setenv("STUDIO_LLM_PLANNER_PROVIDER", "gemini")
    monkeypatch.setenv("STUDIO_LLM_PLANNER_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("STUDIO_LLM_ATELIER_PROVIDER", "anthropic")
    monkeypatch.setenv("STUDIO_LLM_ATELIER_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    monkeypatch.delenv("STUDIO_LLM", raising=False)
    from runner.llm_anthropic import AnthropicChat
    from runner.llm_gemini import GeminiFlash, default_model

    planner = default_model("default")
    atelier = default_model("atelier")
    assert isinstance(planner, GeminiFlash)
    assert planner.model == "gemini-3.5-flash-lite"
    assert isinstance(atelier, AnthropicChat)
    assert atelier.model == "claude-sonnet-4-5"
    qa = default_model("visual_qa")
    assert isinstance(qa, GeminiFlash)
    assert qa.model == "gemini-3.5-flash-lite"


def test_atelier_model_change_does_not_change_planner(monkeypatch) -> None:
    monkeypatch.setenv("STUDIO_LLM_PLANNER_PROVIDER", "gemini")
    monkeypatch.setenv("STUDIO_LLM_PLANNER_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("STUDIO_LLM_ATELIER_PROVIDER", "openai")
    monkeypatch.setenv("STUDIO_LLM_ATELIER_MODEL", "glm-4.6")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("STUDIO_LLM", raising=False)
    from runner.llm_gemini import GeminiFlash, default_model
    from runner.llm_openai import OpenAIChat

    planner = default_model()
    atelier = default_model("atelier")
    assert isinstance(planner, GeminiFlash)
    assert planner.model == "gemini-3.5-flash-lite"
    assert isinstance(atelier, OpenAIChat)
    assert atelier.model == "glm-4.6"


def test_openai_compat_posts_to_base_url(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        captured["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr("runner.llm_openai.urlopen", fake_urlopen)
    monkeypatch.setenv("STUDIO_LLM_ATELIER_PROVIDER", "openai_compat")
    monkeypatch.setenv("STUDIO_LLM_ATELIER_MODEL", "glm-4.6")
    monkeypatch.setenv("STUDIO_LLM_ATELIER_BASE_URL", "https://api.z.ai/api/paas/v4")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("STUDIO_LLM", raising=False)
    from runner.llm_gemini import default_model

    client = default_model("atelier")
    client.generate_vision(system="s", user="u", images=[])
    assert captured["url"] == "https://api.z.ai/api/paas/v4/chat/completions"
    assert "api.openai.com" not in captured["url"]


def test_no_gemini_model_id_rewrite(monkeypatch) -> None:
    monkeypatch.setenv("STUDIO_LLM_PLANNER_PROVIDER", "openai")
    monkeypatch.setenv("STUDIO_LLM_PLANNER_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("STUDIO_LLM", raising=False)
    from runner.llm_gemini import default_model

    client = default_model()
    assert client.model == "gemini-3.5-flash-lite"


def test_load_complete_never_truncates() -> None:
    from runner import skills

    rel = "skills/meta/reviewer.md"
    blob = skills.load_complete([rel])
    raw = (ROOT / rel).read_text(encoding="utf-8")
    assert "[truncated]" not in blob
    assert raw in blob
    assert len(blob) >= len(raw)


def test_atelier_remotion_pack_is_complete() -> None:
    from runner import skills

    pack = skills.load_picture_pack("remotion")
    bespoke = (ROOT / "skills" / "meta" / "bespoke-composition.md").read_text(encoding="utf-8")
    assert "[truncated]" not in pack
    assert bespoke in pack
    assert len(pack) >= len(bespoke)
    core = (ROOT / "skills" / "core" / "remotion.md").read_text(encoding="utf-8")
    assert core in pack


def test_planner_keeps_reviewer_and_excludes_picture_layer3() -> None:
    from runner import skills

    manifest = {"stages": [{"name": "proposal", "skill": "pipelines/explainer/proposal-director"}]}
    text = skills.load_stage_skill(
        manifest,
        "proposal",
        extra_skills=["meta/bespoke-composition", "meta/reviewer"],
        layer3=["remotion", "remotion-best-practices", "ffmpeg"],
    )
    assert "[truncated]" not in text
    reviewer = (ROOT / "skills" / "meta" / "reviewer.md").read_text(encoding="utf-8")
    bespoke = (ROOT / "skills" / "meta" / "bespoke-composition.md").read_text(encoding="utf-8")
    assert reviewer in text
    assert bespoke in text
    assert "makeScene2D" not in text
    assert "Layer 3 skill: remotion" not in text


def test_picture_packs_are_not_combined() -> None:
    from runner import skills

    rem = skills.load_picture_pack("remotion")
    hf = skills.load_picture_pack("hyperframes")
    assert "bespoke-composition" in rem
    assert "gsap-core" in hf or "gsap" in hf
    assert rem != hf


def test_vision_patch_three_fails_keeps_last(tmp_path: Path) -> None:
    from runner import picture_patch

    orig = tmp_path / "sc1.mp4"
    orig.write_bytes(b"orig")
    calls = {"n": 0}

    def patch(_qa, _stills):
        calls["n"] += 1
        dest = tmp_path / f"round{calls['n']}.mp4"
        dest.write_bytes(f"round{calls['n']}".encode())
        return dest

    def review(_path):
        return {"qa_status": "fail", "rewrite_hint": "again", "stills": []}

    last, qa, n = picture_patch.run_rounds(
        initial_path=orig,
        initial_qa={"qa_status": "fail", "rewrite_hint": "fix", "stills": []},
        patch=patch,
        review=review,
        canned=False,
        rounds=3,
    )
    assert n == 3
    assert calls["n"] == 3
    assert last.read_bytes() == b"round3"
    assert qa["qa_status"] == "fail"


def test_vision_patch_stops_on_pass_round_two(tmp_path: Path) -> None:
    from runner import picture_patch

    orig = tmp_path / "sc1.mp4"
    orig.write_bytes(b"orig")
    calls = {"n": 0}

    def patch(_qa, _stills):
        calls["n"] += 1
        dest = tmp_path / f"round{calls['n']}.mp4"
        dest.write_bytes(b"ok")
        return dest

    def review(_path):
        if calls["n"] < 2:
            return {"qa_status": "fail", "stills": []}
        return {"qa_status": "pass", "stills": []}

    last, qa, n = picture_patch.run_rounds(
        initial_path=orig,
        initial_qa={"qa_status": "fail", "stills": []},
        patch=patch,
        review=review,
        canned=False,
        rounds=3,
    )
    assert n == 2
    assert qa["qa_status"] == "pass"
    assert last.read_bytes() == b"ok"


def test_vision_patch_character_uses_same_helper(tmp_path: Path) -> None:
    from runner import picture_patch

    html_out = tmp_path / "index.html"
    html_out.write_text("before", encoding="utf-8")
    dest = tmp_path / "sc1.mp4"
    dest.write_bytes(b"v1")
    kinds: list[str] = []

    def patch(_qa, _stills):
        kinds.append("hyperframes")
        dest.write_bytes(b"v2")
        return dest

    last, qa, n = picture_patch.run_rounds(
        initial_path=dest,
        initial_qa={"qa_status": "fail", "stills": []},
        patch=patch,
        review=lambda _p: {"qa_status": "warn", "stills": []},
        canned=False,
        rounds=3,
    )
    assert kinds == ["hyperframes"]
    assert n == 1
    assert qa["qa_status"] == "warn"
    assert last.read_bytes() == b"v2"


def test_atelier_rewrite_three_rounds_never_explainer(tmp_path: Path, monkeypatch) -> None:
    from runner import atelier, compose
    from runner.loop import _rewrite_failing_scenes

    called = {"render": 0, "patch": 0}

    def boom(*_a, **_k):
        called["render"] += 1
        raise AssertionError("Explainer must not run for atelier rewrite")

    monkeypatch.setattr(compose, "render_scene", boom)

    orig = tmp_path / "sc1.mp4"
    orig.write_bytes(b"original-scene")

    def fake_rerender(**kwargs):
        called["patch"] += 1
        dest = kwargs["scenes_dir"] / f"{kwargs['scene_id']}.mp4"
        dest.write_bytes(f"p{called['patch']}".encode())
        return dest

    monkeypatch.setattr(atelier, "rerender_scene", fake_rerender)
    monkeypatch.setattr(
        "runner.visual_qa.review_scene",
        lambda **_k: {"qa_status": "fail", "stills": [], "rewrite_hint": "again"},
    )
    files, runtimes = _rewrite_failing_scenes(
        failing=[{"id": "sc1", "rewrite_hint": "fix", "stills": [], "qa_status": "fail"}],
        scenes=[{"id": "sc1", "type": "diagram", "start_seconds": 0, "end_seconds": 4}],
        scene_files=[orig],
        scene_runtimes=[{"scene_id": "sc1", "render_runtime": "remotion", "generator": "atelier"}],
        composition_mode="atelier",
        selection={"atelier_slug": "job", "composition_id": "StudioPiece"},
        client=object(),
        work_dir=tmp_path,
        scenes_dir=tmp_path,
        duration=10,
        prefs={},
        skill_text="pack",
        retries=1,
        playbook=None,
        pipeline_name="animated-explainer",
        topic="x",
        asset_manifest={"assets": []},
        character_workspace=None,
        script=None,
        canned_mode=False,
        edit={"cuts": []},
        qa_model=object(),
    )
    assert called["render"] == 0
    assert called["patch"] == 3
    assert files[0].read_bytes() == b"p3"
    assert runtimes[0]["generator"] == "atelier"


def test_edl_diagram_no_clip_is_not_text_card() -> None:
    from runner.edl import compile_edit

    edit = compile_edit(
        runtime="remotion",
        family="explainer-data",
        composition_mode="templated",
        scenes=[{"id": "sc2", "type": "diagram", "description": "Calvin cycle", "start_seconds": 0, "end_seconds": 5}],
        asset_manifest={"assets": [], "metadata": {}},
        topic="photosynthesis",
    )
    cut = edit["cuts"][0]
    assert cut["type"] != "text_card"
    assert cut["type"] != "hero_title"
    assert cut["type"] == "picture"
    assert cut.get("picture_missing") is True
    assert "Calvin" not in str(cut.get("text") or "")
    from schemas.artifacts import validate_artifact

    validate_artifact("edit_decisions", edit)


def test_edl_end_tag_still_hero_title() -> None:
    from runner.edl import compile_edit

    edit = compile_edit(
        runtime="remotion",
        family="explainer-data",
        composition_mode="templated",
        scenes=[{"id": "end_tag", "type": "end_tag", "description": "Thanks", "start_seconds": 8, "end_seconds": 10}],
        asset_manifest={"assets": [], "metadata": {}},
        topic="x",
    )
    assert edit["cuts"][0]["type"] == "hero_title"


def test_compose_diagram_without_clip_refuses_explainer(tmp_path: Path, monkeypatch) -> None:
    from runner import compose as compose_mod

    called = {"n": 0}

    def fake_remotion(*_a, **_k):
        called["n"] += 1
        raise AssertionError("Explainer must not draw a diagram title")

    monkeypatch.setattr(compose_mod, "_remotion", fake_remotion)
    monkeypatch.setattr(compose_mod, "ensure_audio", lambda *a, **k: None)
    dest = tmp_path / "sc2.mp4"
    raised = False
    try:
        compose_mod.render_scene(
            dest=dest,
            scene={"id": "sc2", "type": "diagram", "description": "Calvin cycle", "start_seconds": 0, "end_seconds": 4},
            runtime="remotion",
            pipeline="animated-explainer",
            topic="photosynthesis",
            asset_manifest={"assets": [], "metadata": {}},
            seconds=4,
            canned=True,
            prefs={},
            edit={
                "cuts": [
                    {
                        "id": "sc2",
                        "type": "picture",
                        "source": "generated",
                        "in_seconds": 0,
                        "out_seconds": 4,
                        "picture_missing": True,
                        "text": "",
                    }
                ]
            },
        )
    except compose_mod.ComposeError as exc:
        raised = True
        assert "refusing Explainer" in str(exc)
    assert raised
    assert called["n"] == 0


def test_edl_diagram_with_still_is_picture_not_title(tmp_path: Path) -> None:
    from runner.edl import compile_edit

    png = tmp_path / "diagram.png"
    png.write_bytes(b"\x89PNG")
    edit = compile_edit(
        runtime="remotion",
        family="explainer-data",
        composition_mode="templated",
        scenes=[{"id": "sc2", "type": "diagram", "description": "Calvin cycle", "start_seconds": 0, "end_seconds": 5}],
        asset_manifest={"assets": [{"id": "d1", "type": "diagram", "path": str(png), "scene_id": "sc2"}], "metadata": {}},
        topic="photosynthesis",
    )
    cut = edit["cuts"][0]
    assert cut["type"] == "picture"
    assert cut["source"] == str(png)
    assert cut.get("text") == ""
    assert not cut.get("picture_missing")


def test_loop_has_no_explainer_fallback_symbol() -> None:
    from pathlib import Path as P

    src = (P(__file__).resolve().parents[1] / "runner" / "loop.py").read_text(encoding="utf-8")
    compose_src = (P(__file__).resolve().parents[1] / "runner" / "compose.py").read_text(encoding="utf-8")
    assert "explainer_fallback_from_mc" not in src
    assert "explainer_fallback_from_mc" not in compose_src


def test_research_stage_loads_api_overlay() -> None:
    from runner import skills

    manifest = {"stages": [{"name": "research", "skill": "pipelines/explainer/research-director"}]}
    text = skills.load_stage_skill(manifest, "research", extra_skills=["meta/api-llm-research"])
    overlay = (ROOT / "skills" / "meta" / "api-llm-research.md").read_text(encoding="utf-8")
    assert overlay in text
    assert "web_search" in overlay
    src = (ROOT / "runner" / "loop.py").read_text(encoding="utf-8")
    ep = (ROOT / "runner" / "ep_loop.py").read_text(encoding="utf-8")
    agent = (ROOT / "runner" / "om_agent.py").read_text(encoding="utf-8")
    assert "meta/api-llm-research" in src or "meta/api-llm-research" in ep
    assert "gather_research" not in agent
    assert "schema_salvage" not in src
    assert "_rewrite_invented_urls" not in src


def test_research_url_gate_fails_on_invented(monkeypatch) -> None:
    from runner import loop, tools_exec
    from runner.llm_gemini import LLMError

    monkeypatch.setattr(tools_exec, "traced_urls", lambda: ["https://brave.example/a"])
    art = {
        "data_points": [
            {
                "claim": "x",
                "source_url": "https://www.nature.com/articles/nature05678",
                "credibility": "secondary_source",
            }
        ]
    }
    try:
        loop._gate_research_urls(art, skip=False)
    except LLMError as exc:
        assert "invented URLs" in str(exc)
    else:
        raise AssertionError("expected LLMError")


def test_research_url_gate_fails_without_traces(monkeypatch) -> None:
    from runner import loop, tools_exec
    from runner.llm_gemini import LLMError

    monkeypatch.setattr(tools_exec, "traced_urls", lambda: [])
    art = {
        "data_points": [
            {"claim": "x", "source_url": "https://www.nature.com/articles/fake", "credibility": "secondary_source"}
        ]
    }
    try:
        loop._gate_research_urls(art, skip=False)
    except LLMError as exc:
        assert "were not produced by web_search" in str(exc)
    else:
        raise AssertionError("expected LLMError")


def test_every_creative_stage_gets_api_overlays() -> None:
    from runner import loop

    for stage in sorted(loop.CREATIVE_STAGES):
        extra = loop._api_extra_skills(stage, "animated-explainer")
        assert extra[0] == "meta/api-llm-runner", stage
    research = loop._api_extra_skills("research", "cinematic")
    assert "meta/api-llm-research" in research
    proposal = loop._api_extra_skills("proposal", "animated-explainer")
    assert "meta/api-llm-proposal" in proposal
    assert "meta/bespoke-composition" in proposal
    assert "meta/reviewer" in proposal
    idea = loop._api_extra_skills("idea", "talking-head")
    assert "meta/api-llm-proposal" in idea
    script = loop._api_extra_skills("script", "animated-explainer")
    assert "meta/api-llm-script" in script
    scene = loop._api_extra_skills("scene_plan", "hybrid")
    assert "meta/api-llm-script" in scene
    for name in ("api-llm-runner", "api-llm-research", "api-llm-proposal", "api-llm-script", "api-llm-atelier"):
        path = ROOT / "skills" / "meta" / f"{name}.md"
        assert path.is_file(), name
        text = path.read_text(encoding="utf-8")
        assert "ignore this file" in text.lower()
        assert "overlay wins" in text.lower() or "wins over" in text.lower()


def test_machine_facts_are_booleans_not_secrets() -> None:
    from runner import loop

    facts = loop._machine_facts(
        "animated-explainer",
        {"render_runtime": "remotion", "renderer_family": "explainer-data"},
        {"remotion": True, "ffmpeg": True, "hyperframes": False, "motion_canvas": True},
    )
    assert facts["locked_render_runtime"] == "remotion"
    assert facts["composition_mode_default"] == "undecided"
    assert set(facts["keys_present"].values()) <= {True, False}


def test_atelier_skill_prepends_api_overlay() -> None:
    from runner import loop

    text = loop._atelier_skill_text("remotion")
    overlay = (ROOT / "skills" / "meta" / "api-llm-atelier.md").read_text(encoding="utf-8")
    assert overlay in text
    from runner import skills

    pack = skills.load_picture_pack("remotion")
    assert "You can and should use TailwindCSS in Remotion" in pack
    src = (ROOT / "runner" / "loop.py").read_text(encoding="utf-8")
    agent = (ROOT / "runner" / "om_agent.py").read_text(encoding="utf-8")
    assert "_atelier_skill_text" in src
    assert "MACHINE_FACTS" in agent


def test_hydrate_research_stamps_corpus_not_invented_urls() -> None:
    from runner.saas_flow import hydrate_research
    from schemas.artifacts import validate_artifact

    corpus = {
        "topic": "photosynthesis",
        "hits": [
            {"id": i, "url": f"https://brave.example/{i}", "title": f"Hit {i}", "snippet": f"Fact {i} about light.", "used_for": "data_points" if i > 1 else "landscape"}
            for i in range(5)
        ],
    }
    art = {
        "data_points": [
            {"claim": "Leaves split water", "source_id": 2, "source_url": "https://www.nature.com/fake"},
        ],
        "angles_discovered": [
            {"name": "Mechanism first", "hook": "Follow the electron.", "type": "evergreen", "why_now": "From hit 2"},
            {"name": "Myth bust", "hook": "Plants do not eat soil.", "type": "contrarian", "why_now": "From hit 0"},
            {"name": "Why now", "hook": "Climate models need the real cycle.", "type": "trending", "why_now": "From hit 1"},
        ],
        "research_summary": "Light-driven water splitting is the core mechanism.",
    }
    out = hydrate_research(art, corpus)
    validate_artifact("research_brief", out)
    assert out["data_points"][0]["source_url"] == "https://brave.example/2"
    assert all(s["url"].startswith("https://brave.example/") for s in out["sources"])
    assert all(s.get("used_for") for s in out["sources"])
    assert len(out["sources"]) >= 5
    remapped = hydrate_research(
        {"data_points": [{"claim": "Leaves split water", "source_url": "https://www.nature.com/fake"}]},
        corpus,
    )
    assert remapped["data_points"][0]["source_url"].startswith("https://brave.example/")
    assert "nature.com" not in remapped["data_points"][0]["source_url"]


def test_research_queries_cover_director_batches() -> None:
    from runner.saas_flow import research_queries

    rows = research_queries("photosynthesis", "animated-explainer")
    assert len(rows) >= 10
    used = {kind for kind, _query in rows}
    assert {"landscape", "trending", "data_points", "audience_insights", "visual"} <= used
    blob = " ".join(q for _kind, q in rows)
    assert "site:youtube.com" in blob
    assert "site:reddit.com" in blob
    assert "statistics" in blob


def test_hydrate_proposal_locks_runtime_and_approval() -> None:
    from jsonschema import ValidationError
    from runner.saas_flow import hydrate_proposal
    from schemas.artifacts import validate_artifact

    empty = hydrate_proposal(
        {"concept_options": [], "approval": {"status": "pending"}},
        pipeline="animated-explainer",
        render_runtime="remotion",
        renderer_family="explainer-data",
        composition_mode="atelier",
        topic="photosynthesis",
        duration_seconds=8,
    )
    try:
        validate_artifact("proposal_packet", empty)
    except ValidationError:
        pass
    else:
        raise AssertionError("empty concepts must not be padded to pass schema")

    concepts = [
        {
            "id": f"c{i}",
            "title": title,
            "hook": hook,
            "narrative_structure": structure,
            "visual_approach": "Bespoke motion graphics of the chloroplast, not title cards",
            "target_duration_seconds": 8,
            "why_this_works": "Cites the research brief mechanism gap.",
            "key_points": ["Light splits water", "Sugar is the output"],
        }
        for i, (title, hook, structure) in enumerate(
            [
                ("The 200ms of a leaf", "Follow one photon into a chloroplast.", "journey"),
                ("Plants do not eat soil", "The usual school diagram is the wrong story.", "myth_busting"),
                ("Why the atmosphere has oxygen", "This cycle is why you can breathe.", "data_narrative"),
            ],
            start=1,
        )
    ]
    out = hydrate_proposal(
        {"concept_options": concepts, "approval": {"status": "pending"}},
        pipeline="animated-explainer",
        render_runtime="remotion",
        renderer_family="explainer-data",
        composition_mode="atelier",
        topic="photosynthesis",
        duration_seconds=8,
    )
    validate_artifact("proposal_packet", out)
    assert out["approval"]["status"] == "approved"
    assert out["production_plan"]["render_runtime"] == "remotion"
    assert out["production_plan"]["composition_mode"] == "atelier"
    assert [c["title"] for c in out["concept_options"]] == [c["title"] for c in concepts]


def _motion_tsx(component: str = "Composition", props: str = "CompositionProps") -> str:
    body = (
        'import { AbsoluteFill, Sequence, interpolate, useCurrentFrame } from "remotion";\n'
        f"export interface {props} {{ title: string }}\n"
        f"export const {component}: React.FC<{props}> = ({{ title }}) => {{\n"
        "  const f = useCurrentFrame();\n"
        "  const o = interpolate(f, [0, 20], [0, 1]);\n"
        "  return (\n"
        '    <AbsoluteFill style={{ background: "#123456", opacity: o }}>\n'
        "      <Sequence from={0}>{title}</Sequence>\n"
        "    </AbsoluteFill>\n"
        "  );\n"
        "};\n"
    )
    return body + "// visual motion " + ("x" * 820) + "\n"


def test_atelier_bind_exports_composition_as_scene() -> None:
    from runner.atelier import bind_atelier_exports

    out = bind_atelier_exports(_motion_tsx(), duration_frames=240)
    assert "export const Scene = Composition" in out
    assert "export type SceneProps = CompositionProps" in out
    assert "durationInFrames: 240" in out
    assert "export const calculateMetadata" in out


def test_atelier_bind_keeps_existing_scene_export() -> None:
    from runner.atelier import bind_atelier_exports

    src = _motion_tsx("Scene", "SceneProps")
    src += (
        "export const calculateMetadata = async () => "
        "({ durationInFrames: 90, fps: 30, width: 1920, height: 1080 });\n"
    )
    out = bind_atelier_exports(src, duration_frames=240)
    assert "export const Scene = Scene" not in out
    assert out.count("export const Scene") == 1
    assert "durationInFrames: 90" in out
    assert "durationInFrames: 240" not in out


def test_write_files_rewrites_root_for_composition_export(tmp_path, monkeypatch) -> None:
    from runner import atelier

    monkeypatch.setattr(atelier, "repo_root", lambda: tmp_path)
    wav = tmp_path / "s1.wav"
    wav.write_bytes(b"RIFF")
    proj = atelier.write_files(
        slug="bind-test",
        composition_id="BindTest",
        composition_tsx=_motion_tsx(),
        art_direction_md="test",
        props={"title": "Hi", "durationInSeconds": 8},
        scene_plan={"scenes": [{"id": "sc1", "end_seconds": 8}]},
        asset_manifest={"assets": [{"path": str(wav)}]},
        root_tsx='import { Composition } from "remotion";\nexport const Root = () => null;\n',
    )
    root = (proj / "Root.tsx").read_text(encoding="utf-8")
    comp = (proj / "Composition.tsx").read_text(encoding="utf-8")
    assert 'id="BindTest"' in root
    assert "component={Scene}" in root
    assert "durationInFrames={240}" in root
    assert "export const Root = () => null" not in root
    assert "export const Scene = Composition" in comp
    assert (proj / "public" / "s1.wav").read_bytes() == b"RIFF"


def test_harden_offthread_video_adds_compositor_props() -> None:
    from runner.atelier import bind_atelier_exports, harden_offthread_video

    tsx = '<OffthreadVideo src={staticFile("clip.mp4")} />'
    out = harden_offthread_video(tsx)
    assert "acceptableTimeShiftInSeconds={1}" in out
    assert "toneMapped={false}" in out
    assert harden_offthread_video(out).count("acceptableTimeShiftInSeconds") == 1
    bound = bind_atelier_exports(_motion_tsx() + "\n" + tsx + "\n", duration_frames=90)
    assert "acceptableTimeShiftInSeconds={1}" in bound
    assert "toneMapped={false}" in bound


def test_stage_insert_video_loop_pads_short_clip(tmp_path) -> None:
    import subprocess

    from runner.atelier import _probe_duration, stage_insert_video
    from runner.ffmpeg_bin import find_ffmpeg, find_ffprobe, prepend_to_path

    prepend_to_path()
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not find_ffprobe():
        return
    src = tmp_path / "short.mp4"
    dest = tmp_path / "long.mp4"
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=64x64:d=0.5:r=30",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    stage_insert_video(src, dest, min_seconds=2.0)
    assert dest.is_file()
    assert dest.stat().st_size > 32
    assert _probe_duration(dest) >= 1.9
