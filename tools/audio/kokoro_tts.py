"""Kokoro-82M local text-to-speech provider tool.

Primary backend is ``pykokoro`` (ONNX Runtime, no PyTorch). Timestamp-capable
ONNX models expose ``pred_dur`` word timings, normalized into the same
``{word, start, end}`` rows karaoke already consumes from ElevenLabs.

If pykokoro is not installed, hexgrad ``kokoro`` (KPipeline / Torch) is used
when present. Missing timings are left empty rather than fabricated.

Install: ``pip install "pykokoro[cpu]"``. First run downloads ONNX weights
into the pykokoro cache. Pin with ``TTS_PROVIDER=kokoro``.
"""

from __future__ import annotations

import os
import time
import wave
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

SAMPLE_RATE = 24_000
DEFAULT_VOICE = "af_heart"
DEFAULT_LOCALE = "en-us"
# HuggingFace onnx-community/Kokoro-82M-v1.0-ONNX. q8 is the CPU default:
# ~92MB, matches kokoro-js, int8 is actually faster on CPU than fp16/fp32.
DEFAULT_MODEL_QUALITY = "q8"
_ALLOWED_QUALITIES = {
    "fp32",
    "fp16",
    "q8",
    "q8f16",
    "q4",
    "q4f16",
    "uint8",
    "uint8f16",
}

# hexgrad KPipeline lang_code (first character of a Kokoro voice id).
_LOCALE_TO_LANG = {
    "en": "a",
    "en-us": "a",
    "en_us": "a",
    "en-gb": "b",
    "en_gb": "b",
    "es": "e",
    "fr": "f",
    "hi": "h",
    "it": "i",
    "pt": "p",
    "ja": "j",
    "zh": "z",
    "de": "g",
}

_PREFIX_TO_LOCALE = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "p": "pt",
    "j": "ja",
    "z": "zh",
    "g": "de",
}
_ISO_TO_LOCALE = {
    "en": "en-us",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "hi": "hi",
    "it": "it",
    "pt": "pt",
    "ja": "ja",
    "zh": "zh",
}

_PIPELINES: dict[str, Any] = {}
_PYKOKORO_PIPES: dict[tuple[str, ...], Any] = {}


def _backend() -> str:
    try:
        import pykokoro  # noqa: F401
    except Exception:
        pass
    else:
        return "pykokoro"
    try:
        import kokoro  # noqa: F401
    except Exception:
        return ""
    return "kokoro"


def lang_code_for(voice: str, language_code: str | None = None) -> str:
    raw_voice = str(voice or "").strip()
    if len(raw_voice) >= 1 and raw_voice[0].isalpha():
        return raw_voice[0].lower()
    locale = str(language_code or "").strip().lower().replace("_", "-")
    if locale in _LOCALE_TO_LANG:
        return _LOCALE_TO_LANG[locale]
    if locale[:2] in _LOCALE_TO_LANG:
        return _LOCALE_TO_LANG[locale[:2]]
    return "a"


def locale_for(voice: str, language_code: str | None = None) -> str:
    """pykokoro GenerationConfig.lang (en-us, en-gb, de, …)."""
    raw = str(language_code or "").strip().lower().replace("_", "-")
    if raw:
        if raw.startswith("en") and "gb" in raw:
            return "en-gb"
        if raw in {"en-us", "pt-br", "pt-pt", "zh-cn", "de-de"} or raw in _ISO_TO_LOCALE.values():
            return raw
        two = raw.split("-", 1)[0]
        if two in _ISO_TO_LOCALE:
            return _ISO_TO_LOCALE[two]
    prefix = str(voice or "").strip()[:1].lower()
    return _PREFIX_TO_LOCALE.get(prefix, DEFAULT_LOCALE)


def words_from_tokens(tokens: Any, chunk_offset: float = 0.0) -> list[dict[str, Any]]:
    """Group hexgrad KPipeline tokens on whitespace into karaoke word rows."""
    words: list[dict[str, Any]] = []
    buf: list[str] = []
    start: float | None = None
    end: float | None = None
    last_end = float(chunk_offset)

    for token in tokens or []:
        text = str(getattr(token, "text", "") or "").strip()
        if not text and not isinstance(token, dict):
            continue
        if isinstance(token, dict):
            text = str(token.get("text") or token.get("word") or "").strip()
            start_ts = token.get("start_ts", token.get("start"))
            end_ts = token.get("end_ts", token.get("end"))
            whitespace = str(token.get("whitespace") or " ")
        else:
            start_ts = getattr(token, "start_ts", None)
            end_ts = getattr(token, "end_ts", None)
            whitespace = str(getattr(token, "whitespace", " ") or "")
        if not text:
            if whitespace:
                _flush_word(words, buf, start, end, last_end)
                buf, start, end = [], None, None
            continue
        if start_ts is None or end_ts is None:
            if whitespace:
                buf, start, end = [], None, None
            continue
        abs_start = float(chunk_offset) + float(start_ts)
        abs_end = float(chunk_offset) + float(end_ts)
        if start is None:
            start = abs_start
        end = abs_end
        buf.append(text)
        if whitespace:
            last_end = _flush_word(words, buf, start, end, last_end)
            buf, start, end = [], None, None

    if buf:
        _flush_word(words, buf, start, end, last_end)
    return words


def words_from_pykokoro(result: Any, source_text: str = "") -> list[dict[str, Any]]:
    """Map pykokoro WordTiming rows into karaoke ``{word, start, end}`` seconds."""
    timings = getattr(result, "word_timings", None)
    if timings is None and isinstance(result, dict):
        timings = result.get("word_timings") or result.get("timestamps")
    if not timings:
        return []
    sample_rate = int(getattr(result, "sample_rate", None) or SAMPLE_RATE)
    clean = _pykokoro_clean_text(result, source_text)
    words: list[dict[str, Any]] = []
    for row in timings:
        item = _word_from_pykokoro_timing(row, sample_rate, clean)
        if item:
            words.append(item)
    return words


def _pykokoro_clean_text(result: Any, source_text: str) -> str:
    doc = getattr(result, "document", None)
    if doc is not None:
        clean = getattr(doc, "clean_text", None)
        if clean:
            return str(clean)
    meta = getattr(result, "document_metadata", None)
    if isinstance(meta, dict):
        clean = meta.get("clean_text")
        if clean:
            return str(clean)
    if isinstance(result, dict):
        return str(result.get("clean_text") or source_text or "")
    return source_text


def _word_from_pykokoro_timing(row: Any, sample_rate: int, clean: str) -> dict[str, Any] | None:
    if isinstance(row, dict):
        text = str(row.get("word") or row.get("text") or "").strip()
        if not text:
            cs, ce = row.get("char_start"), row.get("char_end")
            if cs is not None and ce is not None:
                text = str(clean[int(cs) : int(ce)]).strip()
        start = row.get("start", row.get("start_ts"))
        end = row.get("end", row.get("end_ts"))
        if start is None and row.get("start_sample") is not None and sample_rate:
            start = float(row["start_sample"]) / float(sample_rate)
            end = float(row.get("end_sample") or row["start_sample"]) / float(sample_rate)
    else:
        text = str(getattr(row, "text", "") or getattr(row, "word", "") or "").strip()
        if not text:
            cs = getattr(row, "char_start", None)
            ce = getattr(row, "char_end", None)
            if cs is not None and ce is not None:
                text = str(clean[int(cs) : int(ce)]).strip()
        start = _timing_seconds(row, "start", sample_rate)
        end = _timing_seconds(row, "end", sample_rate)
    if not text or start is None or end is None:
        return None
    start_f = float(start)
    end_f = float(end)
    if end_f < start_f:
        end_f = start_f
    return {"word": text, "start": round(start_f, 4), "end": round(end_f, 4)}


def _timing_seconds(row: Any, which: str, sample_rate: int) -> float | None:
    method = getattr(row, f"{which}_seconds", None)
    if callable(method):
        try:
            return float(method(sample_rate))
        except TypeError:
            return float(method())
    sample = getattr(row, f"{which}_sample", None)
    if sample is not None and sample_rate:
        return float(sample) / float(sample_rate)
    raw = getattr(row, which, None)
    if raw is not None:
        return float(raw)
    return None


def _flush_word(
    words: list[dict[str, Any]],
    buf: list[str],
    start: float | None,
    end: float | None,
    last_end: float,
) -> float:
    word = "".join(buf).strip()
    if not word:
        return last_end
    row_start = float(start if start is not None else last_end)
    row_end = float(end if end is not None else row_start)
    if row_end < row_start:
        row_end = row_start
    words.append(
        {
            "word": word,
            "start": round(row_start, 4),
            "end": round(row_end, 4),
        }
    )
    return row_end


def _to_numpy(audio: Any):
    import numpy as np

    if audio is None:
        return None
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim > 1:
        samples = samples.reshape(-1)
    return samples


def _write_wav(path: Path, samples, sample_rate: int = SAMPLE_RATE) -> None:
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import soundfile as sf

        sf.write(str(path), samples, sample_rate)
        return
    except Exception:
        pass
    pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm_i16 = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm_i16.tobytes())


def _hexgrad_pipeline(lang_code: str):
    if lang_code not in _PIPELINES:
        from kokoro import KPipeline

        _PIPELINES[lang_code] = KPipeline(lang_code=lang_code)
    return _PIPELINES[lang_code]


def model_quality() -> str:
    raw = (os.environ.get("KOKORO_MODEL_QUALITY") or DEFAULT_MODEL_QUALITY).strip().lower()
    return raw if raw in _ALLOWED_QUALITIES else DEFAULT_MODEL_QUALITY


def _pykokoro_pipeline(locale: str, voice: str, speed: float):
    provider = (os.environ.get("KOKORO_ONNX_PROVIDER") or "cpu").strip() or "cpu"
    quality = model_quality()
    key = (locale, voice, f"{speed:.3f}", provider, quality)
    if key not in _PYKOKORO_PIPES:
        from pykokoro import GenerationConfig, KokoroPipeline, PipelineConfig

        _PYKOKORO_PIPES[key] = KokoroPipeline(
            PipelineConfig(
                generation=GenerationConfig(lang=locale, speed=speed),
                voice=voice,
                provider=provider,
                model_quality=quality,
                retain_segment_audio=False,
            )
        )
    return _PYKOKORO_PIPES[key]


def _pykokoro_run(text: str, voice: str, locale: str, speed: float):
    return _pykokoro_pipeline(locale, voice, speed).run(text)


def _save_pykokoro_wav(result: Any, output_path: Path, samples) -> int:
    sample_rate = int(getattr(result, "sample_rate", None) or SAMPLE_RATE)
    saver = getattr(result, "save_wav", None)
    if callable(saver):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        saver(str(output_path))
        return sample_rate
    if samples is None:
        raise RuntimeError("pykokoro produced no audio.")
    _write_wav(output_path, samples, sample_rate)
    return sample_rate


class KokoroTTS(BaseTool):
    name = "kokoro_tts"
    version = "0.2.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "kokoro"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = []
    install_instructions = (
        "Install Kokoro locally via ONNX (no API key, no PyTorch):\n"
        "  pip install \"pykokoro[cpu]\"\n"
        "First run downloads onnx-community/Kokoro-82M-v1.0-ONNX (default q8).\n"
        "Override with KOKORO_MODEL_QUALITY=fp32|fp16|q8|q4.\n"
        "Optional Windows GPU: pip install \"pykokoro[directml]\" then "
        "KOKORO_ONNX_PROVIDER=directml.\n"
        "Pin with TTS_PROVIDER=kokoro when ElevenLabs quota is exhausted."
    )
    fallback = "piper_tts"
    fallback_tools = ["piper_tts", "openai_tts"]
    agent_skills = ["text-to-speech"]

    capabilities = [
        "text_to_speech",
        "offline_generation",
        "word_timestamps",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": True,
        "native_audio": True,
        "word_timestamps": True,
    }
    best_for = [
        "free local narration when cloud TTS quota is exhausted",
        "ONNX karaoke timestamps without installing PyTorch",
        "privacy-sensitive local-only workflows",
    ]
    not_good_for = [
        "best-in-class expressive voice quality",
        "voice clone matching",
        "models that omit duration heads (no fabricated timestamps)",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "voice": {
                "type": "string",
                "default": DEFAULT_VOICE,
                "description": "Kokoro voice id such as af_heart or am_michael.",
            },
            "voice_id": {
                "type": "string",
                "description": "Alias for voice. Selector may pass voice_id.",
            },
            "speed": {
                "type": "number",
                "default": 1.0,
                "minimum": 0.5,
                "maximum": 2.0,
            },
            "language_code": {
                "type": "string",
                "description": "Locale hint (en, en-GB, es, …) used when voice is omitted.",
            },
            "timestamps": {
                "type": "boolean",
                "default": False,
                "description": "Include word timestamps when the backend provides them.",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=2048, vram_mb=0, disk_mb=400, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=[])
    idempotency_key_fields = ["text", "voice", "voice_id", "speed", "language_code"]
    side_effects = ["writes audio file to output_path", "may download Kokoro ONNX weights on first run"]
    user_visible_verification = ["Listen to generated audio for intelligibility"]

    def get_status(self) -> ToolStatus:
        if _backend():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(success=False, error="Kokoro TTS not available. " + self.install_instructions)

        start = time.time()
        try:
            result = self._generate(inputs)
        except Exception as exc:
            return ToolResult(success=False, error=f"Kokoro TTS failed: {exc}")
        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _generate(self, inputs: dict[str, Any]) -> ToolResult:
        text = str(inputs.get("text") or "").strip()
        if not text:
            return ToolResult(success=False, error="Kokoro TTS requires non-empty text.")

        from tools.audio.tts_selector import is_kokoro_voice

        raw_voice = str(inputs.get("voice") or inputs.get("voice_id") or "").strip()
        voice = raw_voice if is_kokoro_voice(raw_voice) else DEFAULT_VOICE
        speed = min(2.0, max(0.5, float(inputs.get("speed") or inputs.get("speaking_rate") or 1.0)))
        output_path = Path(inputs.get("output_path") or "kokoro_tts.wav")
        if output_path.suffix.lower() != ".wav":
            output_path = output_path.with_suffix(".wav")

        backend = _backend()
        if backend == "pykokoro":
            return self._generate_pykokoro(inputs, text, voice, speed, output_path)
        return self._generate_hexgrad(inputs, text, voice, speed, output_path)

    def _generate_pykokoro(
        self,
        inputs: dict[str, Any],
        text: str,
        voice: str,
        speed: float,
        output_path: Path,
    ) -> ToolResult:
        locale = locale_for(voice, inputs.get("language_code"))
        result = _pykokoro_run(text, voice, locale, speed)
        samples = _to_numpy(getattr(result, "audio", None))
        sample_rate = _save_pykokoro_wav(result, output_path, samples)
        timestamps = words_from_pykokoro(result, text)
        duration = None
        if samples is not None and samples.size:
            duration = round(float(samples.size) / float(sample_rate), 4)
        release = getattr(result, "release_audio", None)
        if callable(release):
            try:
                release()
            except Exception:
                pass
        return self._result(
            inputs,
            output_path,
            voice=voice,
            lang=locale,
            timestamps=timestamps,
            sample_rate=sample_rate,
            duration=duration,
            model="kokoro-82m-onnx",
            backend="pykokoro",
        )

    def _generate_hexgrad(
        self,
        inputs: dict[str, Any],
        text: str,
        voice: str,
        speed: float,
        output_path: Path,
    ) -> ToolResult:
        import numpy as np

        lang = lang_code_for(voice, inputs.get("language_code"))
        pipeline = _hexgrad_pipeline(lang)
        chunks: list[Any] = []
        timestamps: list[dict[str, Any]] = []
        offset = 0.0
        for result in pipeline(text, voice=voice, speed=speed, split_pattern=r"\n+"):
            samples = _to_numpy(getattr(result, "audio", None))
            if samples is None or samples.size == 0:
                continue
            timestamps.extend(words_from_tokens(getattr(result, "tokens", None), offset))
            offset += float(samples.size) / float(SAMPLE_RATE)
            chunks.append(samples)
        if not chunks:
            return ToolResult(success=False, error="Kokoro produced no audio.")
        combined = np.concatenate(chunks)
        _write_wav(output_path, combined, SAMPLE_RATE)
        return self._result(
            inputs,
            output_path,
            voice=voice,
            lang=lang,
            timestamps=timestamps,
            sample_rate=SAMPLE_RATE,
            duration=round(float(combined.size) / float(SAMPLE_RATE), 4),
            model="kokoro-82m",
            backend="kokoro",
        )

    def _result(
        self,
        inputs: dict[str, Any],
        output_path: Path,
        *,
        voice: str,
        lang: str,
        timestamps: list[dict[str, Any]],
        sample_rate: int,
        duration: float | None,
        model: str,
        backend: str,
    ) -> ToolResult:
        if not output_path.is_file():
            return ToolResult(success=False, error="Kokoro produced no audio.")
        data: dict[str, Any] = {
            "provider": self.provider,
            "backend": backend,
            "model": model,
            "voice": voice,
            "voice_id": voice,
            "text_length": len(str(inputs.get("text") or "")),
            "output": str(output_path),
            "output_path": str(output_path),
            "path": str(output_path),
            "format": "wav",
            "sample_rate": sample_rate,
            "lang_code": lang,
        }
        if backend == "pykokoro":
            data["model_quality"] = model_quality()
        if duration is not None:
            data["duration"] = duration
        if timestamps:
            data["timestamps"] = timestamps
            data["word_timestamps"] = timestamps
            data["words"] = timestamps
        elif inputs.get("timestamps"):
            data["timestamps"] = []
        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(output_path)],
            model=model,
        )
