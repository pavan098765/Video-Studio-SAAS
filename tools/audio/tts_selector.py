"""Capability-level text-to-speech selector that chooses among provider tools.

Provider discovery is automatic — any BaseTool with capability="tts"
is picked up from the registry.  Adding a new TTS provider requires only creating
the tool file in tools/audio/; no changes to this selector are needed.
"""

from __future__ import annotations

import re
from typing import Any

from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStability, ToolTier, ToolStatus

# Piper checkpoint names (en_US-lessac-medium). Not valid on ElevenLabs/Azure/fal.
_PIPER_VOICE = re.compile(r"^[a-z]{2}_[A-Z]{2}-.+-(x_low|low|medium|high)$")
# Kokoro-82M voice ids (af_heart, am_michael, bf_emma, ef_dora). Not valid on cloud TTS.
_KOKORO_VOICE = re.compile(r"^[a-z][fm]_[a-z0-9]+$")


def is_piper_voice(value: Any) -> bool:
    return bool(_PIPER_VOICE.match(str(value or "").strip()))


def is_kokoro_voice(value: Any) -> bool:
    return bool(_KOKORO_VOICE.match(str(value or "").strip()))


class TTSSelector(BaseTool):
    name = "tts_selector"
    version = "0.2.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "selector"
    stability = ToolStability.BETA
    runtime = ToolRuntime.HYBRID
    agent_skills = ["text-to-speech", "elevenlabs"]

    capabilities = [
        "text_to_speech",
        "provider_selection",
    ]
    supports = {
        "user_preference_routing": True,
        "offline_fallback": True,
        "multilingual": True,
    }
    best_for = [
        "preflight tool selection",
        "user-facing recommendation flows",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "voice_id": {
                "type": "string",
                "description": "Provider-specific voice ID. Passed through to the selected TTS provider.",
            },
            "voice": {
                "type": "string",
                "description": "Provider-specific voice name or ID. fal.ai ElevenLabs accepts names such as Rachel.",
            },
            "voice_language": {
                "type": "string",
                "enum": ["zh", "en"],
                "description": "Kling official voice language. Passed through when selected provider supports it.",
            },
            "voice_speed": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 2.0,
                "description": "Kling official voice speed. Use speed for OpenAI/ElevenLabs-style controls.",
            },
            "model_id": {
                "type": "string",
                "description": "TTS model to use (e.g. eleven-v3 or eleven_multilingual_v2). Passed through to provider.",
            },
            "stability": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Voice stability (ElevenLabs). Lower = more expressive.",
            },
            "similarity_boost": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Voice similarity boost (ElevenLabs).",
            },
            "style": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Style exaggeration (ElevenLabs). Higher = more expressive.",
            },
            "instructions": {
                "type": "string",
                "description": "Provider-level delivery instructions for expressive narration when supported.",
            },
            "speaking_rate": {
                "type": "number",
                "minimum": 0.25,
                "maximum": 2.0,
                "description": "Google-style speakingRate control. Use speed for OpenAI/ElevenLabs-style controls.",
            },
            "speed": {
                "type": "number",
                "minimum": 0.25,
                "maximum": 4.0,
                "description": "Alias for speaking speed used by some providers.",
            },
            "pitch": {
                "type": "number",
                "minimum": -50,
                "maximum": 50,
                "description": "Provider-specific pitch control. Google TTS accepts -20..20; HeyGen-style providers may accept wider ranges.",
            },
            "input_type": {
                "type": "string",
                "enum": ["text", "ssml"],
                "default": "text",
                "description": "Use 'ssml' only when the selected provider supports tags such as <break>.",
            },
            "language_code": {
                "type": "string",
                "description": "Provider-specific language code, such as en-US for Google or en for fal.ai ElevenLabs.",
            },
            "timestamps": {
                "type": "boolean",
                "default": False,
                "description": "Request word timestamps when the selected provider supports them.",
            },
            "apply_text_normalization": {
                "type": "string",
                "enum": ["auto", "on", "off"],
                "description": "Text normalization mode for providers that support it.",
            },
            "seed": {
                "type": "integer",
                "description": "Optional generation seed for providers that support reproducible speech.",
            },
            "voice_performance": {
                "type": "object",
                "description": "Structured voice-performance plan or section delivery cues from the script artifact.",
            },
            "sample_mode": {
                "type": "boolean",
                "default": False,
                "description": "True when generating an approval sample before batch narration.",
            },
            "output_format": {
                "type": "string",
                "description": "Audio output format (e.g. mp3_44100_128). Passed through to provider.",
            },
            "preferred_provider": {
                "type": "string",
                "description": "Provider name or 'auto'. Valid values are discovered at runtime from the registry.",
                "default": "auto",
            },
            "allowed_providers": {
                "type": "array",
                "items": {"type": "string"},
            },
            "operation": {
                "type": "string",
                "enum": ["generate", "rank"],
                "default": "generate",
                "description": "Operation mode. 'rank' returns scored provider rankings without generating.",
            },
            "output_path": {"type": "string"},
        },
    }

    def _providers(self) -> list[BaseTool]:
        """Auto-discover TTS providers from the registry."""
        from tools.tool_registry import registry
        registry.ensure_discovered()
        return [t for t in registry.get_by_capability("tts")
                if t.name != self.name]

    @property
    def fallback_tools(self) -> list[str]:
        """Dynamically built from discovered providers."""
        return [t.name for t in self._providers()]

    @property
    def provider_matrix(self) -> dict[str, dict[str, str]]:
        """Built at runtime from each provider's best_for field."""
        matrix = {}
        for tool in self._providers():
            strength = ", ".join(tool.best_for) if tool.best_for else tool.name
            matrix[tool.provider] = {"tool": tool.name, "strength": strength}
        return matrix

    def get_status(self) -> ToolStatus:
        if any(tool.get_status() == ToolStatus.AVAILABLE for tool in self._providers()):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        candidates = self._providers()
        if not candidates:
            return 0.0
        tool, _ = self._select_best_tool(inputs, candidates, self._prepare_task_context(inputs))
        return tool.estimate_cost(inputs) if tool else 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        from lib.scoring import rank_providers

        task_context = self._prepare_task_context(inputs)
        candidates = self._providers()

        # Rank mode — return scored provider rankings without generating
        if inputs.get("operation") == "rank":
            rankings = rank_providers(candidates, task_context)
            return ToolResult(
                success=True,
                data={
                    "rankings": self._serialize_rankings(candidates, rankings),
                    "explanation": "\n".join(r.explain() for r in rankings[:5]),
                    "normalized_task_context": task_context,
                },
            )

        # Normal generation — use scored selection, then walk ranked fallbacks
        # so an exhausted cloud quota can still land on local Kokoro/Piper.
        ordered = self._ranked_available_tools(inputs, candidates, task_context)
        if not ordered:
            return ToolResult(success=False, error="No TTS provider available.")

        errors: list[dict[str, str]] = []
        last_result: ToolResult | None = None
        for tool, score in ordered:
            result = tool.execute(self._adapt_inputs(tool, inputs))
            last_result = result
            if result.success:
                if not isinstance(result.data, dict):
                    result.data = {}
                result.data.setdefault("selected_tool", tool.name)
                result.data["selected_provider"] = tool.provider
                result.data["selection_reason"] = (
                    score.explain() if score else f"Selected {tool.provider} ({tool.name})"
                )
                if score:
                    result.data["provider_score"] = score.to_dict()
                result.data.update(self._tool_context_payload(tool))
                result.data["alternatives_considered"] = [
                    t.name for t in candidates
                    if t.name != tool.name and t.get_status().value == "available"
                ]
                if errors:
                    result.data["fallback_from"] = errors
                return result
            errors.append({"tool": tool.name, "error": str(result.error or "failed")})

        if last_result is not None and errors:
            prior = "; ".join(f"{row['tool']}: {row['error']}" for row in errors[:-1])
            if prior:
                last_result.error = (
                    f"{last_result.error or 'TTS failed'} "
                    f"(also tried: {prior})"
                )
        return last_result or ToolResult(success=False, error="No TTS provider available.")

    @staticmethod
    def _adapt_inputs(tool: BaseTool, inputs: dict[str, Any]) -> dict[str, Any]:
        """Translate capability-level controls to provider-native inputs."""
        adapted = dict(inputs)
        if tool.name != "piper_tts":
            for key in ("voice_id", "voice"):
                if is_piper_voice(adapted.get(key)):
                    adapted.pop(key, None)
        if tool.name != "kokoro_tts":
            for key in ("voice_id", "voice"):
                if is_kokoro_voice(adapted.get(key)):
                    adapted.pop(key, None)
        else:
            if adapted.get("voice_id") and not adapted.get("voice"):
                adapted["voice"] = adapted["voice_id"]
            if adapted.get("speaking_rate") is not None and "speed" not in inputs:
                adapted["speed"] = adapted["speaking_rate"]
            return adapted
        if tool.name != "azure_tts":
            return adapted

        if adapted.get("voice_id") and not adapted.get("voice"):
            adapted["voice"] = adapted["voice_id"]

        speed = inputs.get("speaking_rate", inputs.get("speed"))
        if speed is not None and "rate" not in inputs:
            percent = round((float(speed) - 1.0) * 100)
            adapted["rate"] = f"{percent:+d}%" if percent else "0%"

        pitch = inputs.get("pitch")
        if isinstance(pitch, (int, float)):
            adapted["pitch"] = f"{pitch:+g}st" if pitch else "0%"

        # The selector's numeric style is ElevenLabs-specific. Azure's style
        # is a named express-as value such as "calm" or "newscast".
        if not isinstance(inputs.get("style"), str):
            adapted.pop("style", None)

        output_format = str(inputs.get("output_format", ""))
        if output_format.startswith("mp3"):
            adapted["output_format"] = "mp3"
        elif output_format.startswith(("wav", "riff", "pcm")):
            adapted["output_format"] = "wav"
        return adapted

    def _select_best_tool(
        self,
        inputs: dict[str, Any],
        candidates: list[BaseTool],
        task_context: dict[str, Any],
    ) -> tuple[BaseTool | None, object]:
        """Select the best TTS provider using scored ranking."""
        ordered = self._ranked_available_tools(inputs, candidates, task_context)
        if not ordered:
            return None, None
        return ordered[0]

    def _ranked_available_tools(
        self,
        inputs: dict[str, Any],
        candidates: list[BaseTool],
        task_context: dict[str, Any],
    ) -> list[tuple[BaseTool, object]]:
        """Preferred provider first, then remaining ranked available tools."""
        from lib.scoring import rank_providers

        preferred = inputs.get("preferred_provider", "auto")
        allowed = set(inputs.get("allowed_providers") or [])
        if allowed:
            candidates = [tool for tool in candidates if tool.provider in allowed]

        if inputs.get("timestamps"):
            timestamping = [
                tool
                for tool in candidates
                if tool.get_status() == ToolStatus.AVAILABLE
                and (
                    bool((tool.supports or {}).get("word_timestamps"))
                    or "word_timestamps" in (tool.capabilities or [])
                    or tool.name in {"elevenlabs_tts", "fal_elevenlabs_tts"}
                )
            ]
            if timestamping:
                candidates = timestamping
                if preferred == "piper":
                    preferred = "auto"

        rankings = rank_providers(candidates, task_context)
        tool_by_provider: dict[str, BaseTool] = {}
        for tool in candidates:
            if tool.provider not in tool_by_provider and tool.get_status() == ToolStatus.AVAILABLE:
                tool_by_provider[tool.provider] = tool

        ordered: list[tuple[BaseTool, object]] = []
        seen: set[str] = set()
        if preferred != "auto":
            for score_item in rankings:
                if score_item.provider == preferred and score_item.provider in tool_by_provider:
                    tool = tool_by_provider[score_item.provider]
                    ordered.append((tool, score_item))
                    seen.add(tool.name)
                    break
        for score_item in rankings:
            tool = tool_by_provider.get(score_item.provider)
            if tool is None or tool.name in seen:
                continue
            ordered.append((tool, score_item))
            seen.add(tool.name)
        return ordered

    def _prepare_task_context(self, inputs: dict[str, Any]) -> dict[str, Any]:
        from lib.scoring import normalize_task_context

        return normalize_task_context(
            inputs.get("task_context", {}),
            prompt=inputs.get("text", ""),
            capability=self.capability,
            operation=inputs.get("operation", "generate"),
        )

    @staticmethod
    def _tool_context_payload(tool: BaseTool) -> dict[str, Any]:
        info = tool.get_info()
        return {
            "selected_tool_agent_skills": info.get("agent_skills", []),
            "required_agent_skills": info.get("agent_skills", []),
            "selected_tool_usage_location": info.get("usage_location"),
            "selected_tool_best_for": info.get("best_for", []),
        }

    def _serialize_rankings(self, candidates: list[BaseTool], rankings: list[object]) -> list[dict[str, Any]]:
        tool_by_name = {tool.name: tool for tool in candidates}
        serialized: list[dict[str, Any]] = []
        for score in rankings:
            item = score.to_dict()
            tool = tool_by_name.get(score.tool_name)
            if tool:
                info = tool.get_info()
                item["agent_skills"] = info.get("agent_skills", [])
                item["usage_location"] = info.get("usage_location")
                item["best_for"] = info.get("best_for", [])
                item["status"] = str(tool.get_status())
            serialized.append(item)
        return serialized
