"""OpenAI Chat Completions StageModel. Same contract as GeminiFlash."""

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


class OpenAIChat:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        *,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
    ):
        self.api_key_env = api_key_env or "OPENAI_API_KEY"
        self.api_key = api_key or os.environ.get(self.api_key_env) or os.environ.get("OPENAI_API_KEY") or ""
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

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
            raise LLMError(f"{self.api_key_env} missing")
        decls = _openai_tools(tools)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last = LLMTurn()
        rounds = max(1, int(max_rounds or 8))
        side_effect_called = False
        for _ in range(rounds):
            mode = function_calling_mode(
                force_tools=force_tools, side_effect_called=side_effect_called, has_tools=bool(decls)
            )
            payload = self._post(messages, decls, purpose="run_stage", function_calling_mode=mode)
            last = _parse_openai(payload)
            if last.tool_calls:
                if on_tool is None:
                    return last
                assistant = ((payload.get("choices") or [{}])[0].get("message")) or {}
                messages.append(assistant)
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
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or name,
                            "content": json.dumps(body),
                        }
                    )
                from runner import tools_exec

                messages.append({"role": "user", "content": tools_exec.legal_url_reminder()})
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
            raise LLMError(f"{self.api_key_env} missing")
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        content.extend(_openai_image_parts(images))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        payload = self._post(messages, [], purpose="vision")
        return _parse_openai(payload)

    def _post(
        self,
        messages: list[dict[str, Any]],
        decls: list[dict[str, Any]],
        *,
        purpose: str = "run_stage",
        function_calling_mode: str | None = None,
    ) -> dict[str, Any]:
        from runner import telemetry

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.4,
        }
        if decls:
            body["tools"] = decls
            mode = str(function_calling_mode or "AUTO").upper()
            body["tool_choice"] = "required" if mode == "ANY" else "none" if mode == "NONE" else "auto"
        url = f"{self.base_url}/chat/completions"
        req = Request(url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        provider = "openai" if "api.openai.com" in self.base_url else "openai_compat"
        try:
            return telemetry.timed_llm(
                provider=provider,
                model=self.model,
                purpose=purpose,
                call=lambda: self._post_raw(req),
            )
        except HTTPError as exc:
            raise LLMError(f"OpenAI HTTP {exc.code}: {exc.reason}") from exc

    def _post_raw(self, req: Request) -> dict[str, Any]:
        with urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decls: list[dict[str, Any]] = []
    for tool in function_declarations(tools):
        decls.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": str(tool.get("description") or tool["name"])[:1024],
                    "parameters": tool.get("parameters") or {"type": "object"},
                },
            }
        )
    return decls


def _openai_image_parts(images: list[Path]) -> list[dict[str, Any]]:
    import base64

    parts: list[dict[str, Any]] = []
    for path in images:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/webp" if suffix == ".webp" else "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts


def _parse_openai(payload: dict[str, Any]) -> LLMTurn:
    choices = payload.get("choices") or []
    if not choices:
        raise LLMError("empty OpenAI response")
    message = (choices[0] or {}).get("message") or {}
    text = str(message.get("content") or "")
    calls: list[dict[str, Any]] = []
    for tc in message.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        name = fn.get("name")
        if name in FORBIDDEN_TOOLS:
            raise LLMError("refused free-form shell tool")
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append({"name": name, "arguments": args, "id": tc.get("id")})
    return LLMTurn(artifact=_parse_json_blob(text), tool_calls=calls, text=text, model_parts=[message])
