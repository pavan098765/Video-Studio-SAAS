from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from runner.karaoke import extract_words
from tools.audio.elevenlabs_tts import ElevenLabsTTS
from tools.audio.kokoro_tts import (
    DEFAULT_VOICE,
    KokoroTTS,
    lang_code_for,
    locale_for,
    model_quality,
    words_from_pykokoro,
    words_from_tokens,
)
from tools.audio.piper_tts import PiperTTS
from tools.audio.tts_selector import TTSSelector, is_kokoro_voice, is_piper_voice
from tools.base_tool import ToolResult, ToolStatus


def test_kokoro_voice_ids_are_detected() -> None:
    assert is_kokoro_voice("af_heart")
    assert is_kokoro_voice("am_michael")
    assert is_kokoro_voice("bf_emma")
    assert is_kokoro_voice("ef_dora")
    assert not is_kokoro_voice("en_US-lessac-medium")
    assert not is_kokoro_voice("Xb7hH8MSUJpSbSDYk0k2")
    assert not is_kokoro_voice("Rachel")
    assert not is_kokoro_voice("")


def test_selector_drops_kokoro_voice_for_elevenlabs() -> None:
    adapted = TTSSelector._adapt_inputs(
        ElevenLabsTTS(),
        {"text": "hello", "voice_id": "af_heart"},
    )
    assert "voice_id" not in adapted
    kept = TTSSelector._adapt_inputs(
        KokoroTTS(),
        {"text": "hello", "voice_id": "af_heart"},
    )
    assert kept["voice"] == "af_heart"
    assert kept["voice_id"] == "af_heart"


def test_elevenlabs_replaces_kokoro_voice_with_default() -> None:
    tool = ElevenLabsTTS()
    assert tool._resolve_voice_id({"voice_id": "af_heart"}) == ElevenLabsTTS.DEFAULT_VOICE_ID
    assert tool._resolve_voice_id({"voice_id": "en_US-lessac-medium"}) == ElevenLabsTTS.DEFAULT_VOICE_ID


def test_lang_code_follows_voice_prefix() -> None:
    assert lang_code_for("af_heart") == "a"
    assert lang_code_for("bf_emma") == "b"
    assert lang_code_for("", "en-GB") == "b"
    assert lang_code_for("", "zh") == "z"


def test_locale_for_pykokoro() -> None:
    assert locale_for("af_heart") == "en-us"
    assert locale_for("bf_emma") == "en-gb"
    assert locale_for("", "en-GB") == "en-gb"
    assert locale_for("", "zh") == "zh"


def test_model_quality_defaults_to_q8(monkeypatch) -> None:
    monkeypatch.delenv("KOKORO_MODEL_QUALITY", raising=False)
    assert model_quality() == "q8"
    monkeypatch.setenv("KOKORO_MODEL_QUALITY", "fp32")
    assert model_quality() == "fp32"
    monkeypatch.setenv("KOKORO_MODEL_QUALITY", "nope")
    assert model_quality() == "q8"


def test_words_from_tokens_groups_on_whitespace() -> None:
    tokens = [
        SimpleNamespace(text="Hel", start_ts=0.0, end_ts=0.12, whitespace=""),
        SimpleNamespace(text="lo", start_ts=0.12, end_ts=0.28, whitespace=" "),
        SimpleNamespace(text="world", start_ts=0.30, end_ts=0.55, whitespace=""),
    ]
    words = words_from_tokens(tokens, chunk_offset=1.0)
    assert words == [
        {"word": "Hello", "start": 1.0, "end": 1.28},
        {"word": "world", "start": 1.3, "end": 1.55},
    ]
    assert extract_words({"timestamps": words}) == words


def test_words_from_tokens_skips_missing_timestamps() -> None:
    tokens = [
        SimpleNamespace(text="hola", start_ts=None, end_ts=None, whitespace=" "),
        SimpleNamespace(text="mundo", start_ts=None, end_ts=None, whitespace=""),
    ]
    assert words_from_tokens(tokens) == []


def test_words_from_pykokoro_uses_sample_offsets() -> None:
    class FakeWord:
        def __init__(self, text: str, start: float, end: float, sr: int = 24_000):
            self.text = text
            self.start_sample = int(start * sr)
            self.end_sample = int(end * sr)

        def start_seconds(self, sample_rate: int) -> float:
            return self.start_sample / sample_rate

        def end_seconds(self, sample_rate: int) -> float:
            return self.end_sample / sample_rate

    result = SimpleNamespace(
        sample_rate=24_000,
        word_timings=[
            FakeWord("Hello", 0.0, 0.2),
            FakeWord("world", 0.2, 0.45),
        ],
    )
    words = words_from_pykokoro(result, "Hello world")
    assert [row["word"] for row in words] == ["Hello", "world"]
    assert words[0]["start"] == 0.0
    assert words[1]["end"] == 0.45
    assert extract_words({"timestamps": words}) == words


def test_words_from_pykokoro_skips_empty() -> None:
    assert words_from_pykokoro(SimpleNamespace(sample_rate=24000, word_timings=[])) == []
    assert words_from_pykokoro(SimpleNamespace(sample_rate=24000, word_timings=None)) == []


def test_kokoro_unavailable_without_package(monkeypatch) -> None:
    monkeypatch.setattr("tools.audio.kokoro_tts._backend", lambda: "")
    tool = KokoroTTS()
    assert tool.get_status() == ToolStatus.UNAVAILABLE
    result = tool.execute({"text": "hello"})
    assert result.success is False
    assert "not available" in (result.error or "").lower()


def test_pykokoro_execute_writes_wav_and_timestamps(monkeypatch, tmp_path) -> None:
    class FakeWord:
        def __init__(self, text: str, start: float, end: float):
            self.text = text
            self._start = start
            self._end = end

        def start_seconds(self, sample_rate: int) -> float:
            return self._start

        def end_seconds(self, sample_rate: int) -> float:
            return self._end

    class FakeResult:
        def __init__(self, path: str):
            self.audio = np.zeros(12_000, dtype=np.float32)
            self.sample_rate = 24_000
            self.word_timings = [
                FakeWord("Hello", 0.0, 0.2),
                FakeWord("world", 0.2, 0.4),
            ]
            self._path = path

        def save_wav(self, output: str) -> None:
            Path(output).write_bytes(b"RIFF")

        def release_audio(self) -> None:
            return None

    out = tmp_path / "n.wav"

    def fake_run(text, voice, locale, speed):
        assert voice == DEFAULT_VOICE
        assert locale == "en-us"
        return FakeResult(str(out))

    monkeypatch.setattr("tools.audio.kokoro_tts._backend", lambda: "pykokoro")
    monkeypatch.setattr("tools.audio.kokoro_tts._pykokoro_run", fake_run)
    result = KokoroTTS().execute(
        {
            "text": "Hello world",
            "voice_id": "af_heart",
            "timestamps": True,
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    assert out.is_file()
    assert result.data["backend"] == "pykokoro"
    assert result.data["voice"] == DEFAULT_VOICE
    words = extract_words(result.data)
    assert [row["word"] for row in words] == ["Hello", "world"]
    assert words[0]["start"] == 0.0
    assert words[1]["end"] == 0.4


def test_hexgrad_backend_still_writes_wav(monkeypatch, tmp_path) -> None:
    class FakeResult:
        def __init__(self):
            self.audio = np.zeros(12_000, dtype=np.float32)
            self.tokens = [
                SimpleNamespace(text="Hello", start_ts=0.0, end_ts=0.2, whitespace=" "),
                SimpleNamespace(text="world", start_ts=0.2, end_ts=0.4, whitespace=""),
            ]

    class FakePipeline:
        def __init__(self, lang_code="a"):
            self.lang_code = lang_code

        def __call__(self, text, voice="af_heart", speed=1.0, split_pattern=None):
            yield FakeResult()

    monkeypatch.setattr("tools.audio.kokoro_tts._backend", lambda: "kokoro")
    monkeypatch.setattr("tools.audio.kokoro_tts._hexgrad_pipeline", lambda lang: FakePipeline(lang))
    out = tmp_path / "n.wav"
    result = KokoroTTS().execute(
        {
            "text": "Hello world",
            "voice_id": "af_heart",
            "timestamps": True,
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    assert result.data["backend"] == "kokoro"
    words = extract_words(result.data)
    assert [row["word"] for row in words] == ["Hello", "world"]


def test_selector_falls_back_when_preferred_fails(monkeypatch, tmp_path) -> None:
    eleven = ElevenLabsTTS()
    kokoro = KokoroTTS()
    piper = PiperTTS()

    monkeypatch.setattr(eleven, "get_status", lambda: ToolStatus.AVAILABLE)
    monkeypatch.setattr(kokoro, "get_status", lambda: ToolStatus.AVAILABLE)
    monkeypatch.setattr(piper, "get_status", lambda: ToolStatus.UNAVAILABLE)
    monkeypatch.setattr(
        eleven,
        "execute",
        lambda _inputs: ToolResult(success=False, error="ElevenLabs payment required (HTTP 402)."),
    )

    def fake_kokoro(inputs):
        out = tmp_path / "fallback.wav"
        out.write_bytes(b"RIFF")
        return ToolResult(
            success=True,
            data={
                "output_path": str(out),
                "timestamps": [{"word": "Hi", "start": 0.0, "end": 0.2}],
            },
        )

    monkeypatch.setattr(kokoro, "execute", fake_kokoro)
    monkeypatch.setattr(
        TTSSelector,
        "_providers",
        lambda self: [eleven, kokoro, piper],
    )

    result = TTSSelector().execute(
        {
            "text": "Hi",
            "preferred_provider": "elevenlabs",
            "timestamps": True,
            "output_path": str(tmp_path / "n.wav"),
        }
    )
    assert result.success, result.error
    assert result.data["selected_tool"] == "kokoro_tts"
    assert result.data["fallback_from"][0]["tool"] == "elevenlabs_tts"
    assert extract_words(result.data)[0]["word"] == "Hi"


def test_piper_voice_still_dropped_for_kokoro() -> None:
    adapted = TTSSelector._adapt_inputs(
        KokoroTTS(),
        {"text": "hello", "voice_id": "en_US-lessac-medium"},
    )
    assert "voice_id" not in adapted
    assert is_piper_voice("en_US-lessac-medium")


def test_preferred_provider_pin_beats_elevenlabs_key(monkeypatch) -> None:
    from runner.tts import preferred_provider

    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.setenv("TTS_PROVIDER", "kokoro")
    assert preferred_provider() == "kokoro"
    monkeypatch.setenv("TTS_PROVIDER", "auto")
    assert preferred_provider() == "elevenlabs"
