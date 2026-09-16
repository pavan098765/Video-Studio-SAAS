"""Always route TTS through tts_selector."""

from __future__ import annotations

import os
from typing import Any

from runner import tools_exec
from tools.audio.tts_selector import is_kokoro_voice, is_piper_voice


def preferred_provider() -> str:
    if os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("TTS_PROVIDER") in {None, "", "auto"}:
        return "elevenlabs"
    return os.environ.get("TTS_PROVIDER") or ("elevenlabs" if os.environ.get("ELEVENLABS_API_KEY") else "piper")


def _drop_foreign_voice(provider: str, voice_id: str | None) -> bool:
    if not voice_id:
        return False
    if provider != "piper" and is_piper_voice(voice_id):
        return True
    if provider != "kokoro" and is_kokoro_voice(voice_id):
        return True
    return False


def speak(
    text: str,
    *,
    voice_id: str | None = None,
    output_path: str | None = None,
    voice_performance: dict[str, Any] | None = None,
    timestamps: bool = False,
    language_code: str | None = None,
) -> dict[str, Any]:
    provider = preferred_provider()
    inputs: dict[str, Any] = {
        "text": text,
        "preferred_provider": provider,
        "timestamps": bool(timestamps),
    }
    if language_code:
        inputs["language_code"] = language_code
        inputs["voice_language"] = language_code
    model_id = (os.environ.get("ELEVENLABS_MODEL") or "").strip()
    if model_id:
        inputs["model_id"] = model_id
    if voice_id and not _drop_foreign_voice(provider, voice_id):
        inputs["voice_id"] = voice_id
    if output_path:
        inputs["output_path"] = output_path
    if voice_performance:
        inputs["voice_performance"] = voice_performance
        notes = voice_performance.get("provider_notes") if isinstance(voice_performance, dict) else None
        if isinstance(notes, dict):
            if notes.get("stability") is not None:
                inputs["stability"] = notes["stability"]
            if notes.get("style") is not None:
                inputs["style"] = notes["style"]
            note_voice = notes.get("voice_id")
            if note_voice and "voice_id" not in inputs:
                if not _drop_foreign_voice(provider, str(note_voice)):
                    inputs["voice_id"] = note_voice
    result = tools_exec.execute("tts_selector", inputs)
    if not result.success:
        raise RuntimeError(result.error or "tts_selector failed")
    return result.data if isinstance(result.data, dict) else {"output_path": output_path}
