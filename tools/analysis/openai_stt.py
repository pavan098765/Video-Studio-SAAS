"""OpenAI Whisper API transcription (local file upload).

Cloud STT for the SaaS runner when Azure Speech is not configured.
Output is shaped like `azure_stt` / `transcriber` (segments + word_timestamps).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ResumeSupport,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

_WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
_COST_PER_MINUTE = 0.006


class OpenAISpeechToText(BaseTool):
    name = "openai_stt"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "analysis"
    provider = "openai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set OPENAI_API_KEY to a key that can call /v1/audio/transcriptions.\n"
        "  Get one at https://platform.openai.com/"
    )
    fallback = "dashscope_asr"
    fallback_tools = ["dashscope_asr"]
    side_effects = ["sends audio to OpenAI Whisper"]
    user_visible_verification = [
        "Check transcript text against source audio",
        "Verify word timestamps align with speech",
    ]

    capabilities = ["speech_to_text", "word_timestamps", "multilingual"]
    supports = {"word_timestamps": True, "multilingual": True, "offline": False}
    best_for = ["Cloud Whisper when Azure Speech is not configured"]
    not_good_for = ["Fully offline runs"]

    input_schema = {
        "type": "object",
        "required": ["input_path"],
        "properties": {
            "input_path": {"type": "string"},
            "language": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
    }

    retry_policy = RetryPolicy(
        max_retries=2,
        retryable_errors=["ConnectionError", "Timeout", "429", "503"],
    )
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["input_path", "language"]
    resource_profile = ResourceProfile()

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if os.environ.get("OPENAI_API_KEY") else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        seconds = inputs.get("duration_seconds", 0) or 0
        return round((float(seconds) / 60.0) * _COST_PER_MINUTE, 4)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 30.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        if not input_path.exists():
            return ToolResult(success=False, error=f"Input file not found: {input_path}")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return ToolResult(success=False, error="OPENAI_API_KEY not set. " + self.install_instructions)

        start = time.time()
        try:
            result = self._transcribe(inputs, api_key, input_path)
        except Exception as exc:
            return ToolResult(success=False, error=f"OpenAI Whisper failed: {exc}")
        result.duration_seconds = round(time.time() - start, 2)
        result.model = "whisper-1"
        result.cost_usd = self.estimate_cost(
            {"duration_seconds": (result.data or {}).get("duration_seconds", 0)}
        )
        return result

    def _transcribe(self, inputs: dict[str, Any], api_key: str, input_path: Path) -> ToolResult:
        import requests

        output_dir = Path(inputs.get("output_dir", input_path.parent))
        output_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "model": "whisper-1",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        language = inputs.get("language")
        if language:
            data["language"] = str(language).split("-")[0]
        with open(input_path, "rb") as audio_file:
            response = requests.post(
                _WHISPER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (input_path.name, audio_file, "application/octet-stream")},
                data=data,
                timeout=600,
            )
        if response.status_code != 200:
            return ToolResult(
                success=False,
                error=f"OpenAI Whisper HTTP {response.status_code}: {response.text[:500]}",
            )
        try:
            payload = response.json()
        except ValueError:
            return ToolResult(success=False, error="OpenAI Whisper returned a non-JSON response.")
        result_data = self._parse_payload(payload)
        result_data["provider"] = "openai"
        output_path = output_dir / f"{input_path.stem}_transcript.json"
        output_path.write_text(json.dumps(result_data, indent=2), encoding="utf-8")
        return ToolResult(success=True, data=result_data, artifacts=[str(output_path)])

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        word_timestamps: list[dict[str, Any]] = []
        for idx, row in enumerate(payload.get("segments") or []):
            if not isinstance(row, dict):
                continue
            start = float(row.get("start") or 0)
            end = float(row.get("end") or start)
            text = str(row.get("text") or "").strip()
            seg_words = []
            for w in row.get("words") or []:
                if not isinstance(w, dict):
                    continue
                entry = {
                    "word": str(w.get("word") or w.get("text") or "").strip(),
                    "start": float(w.get("start") or start),
                    "end": float(w.get("end") or end),
                }
                seg_words.append(entry)
                word_timestamps.append(entry)
            segments.append({"id": idx, "start": start, "end": end, "text": text, "words": seg_words})
        if not word_timestamps:
            for w in payload.get("words") or []:
                if not isinstance(w, dict):
                    continue
                word_timestamps.append(
                    {
                        "word": str(w.get("word") or w.get("text") or "").strip(),
                        "start": float(w.get("start") or 0),
                        "end": float(w.get("end") or 0),
                    }
                )
        text = str(payload.get("text") or "").strip()
        if not text:
            text = " ".join(s.get("text") or "" for s in segments).strip()
        duration = payload.get("duration")
        if duration is None and segments:
            duration = max(s["end"] for s in segments)
        return {
            "text": text,
            "segments": segments,
            "word_timestamps": word_timestamps,
            "language": payload.get("language"),
            "duration_seconds": round(float(duration or 0), 3),
        }
