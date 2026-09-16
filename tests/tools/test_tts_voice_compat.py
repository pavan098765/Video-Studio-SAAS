from __future__ import annotations

import base64
from types import SimpleNamespace

from tools.audio.elevenlabs_tts import (
    ElevenLabsTTS,
    _is_library_voice_block,
    _raise_for_elevenlabs,
)
from tools.audio.piper_tts import PiperTTS
from tools.audio.tts_selector import TTSSelector, is_piper_voice


def test_piper_voice_ids_are_detected() -> None:
    assert is_piper_voice("en_US-lessac-medium")
    assert is_piper_voice("en_GB-alba-medium")
    assert not is_piper_voice("Xb7hH8MSUJpSbSDYk0k2")
    assert not is_piper_voice("21m00Tcm4TlvDq8ikWAM")
    assert not is_piper_voice("en-US-JennyNeural")
    assert not is_piper_voice("Rachel")
    assert not is_piper_voice("")


def test_selector_drops_piper_voice_for_elevenlabs() -> None:
    adapted = TTSSelector._adapt_inputs(
        ElevenLabsTTS(),
        {"text": "hello", "voice_id": "en_US-lessac-medium"},
    )
    assert "voice_id" not in adapted
    kept = TTSSelector._adapt_inputs(
        PiperTTS(),
        {"text": "hello", "voice_id": "en_US-lessac-medium"},
    )
    assert kept["voice_id"] == "en_US-lessac-medium"


def test_elevenlabs_replaces_piper_voice_with_default() -> None:
    tool = ElevenLabsTTS()
    assert tool._resolve_voice_id({"voice_id": "en_US-lessac-medium"}) == ElevenLabsTTS.DEFAULT_VOICE_ID
    assert tool._resolve_voice_id({"voice_id": "Xb7hH8MSUJpSbSDYk0k2"}) == "Xb7hH8MSUJpSbSDYk0k2"
    assert tool._resolve_voice_id({}) == ElevenLabsTTS.DEFAULT_VOICE_ID
    assert ElevenLabsTTS.DEFAULT_VOICE_ID != "21m00Tcm4TlvDq8ikWAM"


def test_library_voice_402_is_detected() -> None:
    blocked = SimpleNamespace(
        status_code=402,
        json=lambda: {
            "detail": {
                "type": "payment_required",
                "code": "paid_plan_required",
                "message": "Free users cannot use library voices via the API.",
            }
        },
    )
    assert _is_library_voice_block(blocked)
    credits = SimpleNamespace(
        status_code=402,
        json=lambda: {"detail": {"code": "insufficient_credits", "message": "No credits"}},
    )
    assert not _is_library_voice_block(credits)
    try:
        _raise_for_elevenlabs(blocked, "21m00Tcm4TlvDq8ikWAM")
    except RuntimeError as exc:
        assert "library" in str(exc).lower()
    else:
        raise AssertionError("expected RuntimeError")


def test_retries_library_voice_with_premade_default(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    audio = base64.b64encode(b"ID3fake-audio").decode()

    class Resp:
        def __init__(self, code: int, payload: dict):
            self.status_code = code
            self._payload = payload
            self.content = b"ID3fake-audio"

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"http {self.status_code}")

    def fake_post(url, **_kwargs):
        calls.append(url)
        if "21m00Tcm4TlvDq8ikWAM" in url:
            return Resp(
                402,
                {
                    "detail": {
                        "code": "paid_plan_required",
                        "message": "Free users cannot use library voices via the API.",
                    }
                },
            )
        return Resp(
            200,
            {
                "audio_base64": audio,
                "alignment": {
                    "characters": ["H", "i"],
                    "character_start_times_seconds": [0.0, 0.1],
                    "character_end_times_seconds": [0.1, 0.2],
                },
            },
        )

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr("requests.post", fake_post)
    out = tmp_path / "n.mp3"
    result = ElevenLabsTTS().execute(
        {
            "text": "Hi",
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
            "timestamps": True,
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    assert ElevenLabsTTS.DEFAULT_VOICE_ID in calls[-1]
    assert result.data["voice_id"] == ElevenLabsTTS.DEFAULT_VOICE_ID
    assert out.is_file()
