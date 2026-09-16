"""List A engine-completeness wiring tests."""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.base_tool import ToolResult


def test_whisper_openai_only_does_not_call_azure(tmp_path: Path, monkeypatch) -> None:
    from runner.directors import transcribe_footage, whisper_api_ok

    called: list[str] = []

    def fake_execute(name: str, inputs: dict) -> ToolResult:
        called.append(name)
        if name == "openai_stt":
            return ToolResult(success=True, data={"text": "hello from whisper", "segments": [{"text": "hello from whisper"}]})
        return ToolResult(success=False, error=f"unexpected {name}")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_ENDPOINT", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr("runner.directors.tools_exec.execute", fake_execute)
    clip = tmp_path / "talk.mp4"
    clip.write_bytes(b"fake")
    assert whisper_api_ok() is True
    data = transcribe_footage(tmp_path, [clip], canned=False)
    assert data is not None
    assert data["text"] == "hello from whisper"
    assert called == ["openai_stt"]


def test_whisper_api_ok_requires_callable_backend(monkeypatch) -> None:
    from runner.directors import whisper_api_ok

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert whisper_api_ok() is False
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-test")
    assert whisper_api_ok() is False
    assert whisper_api_ok(public_audio_url="https://cdn.example/a.mp4") is True


def test_ingest_asset_urls_get_url(tmp_path: Path, monkeypatch) -> None:
    from runner.ingest import ingest

    payload = b"downloaded-bytes"

    class Resp:
        def read(self) -> bytes:
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

    monkeypatch.setattr("runner.ingest.urlopen", lambda *args, **kwargs: Resp())
    files = ingest(
        {
            "pipeline": "talking-head",
            "asset_urls": [{"r2_key": "uploads/a.mp4", "get_url": "https://example.com/a.mp4"}],
        },
        tmp_path,
    )
    assert files and files[0].is_file()
    assert files[0].read_bytes() == payload


def test_studio_job_schema_accepts_runner_fields() -> None:
    import jsonschema

    schema = json.loads((ROOT / "docs" / "contracts" / "studio-job.schema.json").read_text(encoding="utf-8"))
    job = {
        "job_id": "job-1",
        "pipeline": "talking-head",
        "review_mode": True,
        "canned": False,
        "asset_keys": ["/tmp/a.mp4"],
        "reference_url": "https://youtube.com/watch?v=1",
        "prefs": {
            "topic": "clip",
            "duration_seconds": 30,
            "budget_cap_usd": 2.5,
            "reference_url": "https://youtube.com/watch?v=1",
        },
        "asset_urls": [{"r2_key": "a.mp4", "get_url": "https://r2.example/a.mp4"}],
        "r2": {"prefix": "jobs/1", "put_base": "https://put", "get": {"artifacts/x.json": "https://get/x"}},
    }
    jsonschema.validate(instance=job, schema=schema)


def test_atelier_rewrite_never_calls_explainer(tmp_path: Path, monkeypatch) -> None:
    from runner import atelier, compose
    from runner.loop import _rewrite_failing_scenes

    called = {"render": 0}

    def boom(*_args, **_kwargs):
        called["render"] += 1
        raise AssertionError("Explainer must not run for atelier rewrite")

    monkeypatch.setattr(compose, "render_scene", boom)

    def fail_patch(*_args, **_kwargs):
        raise atelier.AtelierError("patch failed")

    monkeypatch.setattr(atelier, "rerender_scene", fail_patch)
    orig = tmp_path / "sc1.mp4"
    orig.write_bytes(b"original-scene")
    files, runtimes = _rewrite_failing_scenes(
        failing=[{"id": "sc1", "rewrite_hint": "fix the diagram"}],
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
        skill_text="",
        retries=1,
        playbook=None,
        pipeline_name="animated-explainer",
        topic="x",
        asset_manifest={"assets": []},
        character_workspace=None,
        script=None,
        canned_mode=True,
        edit={"cuts": []},
    )
    assert called["render"] == 0
    assert files[0] == orig
    assert runtimes[0]["generator"] == "atelier"


def test_templated_regen_empty_manifest_fails(tmp_path: Path) -> None:
    from runner.loop import run_job
    from runner.preflight import remotion_ok

    if not remotion_ok():
        import pytest

        pytest.skip("remotion doctor")
    result = run_job(
        {
            "job_id": "regen-empty",
            "pipeline": "animated-explainer",
            "review_mode": True,
            "canned": True,
            "regenerate_scene_id": "sc1",
            "prefs": {"topic": "x", "duration_seconds": 10, "composition_mode": "templated"},
        },
        work_dir=tmp_path,
    )
    assert result["status"] == "failed"
    assert result.get("error") == "assets"


def test_speak_timestamps_default_false() -> None:
    from runner.tts import speak

    assert inspect.signature(speak).parameters["timestamps"].default is False


def test_edl_end_tag_not_overwritten_by_chart() -> None:
    from runner.edl import compile_edit

    research = {
        "data_points": [
            {"claim": "42% A", "source_url": "https://example.com/a", "credibility": "secondary_source"},
            {"claim": "12% B", "source_url": "https://example.com/b", "credibility": "secondary_source"},
            {"claim": "3% C", "source_url": "https://example.com/c", "credibility": "secondary_source"},
        ]
    }
    edit = compile_edit(
        runtime="remotion",
        family="explainer-data",
        composition_mode="templated",
        scenes=[
            {"id": "end_tag", "type": "end_tag", "description": "Thanks", "start_seconds": 8, "end_seconds": 10},
            {"id": "sc1", "type": "text_card", "description": "Hook", "start_seconds": 0, "end_seconds": 4},
        ],
        asset_manifest={"assets": [], "metadata": {}},
        research=research,
        topic="photosynthesis",
    )
    by_id = {cut["id"]: cut for cut in edit["cuts"]}
    assert by_id["end_tag"]["type"] == "hero_title"
    assert by_id["end_tag"].get("chartData") is None
    assert by_id["sc1"]["type"] == "bar_chart"


def test_budget_cap_blocks_paid_estimate(tmp_path: Path) -> None:
    from runner import budget

    class Fake:
        def estimate_cost(self, inputs):
            return 5.0

    budget.attach({"budget_cap_usd": 0.01}, canned=False, work_dir=tmp_path)
    try:
        raised = False
        try:
            budget.reserve_tool(Fake(), "fake_paid", {})
        except budget.BudgetCapError:
            raised = True
        assert raised
        assert budget.exceeded_message()
    finally:
        budget.clear()


def test_default_model_openai_provider(monkeypatch) -> None:
    monkeypatch.setenv("STUDIO_LLM_PLANNER_PROVIDER", "openai")
    monkeypatch.setenv("STUDIO_LLM_PLANNER_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("STUDIO_LLM", raising=False)
    monkeypatch.delenv("STUDIO_LLM_PROVIDER", raising=False)
    from runner.llm_gemini import default_model
    from runner.llm_openai import OpenAIChat

    client = default_model()
    assert isinstance(client, OpenAIChat)
    assert client.model == "gpt-4o-mini"
