"""Job telemetry: token usage, stage spans, demo_studio report."""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_usage_from_payload_gemini_openai_anthropic() -> None:
    from runner.telemetry import usage_from_payload

    gemini = usage_from_payload(
        {
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 20,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 125,
            }
        }
    )
    assert gemini == {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125}

    openai = usage_from_payload({"usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}})
    assert openai == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}

    anthropic = usage_from_payload(
        {"usage": {"input_tokens": 80, "output_tokens": 12, "cache_read_input_tokens": 8}}
    )
    assert anthropic["input_tokens"] == 88
    assert anthropic["output_tokens"] == 12
    assert anthropic["total_tokens"] == 100


def test_span_and_llm_snapshot(tmp_path: Path) -> None:
    from runner import telemetry

    telemetry.attach(job_id="job-1", pipeline="animated-explainer", work_dir=tmp_path)
    telemetry.set_role("planner")
    with telemetry.span("research"):
        telemetry.record_llm(
            provider="gemini",
            model="gemini-3.5-flash-lite",
            purpose="run_stage",
            ok=True,
            elapsed_ms=1200,
            input_tokens=1000,
            output_tokens=50,
            total_tokens=1050,
        )
        telemetry.record_tool(name="web_search", ok=True, elapsed_ms=80, query="photosynthesis")
    telemetry.record_flag("warning", "sine_mux")
    snap = telemetry.current().snapshot()
    path = telemetry.persist(tmp_path)
    telemetry.clear()
    assert snap["llm"]["calls"] == 1
    assert snap["llm"]["input_tokens"] == 1000
    assert snap["llm"]["output_tokens"] == 50
    assert snap["llm"]["by_model"]["gemini:gemini-3.5-flash-lite"]["calls"] == 1
    assert snap["stages"][0]["name"] == "research"
    assert snap["stages"][0]["ok"] is True
    assert snap["tools"]["calls"] == 1
    assert snap["flags"][0]["kind"] == "warning"
    assert path is not None and path.is_file()
    dumped = json.loads(path.read_text(encoding="utf-8"))
    assert dumped["job_id"] == "job-1"
    report = telemetry.format_report(snap)
    assert "in=1000" in report
    assert "stage  research" in report
    compact = telemetry.compact(snap)
    assert "llm_calls" not in compact
    assert compact["llm"]["total_tokens"] == 1050


def test_gemini_records_usage_without_live_api(tmp_path: Path) -> None:
    from runner import telemetry
    from runner.llm_gemini import GeminiFlash

    telemetry.attach(job_id="g", pipeline="p", work_dir=tmp_path)
    client = GeminiFlash(api_key="test-key", model="gemini-3.5-flash-lite")
    client.role = "planner"
    client._post = lambda *args, **kwargs: {
        "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120},
    }
    turn = client.run_stage(system="s", user="u", tools=[], retry=0, max_rounds=1)
    assert turn.artifact == {"ok": True}
    snap = telemetry.current().snapshot()
    telemetry.clear()
    assert snap["llm"]["calls"] == 1
    assert snap["llm"]["input_tokens"] == 100
    assert snap["llm"]["output_tokens"] == 20
    assert snap["llm"]["total_tokens"] == 120
    assert snap["llm_calls"][0]["provider"] == "gemini"
    assert snap["llm_calls"][0]["ok"] is True


def test_openai_compat_records_usage(monkeypatch, tmp_path: Path) -> None:
    from runner import telemetry
    from runner.llm_openai import OpenAIChat

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr("runner.llm_openai.urlopen", lambda req, timeout=0: FakeResp())
    telemetry.attach(job_id="o", pipeline="p", work_dir=tmp_path)
    client = OpenAIChat(api_key="sk", model="glm-4.6", base_url="https://api.z.ai/api/paas/v4")
    client.generate_vision(system="s", user="u", images=[])
    snap = telemetry.current().snapshot()
    telemetry.clear()
    assert snap["llm"]["input_tokens"] == 11
    assert snap["llm_calls"][0]["provider"] == "openai_compat"
    assert snap["llm_calls"][0]["purpose"] == "vision"


def test_llm_http_error_is_recorded(tmp_path: Path) -> None:
    from runner import telemetry
    from runner.llm_gemini import GeminiFlash, LLMError

    telemetry.attach(job_id="e", pipeline="p", work_dir=tmp_path)
    client = GeminiFlash(api_key="test-key", model="gemini-3.5-flash-lite")

    def boom(*_args, **_kwargs):
        raise HTTPError("https://example", 404, "Not Found", hdrs=None, fp=BytesIO(b'{"error":"nope"}'))

    client._post = boom
    try:
        client.run_stage(system="s", user="u", tools=[], retry=0, max_rounds=1)
        raise AssertionError("expected LLMError")
    except LLMError:
        pass
    snap = telemetry.current().snapshot()
    telemetry.clear()
    assert snap["llm"]["errors"] == 1
    assert snap["llm_calls"][0]["ok"] is False
    assert snap["llm_calls"][0]["http_status"] == 404
    assert snap["errors"]


def test_tools_exec_records_unknown_tool() -> None:
    from runner import telemetry, tools_exec

    telemetry.attach(job_id="t", pipeline="p")
    result = tools_exec.execute("not_a_real_studio_tool", {})
    snap = telemetry.current().snapshot()
    telemetry.clear()
    assert result.success is False
    assert snap["tools"]["calls"] == 1
    assert snap["tools"]["errors"] == 1
    assert snap["tool_calls"][0]["name"] == "not_a_real_studio_tool"


def test_demo_studio_writes_telemetry_into_summary_shape() -> None:
    import importlib.util

    from runner.telemetry import compact, format_report

    path = ROOT / "scripts" / "demo_studio.py"
    spec = importlib.util.spec_from_file_location("demo_studio_telemetry", path)
    assert spec is not None and spec.loader is not None
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    assert "ANTHROPIC_API_KEY" in demo.KEYS_TEXT
    snap = {
        "elapsed_seconds": 12.5,
        "llm": {
            "calls": 2,
            "errors": 0,
            "input_tokens": 50,
            "output_tokens": 10,
            "total_tokens": 60,
            "elapsed_ms": 900,
            "by_model": {"gemini:x": {"calls": 2, "input_tokens": 50, "output_tokens": 10, "total_tokens": 60, "elapsed_ms": 900, "errors": 0}},
        },
        "tools": {"calls": 3, "errors": 1, "elapsed_ms": 40},
        "stages": [{"name": "research", "elapsed_ms": 400, "ok": True}],
        "flags": [{"kind": "audio", "message": "sine_mux"}],
        "errors": [],
        "budget": {"budget_spent_usd": 0.12, "budget_total_usd": 15},
        "meta": {"composition_mode": "atelier"},
    }
    text = format_report(snap)
    assert "in=50" in text
    assert "sine_mux" in text
    row = compact(snap)
    assert row["llm"]["calls"] == 2
    assert row["meta"]["composition_mode"] == "atelier"


def test_openai_compat_still_posts_to_base_url(monkeypatch) -> None:
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
    assert getattr(client, "role", "") == "atelier"
