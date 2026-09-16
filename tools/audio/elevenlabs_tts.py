"""ElevenLabs text-to-speech provider tool."""

from __future__ import annotations

import os
import time
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


class ElevenLabsTTS(BaseTool):
    name = "elevenlabs_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "elevenlabs"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set the ELEVENLABS_API_KEY environment variable:\n"
        "  export ELEVENLABS_API_KEY=your_key_here\n"
        "Get a key at https://elevenlabs.io\n"
        "If fal_elevenlabs_tts is available, use it instead to access ElevenLabs "
        "speech through fal.ai without a separate ElevenLabs key."
    )
    fallback = "openai_tts"
    fallback_tools = ["openai_tts", "kokoro_tts", "piper_tts"]
    agent_skills = ["elevenlabs", "text-to-speech"]

    capabilities = [
        "text_to_speech",
        "voice_selection",
        "ssml_support",
        "pronunciation_control",
        "word_timestamps",
    ]
    supports = {
        "voice_cloning": True,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
        "word_timestamps": True,
    }
    best_for = [
        "high-quality narration",
        "voice-sensitive spokesperson videos",
        "multilingual spoken delivery",
    ]
    not_good_for = [
        "fully offline production",
        "privacy-constrained local-only workflows",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "Text to convert to speech"},
            "voice_id": {
                "type": "string",
                "description": "ElevenLabs voice ID (default: Alice, a free-plan premade voice).",
            },
            "model_id": {
                "type": "string",
                "default": "eleven_multilingual_v2",
                "description": "TTS model. Override with ELEVENLABS_MODEL (e.g. eleven_flash_v2_5).",
            },
            "stability": {
                "type": "number",
                "default": 0.5,
                "minimum": 0,
                "maximum": 1,
            },
            "similarity_boost": {
                "type": "number",
                "default": 0.75,
                "minimum": 0,
                "maximum": 1,
            },
            "style": {
                "type": "number",
                "default": 0.0,
                "minimum": 0,
                "maximum": 1,
            },
            "speed": {
                "type": "number",
                "default": 1.0,
                "minimum": 0.7,
                "maximum": 1.2,
            },
            "use_speaker_boost": {
                "type": "boolean",
                "default": True,
            },
            "output_path": {"type": "string"},
            "timestamps": {
                "type": "boolean",
                "default": False,
                "description": "Request word timestamps via /with-timestamps.",
            },
            "output_format": {
                "type": "string",
                "default": "mp3_44100_128",
                "enum": ["mp3_44100_128", "mp3_44100_192", "pcm_16000", "pcm_24000"],
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "text",
        "voice_id",
        "model_id",
        "stability",
        "similarity_boost",
        "style",
        "speed",
        "use_speaker_boost",
    ]
    side_effects = ["writes audio file to output_path", "calls ElevenLabs API"]
    user_visible_verification = ["Listen to generated audio for natural speech quality"]

    # Premade default (Alice). Rachel 21m00Tcm4TlvDq8ikWAM is Voice Library and
    # returns HTTP 402 paid_plan_required on the free API even with 10k credits.
    DEFAULT_VOICE_ID = "Xb7hH8MSUJpSbSDYk0k2"

    def _resolve_voice_id(self, inputs: dict[str, Any]) -> str:
        from tools.audio.tts_selector import is_kokoro_voice, is_piper_voice

        raw = str(inputs.get("voice_id") or inputs.get("voice") or "").strip()
        if not raw or is_piper_voice(raw) or is_kokoro_voice(raw):
            return self.DEFAULT_VOICE_ID
        return raw

    def get_status(self) -> ToolStatus:
        if os.environ.get("ELEVENLABS_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return round(len(inputs.get("text", "")) * 0.0003, 4)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            return ToolResult(success=False, error="No ElevenLabs API key. " + self.install_instructions)

        start = time.time()
        try:
            result = self._generate(inputs, api_key)
        except Exception as exc:
            return ToolResult(success=False, error=f"TTS generation failed: {exc}")

        result.duration_seconds = round(time.time() - start, 2)
        result.cost_usd = self.estimate_cost(inputs)
        return result

    def _generate(self, inputs: dict[str, Any], api_key: str) -> ToolResult:
        import base64

        import requests

        text = inputs["text"]
        voice_id = self._resolve_voice_id(inputs)
        model_id = (
            str(inputs.get("model_id") or os.environ.get("ELEVENLABS_MODEL") or "").strip()
            or "eleven_multilingual_v2"
        )
        output_format = inputs.get("output_format", "mp3_44100_128")
        want_ts = bool(inputs.get("timestamps"))
        voice_settings = {
            "stability": inputs.get("stability", 0.5),
            "similarity_boost": inputs.get("similarity_boost", 0.75),
            "style": inputs.get("style", 0.0),
            "speed": inputs.get("speed", 1.0),
            "use_speaker_boost": inputs.get("use_speaker_boost", True),
        }

        ext = "mp3" if "mp3" in output_format else "wav"
        output_path = Path(inputs.get("output_path", f"tts_output.{ext}"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        body = {
            "text": text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        }
        response = _tts_post(api_key, voice_id, want_ts, output_format, body)
        if _is_library_voice_block(response) and voice_id != self.DEFAULT_VOICE_ID:
            voice_id = self.DEFAULT_VOICE_ID
            response = _tts_post(api_key, voice_id, want_ts, output_format, body)
        _raise_for_elevenlabs(response, voice_id)

        timestamps: list[dict[str, Any]] = []
        if want_ts:
            payload = response.json()
            audio_b64 = payload.get("audio_base64") or payload.get("audioBase64") or ""
            output_path.write_bytes(base64.b64decode(audio_b64))
            alignment = payload.get("normalized_alignment") or payload.get("alignment") or {}
            timestamps = _words_from_alignment(alignment if isinstance(alignment, dict) else {})
        else:
            output_path.write_bytes(response.content)

        data: dict[str, Any] = {
            "provider": self.provider,
            "model": model_id,
            "voice_id": voice_id,
            "voice_settings": voice_settings,
            "text_length": len(text),
            "output": str(output_path),
            "output_path": str(output_path),
            "format": output_format,
        }
        if timestamps:
            data["timestamps"] = timestamps
        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(output_path)],
            model=model_id,
        )


def _tts_post(api_key: str, voice_id: str, want_ts: bool, output_format: str, body: dict[str, Any]):
    import requests

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    if want_ts:
        url += "/with-timestamps"
    return requests.post(
        url,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json" if want_ts else "audio/mpeg",
        },
        json=body,
        params={"output_format": output_format},
        timeout=120,
    )


def _detail(response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, dict) else {}


def _is_library_voice_block(response) -> bool:
    if getattr(response, "status_code", None) != 402:
        return False
    detail = _detail(response)
    code = str(detail.get("code") or "")
    message = str(detail.get("message") or "").lower()
    return code == "paid_plan_required" or "library voices" in message


def _raise_for_elevenlabs(response, voice_id: str) -> None:
    status = getattr(response, "status_code", 0)
    if status < 400:
        return
    detail = _detail(response)
    message = str(detail.get("message") or "").strip()
    code = str(detail.get("code") or "")
    if status == 402 and (code == "paid_plan_required" or "library voices" in message.lower()):
        raise RuntimeError(
            "ElevenLabs free API cannot use Voice Library voices "
            f"(voice_id={voice_id!r}). Using a premade default such as Alice. {message}"
        )
    if status == 402:
        raise RuntimeError(message or "ElevenLabs payment required (HTTP 402).")
    if status == 400:
        raise RuntimeError(
            message
            or f"ElevenLabs rejected voice_id={voice_id!r} (HTTP 400). "
            "Use an ElevenLabs voice id, not a Piper model name."
        )
    if message:
        raise RuntimeError(f"ElevenLabs HTTP {status} ({code or 'error'}): {message}")
    response.raise_for_status()


def _words_from_alignment(alignment: dict[str, Any]) -> list[dict[str, Any]]:
    chars = alignment.get("characters") or alignment.get("chars") or []
    starts = alignment.get("character_start_times_seconds") or alignment.get("characterStartTimesSeconds") or []
    ends = alignment.get("character_end_times_seconds") or alignment.get("characterEndTimesSeconds") or []
    if not chars:
        return []
    words: list[dict[str, Any]] = []
    buf = ""
    start: float | None = None
    end = 0.0
    for i, raw in enumerate(chars):
        ch = str(raw)
        s = float(starts[i]) if i < len(starts) else end
        e = float(ends[i]) if i < len(ends) else s
        if ch.isspace():
            if buf.strip():
                words.append({"word": buf.strip(), "start": float(start if start is not None else s), "end": float(end)})
            buf = ""
            start = None
            end = e
            continue
        if start is None:
            start = s
        buf += ch
        end = e
    if buf.strip():
        words.append({"word": buf.strip(), "start": float(start if start is not None else 0.0), "end": float(end)})
    return words
