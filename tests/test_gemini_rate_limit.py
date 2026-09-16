"""Gemini free-tier RPM / TPM / RPD pacing."""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.t += float(seconds)


def _arm(monkeypatch, tmp_path: Path) -> _Clock:
    clock = _Clock()
    monkeypatch.setenv("STUDIO_GEMINI_QUOTA_PATH", str(tmp_path / "quota.json"))
    monkeypatch.delenv("STUDIO_GEMINI_RATE_LIMIT", raising=False)
    from runner import gemini_limits

    gemini_limits.set_clock(now=clock.now, sleep=clock.sleep)
    return clock


def test_flash_family_shares_3_8_limits() -> None:
    from runner.gemini_limits import limits_for

    flash = {"rpm": 5, "tpm": 250_000, "rpd": 20}
    for name in (
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
    ):
        assert limits_for(name) == flash, name
    assert limits_for("gemini-3.5-flash-lite") == {"rpm": 15, "tpm": 250_000, "rpd": 500}


def test_atelier_fallback_chain_starts_at_primary() -> None:
    from runner.gemini_limits import atelier_fallback_chain

    chain = atelier_fallback_chain("gemini-3.8-flash")
    assert chain[0] == "gemini-3.8-flash"
    assert all("lite" not in name for name in chain)
    assert "gemini-3-flash-preview" in chain
    assert "gemini-3-flash" not in chain
    assert "gemini-2.5-flash" not in chain
    mid = atelier_fallback_chain("gemini-3.6-flash")
    assert mid[0] == "gemini-3.6-flash"
    assert "gemini-3.5-flash" not in mid


def test_planner_fallback_chain_starts_at_lite() -> None:
    from runner.gemini_limits import planner_fallback_chain

    chain = planner_fallback_chain("gemini-3.5-flash-lite")
    assert chain[0] == "gemini-3.5-flash-lite"
    assert "gemini-2.0-flash" in chain
    assert "gemini-3.8-flash" not in chain
    off = planner_fallback_chain("gemini-3.8-flash")
    assert off[0] == "gemini-3.8-flash"
    assert "gemini-3.5-flash-lite" in off


def test_gemini3_flash_omits_temperature(monkeypatch, tmp_path: Path) -> None:
    _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    bodies: list[dict] = []

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps({"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        bodies.append(json.loads(req.data.decode()))
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    try:
        flash = GeminiFlash(api_key="k", model="gemini-3.8-flash")
        flash.role = "atelier"
        flash._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        cfg = bodies[-1]["generationConfig"]
        assert "temperature" not in cfg
        assert cfg["thinkingConfig"]["thinkingLevel"] == "low"
        lite = GeminiFlash(api_key="k", model="gemini-3.5-flash-lite")
        lite.role = "planner"
        lite._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert bodies[-1]["generationConfig"]["temperature"] == 0.4
    finally:
        gemini_limits.reset_clock()


def test_atelier_falls_back_when_primary_rpd_is_spent(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    urls: list[str] = []

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps({"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        urls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-3.8-flash")
    client.role = "atelier"
    nxt = gemini_limits.atelier_fallback_chain("gemini-3.8-flash")[1]
    try:
        for _ in range(20):
            gemini_limits.acquire("gemini-3.8-flash", 300)
            clock.t += 61
        client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert any(nxt in url for url in urls)
        assert client.model == nxt
    finally:
        gemini_limits.reset_clock()


def test_atelier_falls_back_on_404(monkeypatch, tmp_path: Path) -> None:
    _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    urls: list[str] = []

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps({"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        urls.append(req.full_url)
        if "gemini-3.8-flash" in req.full_url:
            raise HTTPError("https://example", 404, "Not Found", hdrs=None, fp=BytesIO(b'{"error":"not found"}'))
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-3.8-flash")
    client.role = "atelier"
    nxt = gemini_limits.atelier_fallback_chain("gemini-3.8-flash")[1]
    try:
        client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert "gemini-3.8-flash" in urls[0]
        assert nxt in urls[1]
        assert client.model == nxt
    finally:
        gemini_limits.reset_clock()


def test_gemini_retries_503_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    calls = {"n": 0}

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError("https://example", 503, "Service Unavailable", hdrs=None, fp=BytesIO(b'{"error":"unavailable"}'))
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-3.5-flash-lite")
    try:
        payload = client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert payload["candidates"]
        assert calls["n"] == 2
        assert clock.sleeps
    finally:
        gemini_limits.reset_clock()


def test_atelier_persistent_503_walks_next_flash(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    urls: list[str] = []
    chain = gemini_limits.atelier_fallback_chain("gemini-3.8-flash")
    nxt = chain[1]

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        urls.append(req.full_url)
        if nxt in req.full_url:
            return FakeResp()
        raise HTTPError("https://example", 503, "Service Unavailable", hdrs=None, fp=BytesIO(b'{"error":"unavailable"}'))

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-3.8-flash")
    client.role = "atelier"
    try:
        payload = client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert payload["candidates"]
        assert any("gemini-3.8-flash" in url for url in urls)
        assert any(nxt in url for url in urls)
        assert all("flash-lite" not in url for url in urls)
        assert clock.sleeps
    finally:
        gemini_limits.reset_clock()


def test_planner_does_not_walk_atelier_chain(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.gemini_limits import GeminiRateLimitError
    from runner.llm_gemini import GeminiFlash

    monkeypatch.setattr(
        "runner.llm_gemini.urlopen",
        lambda req, timeout=0: (_ for _ in ()).throw(AssertionError("should not HTTP after RPD")),
    )
    client = GeminiFlash(api_key="k", model="gemini-3.8-flash")
    client.role = "planner"
    try:
        for _ in range(20):
            gemini_limits.acquire("gemini-3.8-flash", 300)
            clock.t += 61
        try:
            client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
            raise AssertionError("expected RPD error")
        except GeminiRateLimitError:
            pass
    finally:
        gemini_limits.reset_clock()
    from runner.gemini_limits import limits_for

    lite = limits_for("models/gemini-3.5-flash-lite")
    assert lite == {"rpm": 15, "tpm": 250_000, "rpd": 500}
    flash = limits_for("gemini-3.8-flash")
    assert flash == {"rpm": 5, "tpm": 250_000, "rpd": 20}
    assert limits_for("gemini-3.6-flash")["rpm"] == 5
    assert limits_for("gemini-3.7-flash")["rpd"] == 20


def test_planner_persistent_503_falls_back_to_next_model(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    urls: list[str] = []

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        urls.append(req.full_url)
        if "gemini-3.5-flash-lite" in req.full_url:
            raise HTTPError(
                "https://example", 503, "Service Unavailable", hdrs=None, fp=BytesIO(b'{"error":"unavailable"}')
            )
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-3.5-flash-lite")
    client.role = "planner"
    try:
        payload = client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert payload["candidates"]
        assert any("gemini-3.5-flash-lite" in url for url in urls)
        assert any("gemini-2.5-flash-lite" in url for url in urls)
        assert client.model == "gemini-2.5-flash-lite"
        assert clock.sleeps
    finally:
        gemini_limits.reset_clock()


def test_planner_lite_pool_503_reaches_flash(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    urls: list[str] = []

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        urls.append(req.full_url)
        if "flash-lite" in req.full_url:
            raise HTTPError(
                "https://example", 503, "Service Unavailable", hdrs=None, fp=BytesIO(b'{"error":"unavailable"}')
            )
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-3.5-flash-lite")
    client.role = "planner"
    try:
        payload = client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert payload["candidates"]
        assert any("gemini-2.0-flash" in url for url in urls)
        assert client.model == "gemini-2.0-flash"
        assert clock.sleeps
    finally:
        gemini_limits.reset_clock()


def test_planner_lite_rpd_does_not_walk_capacity_chain(monkeypatch, tmp_path: Path) -> None:
    _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.gemini_limits import GeminiRateLimitError
    from runner.llm_gemini import GeminiFlash

    gemini_limits.mark_rpd_exhausted("gemini-3.5-flash-lite")
    monkeypatch.setattr(
        "runner.llm_gemini.urlopen",
        lambda req, timeout=0: (_ for _ in ()).throw(AssertionError("should not HTTP after RPD")),
    )
    client = GeminiFlash(api_key="k", model="gemini-3.5-flash-lite")
    client.role = "planner"
    try:
        try:
            client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
            raise AssertionError("expected RPD error")
        except GeminiRateLimitError:
            pass
    finally:
        gemini_limits.reset_clock()


def test_estimate_ignores_image_base64() -> None:
    from runner.gemini_limits import estimate_request_tokens

    body = {
        "system_instruction": {"parts": [{"text": "hello " * 20}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "look"},
                    {"inlineData": {"mimeType": "image/png", "data": "A" * 50_000}},
                ],
            }
        ],
    }
    tokens = estimate_request_tokens(body)
    assert tokens < 5_000
    assert tokens >= 1000


def test_rpm_waits_after_flash_cap(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner.gemini_limits import acquire, reset_clock, snapshot

    try:
        for _ in range(5):
            acquire("gemini-3.8-flash", 300)
        assert clock.sleeps == []
        acquire("gemini-3.8-flash", 300)
        assert sum(clock.sleeps) >= 60
        snap = snapshot("gemini-3.8-flash")
        assert snap["rpm_used"] <= 5
        assert snap["rpd_used"] == 6
    finally:
        reset_clock()


def test_rpd_stops_flash_at_20(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner.gemini_limits import GeminiRateLimitError, acquire, reset_clock

    try:
        for _ in range(20):
            acquire("gemini-3.8-flash", 300)
            clock.t += 61
        try:
            acquire("gemini-3.8-flash", 300)
            raise AssertionError("expected GeminiRateLimitError")
        except GeminiRateLimitError as exc:
            assert "RPD exhausted" in str(exc)
            assert "20/20" in str(exc)
    finally:
        reset_clock()


def test_lite_allows_15_rpm(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner.gemini_limits import acquire, reset_clock

    try:
        for _ in range(15):
            acquire("gemini-3.5-flash-lite", 300)
        assert clock.sleeps == []
        acquire("gemini-3.5-flash-lite", 300)
        assert sum(clock.sleeps) >= 60
    finally:
        reset_clock()


def test_tpm_waits_when_window_is_full(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner.gemini_limits import acquire, reset_clock

    try:
        acquire("gemini-3.5-flash-lite", 200_000)
        acquire("gemini-3.5-flash-lite", 80_000)
        assert sum(clock.sleeps) >= 60
    finally:
        reset_clock()


def test_quota_persists_across_acquire_calls(monkeypatch, tmp_path: Path) -> None:
    _arm(monkeypatch, tmp_path)
    from runner.gemini_limits import acquire, reset_clock, snapshot

    try:
        acquire("gemini-3.8-flash", 300)
        snap = snapshot("gemini-3.8-flash")
        assert snap["rpd_used"] == 1
        data = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
        assert data["models"]["gemini-3.8-flash"]["rpd"] == 1
    finally:
        reset_clock()


def test_disable_skips_waits(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("STUDIO_GEMINI_RATE_LIMIT", "0")
    from runner.gemini_limits import acquire, reset_clock

    try:
        for _ in range(30):
            acquire("gemini-3.8-flash", 300)
        assert clock.sleeps == []
    finally:
        reset_clock()


def test_gemini_post_paces_then_calls(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr("runner.llm_gemini.urlopen", lambda req, timeout=0: FakeResp())
    client = GeminiFlash(api_key="k", model="gemini-3.8-flash")
    try:
        for _ in range(5):
            client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert clock.sleeps == []
        client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert sum(clock.sleeps) >= 60
    finally:
        gemini_limits.reset_clock()


def test_gemini_retries_429_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    clock = _arm(monkeypatch, tmp_path)
    from runner import gemini_limits
    from runner.llm_gemini import GeminiFlash

    calls = {"n": 0}

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError("https://example", 429, "Too Many Requests", hdrs={"Retry-After": "2"}, fp=BytesIO(b'{"error":"rate"}'))
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-3.5-flash-lite")
    try:
        payload = client._post("sys", [{"role": "user", "parts": [{"text": "hi"}]}], [])
        assert payload["candidates"]
        assert calls["n"] == 2
        assert any(s >= 2 for s in clock.sleeps)
    finally:
        gemini_limits.reset_clock()
