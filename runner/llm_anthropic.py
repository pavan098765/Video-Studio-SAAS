"""Anthropic Messages API StageModel. Same contract as GeminiFlash."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runner.llm_gemini import (
    FORBIDDEN_TOOLS,
    LLMError,
    LLMTurn,
    OnArtifact,
    OnTool,
    _parse_json_blob,
    function_calling_mode,
    function_declarations,
    is_side_effect_tool,
)

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


class AnthropicChat:
    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-5"):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        self.model = model

    def run_stage(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        retry: int,
        on_tool: OnTool | None = None,
        max_rounds: int = 8,
        on_artifact: OnArtifact | None = None,
        force_tools: bool = False,
    ) -> LLMTurn:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY missing")
        decls = _anthropic_tools(tools)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last = LLMTurn()
        rounds = max(1, int(max_rounds or 8))
        side_effect_called = False
        for _ in range(rounds):
            mode = function_calling_mode(
                force_tools=force_tools, side_effect_called=side_effect_called, has_tools=bool(decls)
            )
            payload = self._post(system, messages, decls, purpose="run_stage", function_calling_mode=mode)
            last = _parse_anthropic(payload)
            if last.tool_calls:
                if on_tool is None:
                    return last
                content = payload.get("content") or []
                messages.append({"role": "assistant", "content": content})
                tool_results: list[dict[str, Any]] = []
                for call in last.tool_calls:
                    name = call["name"]
                    if name in FORBIDDEN_TOOLS:
                        raise LLMError("refused free-form shell tool")
                    if is_side_effect_tool(name):
                        side_effect_called = True
                    result = on_tool(name, call.get("arguments") or {})
                    body: dict[str, Any]
                    if result.success:
                        body = result.data if isinstance(result.data, dict) else {"ok": True, "data": result.data}
                        if result.artifacts:
                            body["artifacts"] = result.artifacts
                    else:
                        body = {"ok": False, "error": result.error or "tool failed"}
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.get("id") or name,
                            "content": json.dumps(body),
                        }
                    )
                from runner import tools_exec

                tool_results.append({"type": "text", "text": tools_exec.legal_url_reminder()})
                messages.append({"role": "user", "content": tool_results})
                continue
            if last.artifact and on_artifact:
                err = on_artifact(last.artifact)
                if err:
                    messages.append({"role": "assistant", "content": last.text or json.dumps(last.artifact)})
                    messages.append({"role": "user", "content": err})
                    side_effect_called = False
                    continue
            return last
        return last

    def generate_vision(self, *, system: str, user: str, images: list[Path]) -> LLMTurn:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY missing")
        content: list[dict[str, Any]] = list(_anthropic_image_parts(images))
        content.append({"type": "text", "text": user})
        payload = self._post(system, [{"role": "user", "content": content}], [], purpose="vision")
        return _parse_anthropic(payload)

    def _post(
        self,
        system: str,
        messages: list[dict[str, Any]],
        decls: list[dict[str, Any]],
        *,
        purpose: str = "run_stage",
        function_calling_mode: str | None = None,
    ) -> dict[str, Any]:
        from runner import telemetry

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16384,
            "system": system,
            "messages": messages,
            "temperature": 0.4,
        }
        if decls:
            body["tools"] = decls
            mode = str(function_calling_mode or "AUTO").upper()
            if mode == "ANY":
                body["tool_choice"] = {"type": "any"}
            elif mode == "NONE":
                body["tool_choice"] = {"type": "none"}
            else:
                body["tool_choice"] = {"type": "auto"}
        req = Request(_MESSAGES_URL, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", self.api_key)
        req.add_header("anthropic-version", "2023-06-01")
        try:
            return telemetry.timed_llm(
                provider="anthropic",
                model=self.model,
                purpose=purpose,
                call=lambda: self._post_raw(req),
            )
        except HTTPError as exc:
            raise LLMError(f"Anthropic HTTP {exc.code}: {exc.reason}") from exc

    def _post_raw(self, req: Request) -> dict[str, Any]:
        with urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decls: list[dict[str, Any]] = []
    for tool in function_declarations(tools):
        decls.append(
            {
                "name": tool["name"],
                "description": str(tool.get("description") or tool["name"])[:1024],
                "input_schema": tool.get("parameters") or {"type": "object"},
            }
        )
    return decls


def _anthropic_image_parts(images: list[Path]) -> list[dict[str, Any]]:
    import base64

    parts: list[dict[str, Any]] = []
    for path in images:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/webp" if suffix == ".webp" else "image/png"
        parts.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                },
            }
        )
    return parts


def _parse_anthropic(payload: dict[str, Any]) -> LLMTurn:
    blocks = payload.get("content") or []
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
        if block.get("type") == "tool_use":
            name = block.get("name")
            if name in FORBIDDEN_TOOLS:
                raise LLMError("refused free-form shell tool")
            calls.append(
                {
                    "name": name,
                    "arguments": block.get("input") or {},
                    "id": block.get("id"),
                }
            )
    blob = "\n".join(texts).strip()
    return LLMTurn(artifact=_parse_json_blob(blob), tool_calls=calls, text=blob, model_parts=blocks)
