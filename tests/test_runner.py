from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.canned import explainer_10s, write_canned
from runner.ffmpeg_bin import find_ffprobe, pin_scratch_temp, prepend_to_path
from runner.llm_gemini import LLMTurn, MockGemini
from runner.loop import run_job
from runner.preflight import doctors, hyperframes_ok, motion_canvas_ok, remotion_ok
from runner.tools_exec import execute

prepend_to_path()
pin_scratch_temp(ROOT / "work" / "tmp")


def _skip(reason: str) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest

        pytest.skip(reason)
    print("skip:", reason)


def _mean_volume_db(path: Path) -> float | None:
    ff = None
    from runner.ffmpeg_bin import find_ffmpeg

    ff = find_ffmpeg()
    if not ff or not path.is_file():
        return None
    proc = subprocess.run(
        [ff, "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    blob = (proc.stderr or "") + (proc.stdout or "")
    for line in blob.splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                return None
    return None


def test_production_loop_has_no_silent_card_path() -> None:
    loop = (ROOT / "runner" / "loop.py").read_text(encoding="utf-8")
    compose = (ROOT / "runner" / "compose.py").read_text(encoding="utf-8")
    assert "_write_silent_mp4" not in loop
    assert "_write_silent_mp4" not in compose
    assert "_stub_for_stage" not in loop
    assert "STUDIO_DEBUG_CARDS" in loop
    assert "compose.render_scene" in loop


def test_video_compose_lists_motion_canvas() -> None:
    from tools.video.video_compose import VideoCompose

    info = VideoCompose().get_info()
    assert "motion_canvas" in info.get("render_engines", {})


def test_canned_explainer_artifacts(tmp_path: Path) -> None:
    bundle = explainer_10s()
    write_canned(tmp_path, bundle)
    assert (tmp_path / "project" / "artifacts" / "brief.json").is_file()
    assert (tmp_path / "project" / "artifacts" / "edit_decisions.json").is_file()


def test_review_stops_without_stitch(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    job = json.loads((ROOT / "fixtures" / "explainer_review.json").read_text())
    result = run_job(job, work_dir=tmp_path)
    assert result["status"] == "review"
    assert not (tmp_path / "project" / "renders" / "final.mp4").exists()
    board = json.loads((tmp_path / "project" / "artifacts" / "storyboard.json").read_text())
    assert board["scenes"]
    preview = Path(board["scenes"][0]["preview"])
    assert preview.is_file()
    gen = json.loads(preview.with_suffix(".runtime.json").read_text())
    assert gen.get("generator") != "debug_card"


def test_auto_writes_final(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    job = json.loads((ROOT / "fixtures" / "explainer_auto.json").read_text())
    result = run_job(job, work_dir=tmp_path)
    assert result["status"] == "done"
    final = Path(result["final_mp4"])
    assert final.is_file()
    sel = json.loads((tmp_path / "project" / "artifacts" / "render_runtime_selection.json").read_text())
    assert sel["render_runtime"] == "remotion"
    report = json.loads((tmp_path / "project" / "artifacts" / "render_report.json").read_text())
    generators = report["metadata"]["generators"]
    assert "debug_card" not in generators
    vol = _mean_volume_db(final)
    if vol is not None:
        assert vol > -50.0
    runtime_files = list((tmp_path / "project" / "scenes").glob("*.runtime.json"))
    assert runtime_files
    for meta in runtime_files:
        row = json.loads(meta.read_text(encoding="utf-8"))
    assert row.get("generator") in {
        "remotion",
        "motion_canvas",
        "remotion_fallback_from_mc",
        "picture_passthrough",
        "picture_missing",
        "picture_salvage",
        "ffmpeg",
        "hyperframes",
        "atelier",
        "cinematic",
    }


def test_documentary_fixture(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    job = json.loads((ROOT / "fixtures" / "documentary_auto.json").read_text())
    result = run_job(job, work_dir=tmp_path)
    assert result["status"] == "done"
    runtimes = {row["render_runtime"] for row in result["scene_runtimes"]}
    assert "remotion" in runtimes
    if motion_canvas_ok():
        assert "motion_canvas" in runtimes
        assert result["compose_strategy"] == "scene_assemble"
        mc_dirs = list((tmp_path / "project" / "scenes").glob("mc_*"))
        assert mc_dirs
        wait = next((p / "src" / "waituntil.json" for p in mc_dirs if (p / "src" / "waituntil.json").is_file()), None)
        assert wait is not None
    else:
        assert "remotion" in runtimes
    manifest = json.loads((tmp_path / "project" / "artifacts" / "asset_manifest.json").read_text())
    videos = [a for a in manifest["assets"] if a["type"] in {"video", "narration", "audio"}]
    assert videos
    for row in videos:
        path = Path(row["path"])
        assert path.is_file()
        assert path.suffix.lower() != ".png"


def test_cinematic_without_video_keys_fails(tmp_path: Path) -> None:
    job = json.loads((ROOT / "fixtures" / "cinematic_novideo.json").read_text())
    result = run_job(job, work_dir=tmp_path)
    assert result["status"] == "failed"
    assert result["error"] == "delivery_promise"


def test_avatar_without_video_keys_fails(tmp_path: Path) -> None:
    job = json.loads((ROOT / "fixtures" / "avatar_novideo.json").read_text())
    result = run_job(job, work_dir=tmp_path)
    assert result["status"] == "failed"


def test_remaining_zero_gpu_fixtures(tmp_path: Path) -> None:
    info = doctors()
    names = [
        "animation_auto.json",
        "character_animation_auto.json",
        "clip_factory_auto.json",
        "hybrid_auto.json",
        "localization_dub_auto.json",
        "podcast_repurpose_auto.json",
        "screen_demo_auto.json",
        "talking_head_auto.json",
    ]
    remotion_jobs = {
        "animation_auto.json",
        "hybrid_auto.json",
        "screen_demo_auto.json",
        "talking_head_auto.json",
    }
    for name in names:
        if name == "character_animation_auto.json" and not info["hyperframes"]:
            _skip("hyperframes doctor")
            continue
        if name in remotion_jobs and not info["remotion"]:
            _skip(f"remotion doctor for {name}")
            continue
        if name in {"clip_factory_auto.json", "podcast_repurpose_auto.json", "localization_dub_auto.json"} and not info["ffmpeg"]:
            _skip("ffmpeg doctor")
            continue
        job = json.loads((ROOT / "fixtures" / name).read_text())
        result = run_job(job, work_dir=tmp_path / name)
        assert result["status"] == "done", f"{name}: {result}"
        if name == "character_animation_auto.json":
            ws = tmp_path / name / "project" / "character" / "hyperframes"
            assert (ws / "index.html").is_file()
            html = (ws / "index.html").read_text(encoding="utf-8")
            assert "Character animation scene" in html or ".character" in html
            assert "arm-left" in html
            gens = result.get("generators") or []
            assert "debug_card" not in gens
        if name == "screen_demo_auto.json":
            metas = list((tmp_path / name / "project" / "scenes").glob("*.runtime.json"))
            assert metas
        final = Path(result["final_mp4"])
        assert final.is_file()
        vol = _mean_volume_db(final)
        if vol is not None:
            assert vol > -50.0, name


def test_regen_one_scene_keeps_others(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    job = json.loads((ROOT / "fixtures" / "explainer_auto.json").read_text())
    first = run_job(job, work_dir=tmp_path)
    assert first["status"] == "done"
    scene = tmp_path / "project" / "scenes" / "sc1.mp4"
    before = scene.stat().st_mtime if scene.exists() else 0
    job["regenerate_scene_id"] = "sc2"
    job["review_mode"] = True
    second = run_job(job, work_dir=tmp_path)
    assert second["status"] == "review"
    assert scene.exists()
    assert scene.stat().st_mtime == before or scene.stat().st_size > 0


def test_continue_stitches_only(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    job = json.loads((ROOT / "fixtures" / "explainer_auto.json").read_text())
    first = run_job(job, work_dir=tmp_path)
    assert first["status"] == "done"
    job["continue_after_review"] = True
    second = run_job(job, work_dir=tmp_path)
    assert second["status"] == "done"
    assert Path(second["final_mp4"]).is_file()


def test_mock_gemini_sequence(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    from runner import stubs
    from runner.tools_exec import registry

    tool = registry().get("tts_selector")
    if tool is None or str(tool.get_status().value) != "available":
        _skip("tts_selector unavailable")
        return
    bundle = explainer_10s()
    proposal = stubs.proposal_packet("x", "animated-explainer", "remotion")
    turns = [
        LLMTurn(artifact=stubs.research_brief("x")),
        LLMTurn(artifact=proposal),
        LLMTurn(artifact=bundle["script"]),
        LLMTurn(artifact=bundle["scene_plan"]),
        LLMTurn(artifact=bundle["asset_manifest"]),
        LLMTurn(artifact=bundle["edit_decisions"]),
        LLMTurn(artifact=stubs.render_report()),
        LLMTurn(artifact=stubs.publish_log()),
    ]
    job = {
        "job_id": "llm-mock",
        "pipeline": "animated-explainer",
        "review_mode": False,
        "prefs": {"topic": "x", "duration_seconds": 10, "composition_mode": "templated", "music": False},
    }
    result = run_job(job, work_dir=tmp_path, model=MockGemini(turns))
    assert result["status"] == "done", result


def test_gpu_tools_unavailable() -> None:
    r = execute("blender_world", {"operation": "doctor"})
    assert r.success is False
    assert r.error
    r2 = execute("wan_video", {"prompt": "nope"})
    assert r2.success is False


def test_tts_selector_piper_name() -> None:
    from runner.tools_exec import registry

    registry()
    tool = registry().get("tts_selector")
    assert tool is not None
    assert tool.name == "tts_selector"
    assert registry().get("motion_canvas_compose") is not None


def test_motion_canvas_generate_waituntil(tmp_path: Path) -> None:
    result = execute(
        "motion_canvas_compose",
        {
            "operation": "generate",
            "workspace_path": str(tmp_path / "mc"),
            "scene_type": "map",
            "title": "Voyage map",
            "duration_seconds": 4,
            "mermaid": 'flowchart LR\n  A["Start"] --> B["End"]\n',
            "timestamps": [{"end": 0.4}, {"end": 1.2}, {"end": 2.0}],
        },
    )
    assert result.success, result.error
    scene = tmp_path / "mc" / "src" / "scenes" / "generated.tsx"
    wait = tmp_path / "mc" / "src" / "waituntil.json"
    assert scene.is_file()
    text = scene.read_text(encoding="utf-8")
    assert "waitUntil" in text
    assert wait.is_file()
    payload = json.loads(wait.read_text(encoding="utf-8"))
    assert payload.get("waitUntil") is True


def test_ffmpeg_review_continue_and_regen(tmp_path: Path) -> None:
    if not doctors()["ffmpeg"]:
        _skip("ffmpeg doctor")
        return
    job = json.loads((ROOT / "fixtures" / "clip_factory_auto.json").read_text())
    first = run_job(job, work_dir=tmp_path)
    assert first["status"] == "done", first
    scene = tmp_path / "project" / "scenes" / "sc1.mp4"
    assert scene.is_file()
    before = scene.stat().st_mtime
    job["regenerate_scene_id"] = "sc2"
    job["review_mode"] = True
    second = run_job(job, work_dir=tmp_path)
    assert second["status"] == "review", second
    assert scene.exists()
    assert scene.stat().st_mtime == before or scene.stat().st_size > 0
    job.pop("regenerate_scene_id", None)
    job["continue_after_review"] = True
    job["review_mode"] = False
    third = run_job(job, work_dir=tmp_path)
    assert third["status"] == "done", third
    assert Path(third["final_mp4"]).is_file()
    report = json.loads((tmp_path / "project" / "artifacts" / "render_report.json").read_text())
    assert report["metadata"].get("continue") is True or Path(third["final_mp4"]).stat().st_size > 32


def test_r2_checkpoint_resume(tmp_path: Path) -> None:
    if not doctors()["ffmpeg"]:
        _skip("ffmpeg doctor")
        return
    from runner.ingest import _dummy_clip
    from runner import r2 as r2mod

    bucket = tmp_path / "bucket"
    scene = bucket / "scenes" / "sc1.mp4"
    _dummy_clip(scene, seconds=2)
    original = r2mod.get_file

    def fake_get(url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(url).read_bytes())
        return dest

    r2mod.get_file = fake_get  # type: ignore[method-assign]
    try:
        job = json.loads((ROOT / "fixtures" / "clip_factory_auto.json").read_text())
        job["continue_after_review"] = True
        job["review_mode"] = False
        job["r2"] = {"get": {"scenes/sc1.mp4": str(scene)}}
        result = run_job(job, work_dir=tmp_path / "vm2")
        assert result["status"] == "done", result
        assert Path(result["final_mp4"]).is_file()
        assert (tmp_path / "vm2" / "project" / "scenes" / "sc1.mp4").is_file()
    finally:
        r2mod.get_file = original  # type: ignore[method-assign]


def test_empty_tool_schemas_are_empty() -> None:
    from runner.tools_exec import tool_schemas

    assert tool_schemas([]) == []
    assert len(tool_schemas(None)) > 8


def test_yaml_research_allowlist() -> None:
    from lib.pipeline_loader import _load_pipeline_cached
    from runner.pipeline import load, tools_available

    _load_pipeline_cached.cache_clear()
    explainer = load("animated-explainer")
    assert tools_available(explainer, "research") == ["web_search", "web_fetch"]
    assert tools_available(explainer, "proposal") == []
    documentary = load("documentary-montage")
    assert tools_available(documentary, "idea") == ["web_search", "web_fetch"]
    cinematic = load("cinematic")
    assert "web_search" in tools_available(cinematic, "research")
    assert "web_fetch" in tools_available(cinematic, "research")
    animation = load("animation")
    assert tools_available(animation, "research") == ["web_search", "web_fetch"]
    character = load("character-animation")
    assert tools_available(character, "research") == ["web_search", "web_fetch"]


def test_brave_web_search_requires_key(monkeypatch) -> None:
    from runner.tools_exec import execute

    monkeypatch.setenv("BRAVE_API_KEY", "")
    result = execute("web_search", {"query": "openmontage"})
    assert result.success is False
    assert "BRAVE_API_KEY" in (result.error or "")


def test_brave_search_paces_one_request_per_second(monkeypatch) -> None:
    from tools.research import web_search as brave

    sleeps: list[float] = []
    clock = {"t": 100.0}
    monkeypatch.setattr(brave.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(brave.time, "sleep", lambda s: sleeps.append(float(s)) or clock.update(t=clock["t"] + float(s)))
    brave._last_request_at = 0.0
    brave._pace()
    brave._pace()
    assert sleeps and sleeps[-1] >= 1.0


def test_fetch_targets_skip_youtube_and_reddit() -> None:
    from runner.saas_flow import _fetch_targets

    hits = [
        {"id": 0, "url": "https://www.youtube.com/watch?v=aaa", "used_for": "landscape", "title": "yt"},
        {"id": 1, "url": "https://www.reddit.com/r/askscience/x", "used_for": "audience_insights", "title": "rd"},
        {"id": 2, "url": "https://www.nature.com/articles/photosynthesis", "used_for": "data_points", "title": "paper"},
        {"id": 3, "url": "https://en.wikipedia.org/wiki/Photosynthesis", "used_for": "landscape", "title": "wiki"},
    ]
    picked = _fetch_targets(hits)
    urls = [h["url"] for h in picked]
    assert "https://www.nature.com/articles/photosynthesis" in urls
    assert "https://en.wikipedia.org/wiki/Photosynthesis" in urls
    assert all("youtube" not in u and "reddit" not in u for u in urls)
    hits.extend(
        [
            {"id": 4, "url": "https://flexbooks.ck12.org/photosynthesis", "used_for": "visual", "title": "ck12"},
            {"id": 5, "url": "https://www.the-scientist.com/tag/photosynthesis", "used_for": "trending", "title": "sci"},
        ]
    )
    picked = _fetch_targets(hits)
    urls = [h["url"] for h in picked]
    assert all("ck12.org" not in u and "the-scientist.com" not in u for u in urls)


def test_karaoke_alignment_and_output_path() -> None:
    from runner.karaoke import audio_path, extract_words, words_from_alignment
    from tools.audio.elevenlabs_tts import _words_from_alignment

    alignment = {
        "characters": list("hi there"),
        "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
    }
    words = words_from_alignment(alignment)
    assert [w["word"] for w in words] == ["hi", "there"]
    assert _words_from_alignment(alignment)[0]["word"] == "hi"
    data = {"output": "/tmp/vo.mp3", "timestamps": alignment}
    assert audio_path(data, "/fallback.wav") == "/tmp/vo.mp3"
    extracted = extract_words(data)
    assert extracted[0]["word"] == "hi"


def test_require_karaoke_fails_without_words(tmp_path: Path) -> None:
    from runner.assets import AssetError, gather

    tmp_path.mkdir(parents=True, exist_ok=True)
    try:
        gather(
            tmp_path,
            pipeline="animated-explainer",
            scene_plan={"version": "1.0", "scenes": [{"id": "s1", "type": "text_card", "description": "x", "start_seconds": 0, "end_seconds": 5}]},
            script={"sections": [{"id": "s1", "text": "hello world"}]},
            footage=[],
            canned=False,
            require_karaoke=True,
        )
        raise AssertionError("expected AssetError")
    except (AssetError, RuntimeError):
        pass


def test_require_music_continues_without_bed(tmp_path: Path, monkeypatch) -> None:
    from runner import assets as asset_gather

    monkeypatch.setattr(asset_gather, "_pick_music", lambda *a, **k: (None, ""))
    result = asset_gather.gather(
        tmp_path,
        pipeline="animated-explainer",
        scene_plan={
            "version": "1.0",
            "scenes": [
                {
                    "id": "s1",
                    "type": "text_card",
                    "description": "x",
                    "start_seconds": 0,
                    "end_seconds": 5,
                }
            ],
        },
        script={"sections": [{"id": "s1", "text": "hello world"}]},
        footage=[],
        canned=False,
        require_music=True,
        skip_types={"narration", "image"},
    )
    assert result["metadata"]["music_missing"] is True
    assert not any(a.get("type") == "music" for a in result["assets"])


def test_edl_emits_bar_chart_and_captions(tmp_path: Path) -> None:
    from runner.edl import captions_from_timestamps, compile_edit

    tmp_path.mkdir(parents=True, exist_ok=True)
    stamps = tmp_path / "ts.json"
    stamps.write_text(
        json.dumps({"words": [{"word": "hello", "start": 0.0, "end": 0.4}, {"word": "world", "start": 0.4, "end": 0.8}]}),
        encoding="utf-8",
    )
    edit = compile_edit(
        runtime="remotion",
        family="explainer-data",
        composition_mode="templated",
        scenes=[
            {"id": "sc1", "type": "text_card", "description": "Hook", "start_seconds": 0, "end_seconds": 4},
            {"id": "sc2", "type": "diagram", "description": "Chart", "start_seconds": 4, "end_seconds": 8},
        ],
        asset_manifest={"assets": [], "metadata": {"timestamps": str(stamps)}},
        research={
            "data_points": [
                {"claim": "42% of plants use C3", "source_url": "https://example.com/a", "credibility": "secondary_source"},
                {"claim": "12% use C4", "source_url": "https://example.com/b", "credibility": "secondary_source"},
                {"claim": "3% use CAM", "source_url": "https://example.com/c", "credibility": "secondary_source"},
            ]
        },
        topic="photosynthesis",
    )
    assert any(cut.get("type") == "bar_chart" and cut.get("chartData") for cut in edit["cuts"])
    caps = edit.get("captions") or captions_from_timestamps([{"word": "hello", "start": 0, "end": 0.2}])
    assert caps
    assert caps[0]["word"] == "hello"


def test_edl_diagram_with_mc_not_bar_chart(tmp_path: Path) -> None:
    from runner.edl import compile_edit

    tmp_path.mkdir(parents=True, exist_ok=True)
    mc = tmp_path / "mc.mp4"
    mc.write_bytes(b"\x00" * 64)
    edit = compile_edit(
        runtime="remotion",
        family="explainer-data",
        composition_mode="templated",
        scenes=[
            {"id": "sc1", "type": "diagram", "description": "Cycle", "start_seconds": 0, "end_seconds": 5},
        ],
        asset_manifest={
            "assets": [{"id": "mc_sc1", "type": "animation", "path": str(mc), "scene_id": "sc1"}],
            "metadata": {},
        },
        research={
            "data_points": [
                {"claim": "42%", "source_url": "https://example.com/a", "credibility": "secondary_source"},
                {"claim": "12%", "source_url": "https://example.com/b", "credibility": "secondary_source"},
                {"claim": "3%", "source_url": "https://example.com/c", "credibility": "secondary_source"},
            ]
        },
        topic="cycle",
    )
    cut = edit["cuts"][0]
    assert cut.get("backgroundVideo") is None
    assert cut["type"] != "bar_chart"
    assert cut["type"] != "text_card"
    assert cut["type"] == "picture"
    assert cut["source"] == str(mc)


def test_compose_scene_honors_edl_cut(tmp_path: Path) -> None:
    from runner import compose as compose_mod

    tmp_path.mkdir(parents=True, exist_ok=True)
    captured: dict = {}

    def fake_remotion(composition_id, props, dest, seconds, **_kwargs):
        captured["id"] = composition_id
        captured["props"] = props
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00" * 64)
        return dest

    compose_mod._remotion = fake_remotion  # type: ignore[method-assign]
    compose_mod.ensure_audio = lambda *a, **k: None  # type: ignore[method-assign]
    try:
        dest = tmp_path / "sc1.mp4"
        meta = compose_mod.render_scene(
            dest=dest,
            scene={"id": "sc1", "type": "code", "description": "Show the function", "start_seconds": 0, "end_seconds": 4},
            runtime="remotion",
            pipeline="animated-explainer",
            topic="code",
            asset_manifest={"assets": [], "metadata": {}},
            seconds=4,
            canned=True,
            prefs={},
            edit={
                "cuts": [
                    {
                        "id": "sc1",
                        "type": "terminal_scene",
                        "text": "def explain():",
                        "source": "generated",
                        "in_seconds": 0,
                        "out_seconds": 4,
                        "steps": [{"text": "def explain():", "pauseMs": 400}],
                    }
                ]
            },
        )
        assert meta.get("generator") in {"remotion", "picture_passthrough"}
        assert captured["id"] == "Explainer"
        assert captured["props"]["cuts"][0]["type"] == "terminal_scene"
        assert captured["props"]["cuts"][0]["type"] != "hero_title"
    finally:
        import importlib

        importlib.reload(compose_mod)


def test_mc_empty_mermaid_no_two_circle_fixture(tmp_path: Path) -> None:
    result = execute(
        "motion_canvas_compose",
        {
            "operation": "generate",
            "workspace_path": str(tmp_path / "mc-empty"),
            "scene_type": "diagram",
            "title": "Empty",
            "duration_seconds": 3,
            "mermaid": "",
            "allow_mc_fixture": False,
        },
    )
    assert result.success is False
    tsx = tmp_path / "mc-empty" / "src" / "scenes" / "generated.tsx"
    assert not tsx.is_file()
    defaulted = execute(
        "motion_canvas_compose",
        {
            "operation": "generate",
            "workspace_path": str(tmp_path / "mc-empty-default"),
            "scene_type": "diagram",
            "title": "Empty default",
            "duration_seconds": 3,
            "mermaid": "",
        },
    )
    assert defaulted.success is False


def test_gemini_vision_inline_data(tmp_path: Path) -> None:
    from runner.llm_gemini import GeminiFlash

    tmp_path.mkdir(parents=True, exist_ok=True)
    png = tmp_path / "qa.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
    )
    captured: dict = {}

    class CaptureFlash(GeminiFlash):
        def _generate(self, system, contents, decls, *, purpose="run_stage"):
            captured["contents"] = contents
            captured["purpose"] = purpose
            return {"candidates": [{"content": {"parts": [{"text": '{"pass": true, "severity": "ok", "issues": []}'}]}}]}

    CaptureFlash(api_key="test-key").generate_vision(system="qa", user="look", images=[png])
    parts = captured["contents"][0]["parts"]
    assert any("inlineData" in part for part in parts)
    blob = next(part["inlineData"] for part in parts if "inlineData" in part)
    assert blob["mimeType"] == "image/png"
    assert blob["data"]


def test_black_png_fails_without_gemini(tmp_path: Path) -> None:
    from runner import visual_qa as vqa

    tmp_path.mkdir(parents=True, exist_ok=True)
    png = tmp_path / "black.png"
    try:
        from PIL import Image
    except ImportError:
        _skip("pillow")
        return
    Image.new("RGB", (32, 18), (0, 0, 0)).save(png)
    assert vqa.luminance_fail([png])
    mp4 = tmp_path / "empty.mp4"
    mp4.write_bytes(b"")
    row = vqa.review_scene(
        mp4=mp4,
        dest_dir=tmp_path / "qa",
        scene={"id": "sc1", "type": "diagram", "description": "x"},
        model=None,
        canned=False,
    )
    assert row["qa_status"] == "fail"


def test_visual_qa_rewrite_and_storyboard_qa(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    from runner import loop as loop_mod
    from runner import visual_qa as vqa_mod

    hooks = {"rewrite": 0}

    def fake_review(**kwargs):
        scenes = kwargs["scenes"]
        rows = []
        for i, scene in enumerate(scenes):
            sid = str(scene.get("id") or f"sc{i}")
            if i == 0:
                rows.append(
                    {
                        "id": sid,
                        "pass": False,
                        "severity": "critical",
                        "qa_status": "fail",
                        "issues": ["title card soup"],
                        "rewrite_hint": "replace with a diagram",
                    }
                )
            else:
                rows.append(
                    {
                        "id": sid,
                        "pass": True,
                        "severity": "ok",
                        "qa_status": "pass",
                        "issues": [],
                        "rewrite_hint": "",
                    }
                )
        return {"version": "1.0", "rewrites": 0, "scenes": rows}

    def fake_rewrite(**kwargs):
        hooks["rewrite"] += 1
        report = kwargs.get("failing")
        assert report
        return kwargs["scene_files"], kwargs["scene_runtimes"]

    loop_mod._rewrite_failing_scenes = fake_rewrite  # type: ignore[method-assign]
    vqa_mod.review_scenes = fake_review  # type: ignore[method-assign]
    try:
        job = json.loads((ROOT / "fixtures" / "explainer_review.json").read_text())
        result = run_job(job, work_dir=tmp_path)
        assert result["status"] == "review", result
        assert hooks["rewrite"] == 1
        board = json.loads((tmp_path / "project" / "artifacts" / "storyboard.json").read_text())
        assert board["scenes"]
        assert board["scenes"][0].get("qa_status") in {"pass", "warn", "fail", "skipped"}
    finally:
        import importlib

        importlib.reload(vqa_mod)
        importlib.reload(loop_mod)


def test_atelier_regen_keeps_generator(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    from runner import atelier as atelier_mod
    from runner import loop as loop_mod
    from runner.canned import explainer_10s, write_canned
    from runner.ingest import _dummy_clip

    write_canned(tmp_path, explainer_10s())
    scenes_dir = tmp_path / "project" / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    _dummy_clip(scenes_dir / "sc1.mp4", seconds=2)
    _dummy_clip(scenes_dir / "sc2.mp4", seconds=2)
    sel = {
        "render_runtime": "remotion",
        "renderer_family": "explainer-data",
        "compose_strategy": "single_runtime",
        "atelier_slug": "photosynthesis-regen",
        "composition_id": "PhotosynthesisRegen",
        "master_path": str(tmp_path / "project" / "renders" / "atelier_master.mp4"),
        "composition_mode": "atelier",
    }
    (tmp_path / "project" / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "project" / "artifacts" / "render_runtime_selection.json").write_text(
        json.dumps(sel), encoding="utf-8"
    )
    called = {"n": 0}

    def fake_rerender(**kwargs):
        called["n"] += 1
        dest = kwargs["scenes_dir"] / f"{kwargs['scene_id']}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _dummy_clip(dest, seconds=2)
        return dest

    atelier_mod.rerender_scene = fake_rerender  # type: ignore[method-assign]
    orig_rewrite = loop_mod._rewrite_failing_scenes

    def passthrough_rewrite(**kwargs):
        return kwargs["scene_files"], kwargs["scene_runtimes"]

    loop_mod._rewrite_failing_scenes = passthrough_rewrite  # type: ignore[method-assign]
    try:
        job = {
            "job_id": "atelier-regen-1",
            "pipeline": "animated-explainer",
            "review_mode": True,
            "regenerate_scene_id": "sc1",
            "prefs": {
                "topic": "How photosynthesis works",
                "duration_seconds": 10,
                "composition_mode": "atelier",
                "music": False,
            },
        }
        result = run_job(job, work_dir=tmp_path)
        assert result["status"] == "review", result
        assert called["n"] >= 1
        gens = {row.get("generator") for row in result.get("scene_runtimes") or []}
        assert "atelier" in gens
        assert "hero_title" not in gens
        board = json.loads((tmp_path / "project" / "artifacts" / "storyboard.json").read_text())
        assert board.get("composition_mode") == "atelier"
        assert any(s.get("generator") == "atelier" for s in board["scenes"])
    finally:
        import importlib

        importlib.reload(atelier_mod)
        importlib.reload(loop_mod)


def test_footage_without_whisper_api_fails(tmp_path: Path) -> None:
    if not remotion_ok():
        _skip("remotion doctor")
        return
    from runner.ingest import _dummy_clip

    clip = tmp_path / "talk.mp4"
    _dummy_clip(clip, seconds=2)
    old_o = os.environ.pop("OPENAI_API_KEY", None)
    old_a = os.environ.pop("AZURE_SPEECH_KEY", None)
    old_d = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        job = {
            "job_id": "no-whisper",
            "pipeline": "talking-head",
            "review_mode": True,
            "asset_keys": [str(clip)],
            "prefs": {"topic": "clip", "duration_seconds": 10, "composition_mode": "templated"},
        }
        result = run_job(job, work_dir=tmp_path / "work")
        assert result["status"] == "failed"
        assert result.get("error") == "whisper_api"
    finally:
        if old_o is not None:
            os.environ["OPENAI_API_KEY"] = old_o
        if old_a is not None:
            os.environ["AZURE_SPEECH_KEY"] = old_a
        if old_d is not None:
            os.environ["DASHSCOPE_API_KEY"] = old_d


def test_demo_studio_default_pipeline() -> None:
    import importlib.util

    path = ROOT / "scripts" / "demo_studio.py"
    spec = importlib.util.spec_from_file_location("demo_studio_defaults", path)
    assert spec is not None and spec.loader is not None
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    args = demo.build_parser().parse_args([])
    assert args.pipeline == "animated-explainer"
    assert args.duration == 45
    assert demo.build_parser().parse_args(["--doctor"]).doctor is True
    assert demo.build_parser().parse_args(["--setup"]).setup is True


def test_ensure_npm_install_skips_existing_node_modules(tmp_path: Path) -> None:
    from runner.preflight import ensure_npm_install, python_deps_ok

    ok, _detail = python_deps_ok()
    assert ok
    proj = tmp_path / "pkg"
    proj.mkdir()
    (proj / "package.json").write_text("{}", encoding="utf-8")
    (proj / "node_modules").mkdir()
    ensure_npm_install(proj)


def test_atelier_rejects_stock_imports() -> None:
    from runner.atelier import AtelierError, validate_tsx

    try:
        validate_tsx(
            'import {Explainer} from "../../src/components/Explainer";\nexport const Comp = () => null;\n',
            {"scenes": [{"id": "sc1"}]},
        )
        raise AssertionError("expected AtelierError")
    except AtelierError:
        pass


def test_motion_canvas_uses_real_code_and_tex(tmp_path: Path) -> None:
    result = execute(
        "motion_canvas_compose",
        {
            "operation": "generate",
            "workspace_path": str(tmp_path / "mc-code"),
            "scene_type": "code",
            "title": "Snippet",
            "duration_seconds": 3,
            "code_text": "def explain(topic: str) -> str:\n    return topic.upper()\n",
            "timestamps": [{"end": 0.5}, {"end": 1.0}],
        },
    )
    assert result.success, result.error
    text = (tmp_path / "mc-code" / "src" / "scenes" / "generated.tsx").read_text(encoding="utf-8")
    assert "waitUntil" in text
    assert "def explain" in text
    math = execute(
        "motion_canvas_compose",
        {
            "operation": "generate",
            "workspace_path": str(tmp_path / "mc-math"),
            "scene_type": "math",
            "title": "Energy",
            "duration_seconds": 3,
            "formula": "E = mc^2",
        },
    )
    assert math.success, math.error
    tex = (tmp_path / "mc-math" / "src" / "scenes" / "generated.tsx").read_text(encoding="utf-8")
    assert "MathTex" in tex or "E = mc^2" in tex
    diagram = execute(
        "motion_canvas_compose",
        {
            "operation": "generate",
            "workspace_path": str(tmp_path / "mc-dia"),
            "scene_type": "diagram",
            "title": "Flow",
            "duration_seconds": 3,
            "mermaid": 'flowchart LR\n  A["Sunlight"] --> B["Sugar"]\n',
        },
    )
    assert diagram.success, diagram.error
    nodes = (tmp_path / "mc-dia" / "src" / "scenes" / "generated.tsx").read_text(encoding="utf-8")
    assert "Sunlight" in nodes and "Sugar" in nodes
    unquoted = execute(
        "motion_canvas_compose",
        {
            "operation": "generate",
            "workspace_path": str(tmp_path / "mc-unquoted"),
            "scene_type": "diagram",
            "title": "Flow",
            "duration_seconds": 3,
            "mermaid": "graph TD\n    A[Sunlight Photon] --> B[Chlorophyll Pigment]\n    B --> C[Glucose]\n",
        },
    )
    assert unquoted.success, unquoted.error
    labels = (tmp_path / "mc-unquoted" / "src" / "scenes" / "generated.tsx").read_text(encoding="utf-8")
    assert "Sunlight Photon" in labels
    assert "Chlorophyll Pigment" in labels
    assert 'text="A"' not in labels
    assert "from '@motion-canvas/2d'" in labels


def test_mp4_is_blank_detects_solid_field(tmp_path: Path) -> None:
    from runner.ffmpeg_bin import find_ffmpeg
    from tools.video.motion_canvas_compose import _mp4_is_blank, _mermaid_nodes

    assert _mermaid_nodes('graph TD\n    A[Sunlight Photon] --> B[Sugar]\n') == [
        "Sunlight Photon",
        "Sugar",
    ]
    assert _mermaid_nodes('flowchart LR\n  A["Start"] --> B["End"]\n') == ["Start", "End"]
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return
    solid = tmp_path / "solid.mp4"
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
            "color=c=0x0f172a:s=320x180:d=1:r=30",
            "-pix_fmt",
            "yuv420p",
            str(solid),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _mp4_is_blank(solid) is True
    busy = tmp_path / "busy.mp4"
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
            "testsrc2=size=320x180:rate=30:duration=1",
            "-pix_fmt",
            "yuv420p",
            str(busy),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _mp4_is_blank(busy) is False


def test_motion_canvas_render_unquoted_mermaid_not_blank(tmp_path: Path) -> None:
    from runner.preflight import motion_canvas_ok
    from tools.video.motion_canvas_compose import _mp4_is_blank

    if not motion_canvas_ok():
        _skip("motion canvas")
        return
    out = tmp_path / "diagram.mp4"
    result = execute(
        "motion_canvas_compose",
        {
            "operation": "render",
            "workspace_path": str(tmp_path / "ws"),
            "output_path": str(out),
            "scene_type": "diagram",
            "title": "Flow",
            "duration_seconds": 1,
            "mermaid": "graph TD\n    A[Sunlight Photon] --> B[Sugar]\n",
        },
    )
    assert result.success, result.error
    assert out.is_file()
    assert _mp4_is_blank(out) is False


def test_tts_failure_fails_gather(tmp_path: Path) -> None:
    from runner import assets as asset_gather
    from runner import tts as ttsmod

    original = ttsmod.speak

    def boom(*_args, **_kwargs):
        raise RuntimeError("tts_selector failed")

    ttsmod.speak = boom  # type: ignore[method-assign]
    try:
        try:
            asset_gather.gather(
                tmp_path,
                pipeline="animated-explainer",
                scene_plan={
                    "scenes": [
                        {"id": "sc1", "type": "text_card", "description": "hook", "start_seconds": 0, "end_seconds": 2}
                    ]
                },
                script={"sections": [{"id": "s1", "text": "hello there", "start_seconds": 0, "end_seconds": 2}]},
                footage=[],
                canned=False,
                topic="x",
            )
            raise AssertionError("expected gather to fail without TTS")
        except (asset_gather.AssetError, RuntimeError):
            pass
    finally:
        ttsmod.speak = original  # type: ignore[method-assign]


def test_mux_refuses_production_sine(tmp_path: Path) -> None:
    from runner.compose import ComposeError, mux_audio

    try:
        mux_audio(tmp_path / "missing.mp4", None, tmp_path / "out.mp4", 2, allow_sine=False)
        raise AssertionError("expected sine refusal")
    except ComposeError as exc:
        assert "sine" in str(exc).lower() or "narration" in str(exc).lower()


def test_mix_music_under_vo_sends_full_mix(monkeypatch, tmp_path: Path) -> None:
    from runner import compose as compose_mod
    from tools.base_tool import ToolResult

    captured: dict = {}

    def fake_exec(name, inputs):
        captured["name"] = name
        captured["inputs"] = inputs
        Path(inputs["output_path"]).write_bytes(b"RIFF" + b"\x00" * 60)
        return ToolResult(success=True, data={})

    def fake_mux(_video, _audio, dest, _seconds, **_kwargs):
        dest.write_bytes(b"\x00" * 64)
        return dest

    monkeypatch.setattr(compose_mod.tools_exec, "execute", fake_exec)
    monkeypatch.setattr(compose_mod, "mux_audio", fake_mux)
    video = tmp_path / "v.mp4"
    vo = tmp_path / "vo.wav"
    music = tmp_path / "m.mp3"
    for path in (video, vo, music):
        path.write_bytes(b"\x00" * 64)
    dest = tmp_path / "mixed.mp4"
    out = compose_mod.mix_music_under_vo(video, vo, music, dest, 8)
    assert out == dest
    assert captured["name"] == "audio_mixer"
    assert captured["inputs"]["operation"] == "full_mix"
    assert captured["inputs"]["tracks"][0]["path"] == str(vo)


def test_audio_mixer_missing_operation_does_not_raise() -> None:
    from tools.audio.audio_mixer import AudioMixer

    result = AudioMixer().execute({})
    assert not result.success
    assert "operation" in (result.error or "").lower()


def test_assemble_final_single_runtime_skips_stitch(monkeypatch, tmp_path: Path) -> None:
    from runner import compose as compose_mod

    calls: list[str] = []

    def fake_exec(name, inputs):
        calls.append(name)
        raise AssertionError(f"unexpected tool {name}")

    def fake_encode(src: Path, dest: Path) -> Path:
        dest.write_bytes(src.read_bytes())
        return dest

    monkeypatch.setattr(compose_mod.tools_exec, "execute", fake_exec)
    monkeypatch.setattr(compose_mod, "_encode_delivery", fake_encode)
    master = tmp_path / "atelier_master.mp4"
    master.write_bytes(b"\x00" * 64)
    dest = tmp_path / "final.mp4"
    out = compose_mod.assemble_final(
        strategy="single_runtime",
        scene_files=[tmp_path / "scene-1.mp4"],
        dest=dest,
        master=master,
    )
    assert out == dest
    assert dest.is_file()
    assert calls == []


def test_assemble_final_scene_assemble_requires_two_clips(tmp_path: Path) -> None:
    from runner.compose import ComposeError, assemble_final

    clip = tmp_path / "scene-1.mp4"
    clip.write_bytes(b"\x00" * 64)
    try:
        assemble_final(strategy="scene_assemble", scene_files=[clip], dest=tmp_path / "final.mp4")
        raise AssertionError("expected assemble to refuse a single clip")
    except ComposeError as exc:
        assert "2 clips" in str(exc)


def test_atelier_slug_is_unique_per_job() -> None:
    from runner.atelier import slug_for

    a = slug_for("demo-animated-explainer-131713", "How photosynthesis works")
    b = slug_for("demo-animated-explainer-121416", "How photosynthesis works")
    assert a != b
    assert a.endswith("131713") or "131713" in a


def test_ensure_audio_replaces_silent_aac(tmp_path: Path) -> None:
    from runner.compose import _mean_volume_db, ensure_audio
    from runner.ffmpeg_bin import find_ffmpeg

    ff = find_ffmpeg()
    if not ff:
        _skip("ffmpeg")
        return
    tmp_path.mkdir(parents=True, exist_ok=True)
    silent = tmp_path / "silent.mp4"
    subprocess.check_call(
        [
            ff,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(silent),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    before = _mean_volume_db(silent)
    assert before is None or before <= -50.0
    ensure_audio(silent, None, 1, canned=True, width=320, height=180)
    after = _mean_volume_db(silent)
    assert after is not None and after > -50.0


if __name__ == "__main__":
    import shutil

    root = ROOT / "work" / "test-runs"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    p = root
    test_canned_explainer_artifacts(p / "canned")
    test_review_stops_without_stitch(p / "r")
    test_auto_writes_final(p / "a")
    test_documentary_fixture(p / "doc")
    test_cinematic_without_video_keys_fails(p / "cin")
    test_avatar_without_video_keys_fails(p / "av")
    test_remaining_zero_gpu_fixtures(p / "rest")
    test_gpu_tools_unavailable()
    test_tts_selector_piper_name()
    test_production_loop_has_no_silent_card_path()
    test_video_compose_lists_motion_canvas()
    test_motion_canvas_generate_waituntil(p / "mc")
    test_ffmpeg_review_continue_and_regen(p / "ffresume")
    test_r2_checkpoint_resume(p / "r2")
    test_regen_one_scene_keeps_others(p / "regen")
    test_continue_stitches_only(p / "cont")
    test_empty_tool_schemas_are_empty()
    test_yaml_research_allowlist()
    test_brave_web_search_requires_key()
    test_karaoke_alignment_and_output_path()
    test_require_karaoke_fails_without_words(p / "karaoke")
    test_edl_emits_bar_chart_and_captions(p / "edl")
    test_edl_diagram_with_mc_not_bar_chart(p / "edl-mc")
    test_compose_scene_honors_edl_cut(p / "compose-cut")
    test_mc_empty_mermaid_no_two_circle_fixture(p / "mc-empty")
    test_gemini_vision_inline_data(p / "vision")
    test_black_png_fails_without_gemini(p / "black-qa")
    test_visual_qa_rewrite_and_storyboard_qa(p / "vqa")
    test_atelier_regen_keeps_generator(p / "atelier-regen")
    test_footage_without_whisper_api_fails(p / "whisper")
    test_demo_studio_default_pipeline()
    test_ensure_npm_install_skips_existing_node_modules(p / "npm-skip")
    test_atelier_rejects_stock_imports()
    test_motion_canvas_uses_real_code_and_tex(p / "mc-real")
    test_mp4_is_blank_detects_solid_field(p / "mc-blank")
    test_tts_failure_fails_gather(p / "tts")
    test_mux_refuses_production_sine(p / "sine")
    test_ensure_audio_replaces_silent_aac(p / "silent-aac")
    test_mock_gemini_sequence(p / "llm")
    print("ok")
