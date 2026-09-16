"""Gemini Flash tool-calling. Refuses free-form shell. Tests inject a fake client."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools.base_tool import ToolResult


class LLMError(RuntimeError):
    pass


FORBIDDEN_TOOLS = {"shell", "bash", "run_command", "exec", "sh"}


# Gemini function Schema is a protobuf allowlist, not JSON Schema.
# Unknown keys (exclusiveMinimum, additionalProperties, …) are HTTP 400.
# enum is repeated string — numeric enum values also 400.
_GEMINI_KEYS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "items",
        "enum",
        "format",
        "nullable",
    }
)


def gemini_parameters(schema: Any) -> dict[str, Any]:
    """BaseTool jsonschema → Gemini function Schema protobuf fields.

    Arrays without ``items``, objects without ``properties``, unknown
    JSON Schema keywords, and non-string ``enum`` values are HTTP 400.
    """
    cleaned = _gemini_schema(schema if schema is not None else {"type": "object"})
    if not isinstance(cleaned, dict):
        return {"type": "object", "properties": {}}
    if cleaned.get("type") != "object":
        return {"type": "object", "properties": {"value": cleaned}}
    props = cleaned.get("properties")
    if not isinstance(props, dict):
        cleaned["properties"] = {}
    cleaned["type"] = "object"
    return cleaned


def _gemini_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_gemini_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    typ = node.get("type")
    if isinstance(typ, list):
        typ = next((item for item in typ if item != "null"), "string")
    if isinstance(typ, str):
        out["type"] = typ
    desc = node.get("description")
    if isinstance(desc, str) and desc:
        out["description"] = desc[:1024]
    fmt = node.get("format")
    if isinstance(fmt, str) and fmt:
        out["format"] = fmt
    if node.get("nullable") is True:
        out["nullable"] = True
    enum = node.get("enum")
    if not isinstance(enum, list):
        const = node.get("const")
        enum = [const] if const is not None else None
    if isinstance(enum, list):
        coerced: list[str] = []
        for item in enum:
            if isinstance(item, bool):
                coerced.append("true" if item else "false")
            else:
                coerced.append(str(item))
        out["enum"] = coerced
    if typ == "array":
        items = node.get("items")
        if isinstance(items, list):
            items = _gemini_schema(items[0]) if items else {"type": "string"}
        elif isinstance(items, dict):
            items = _gemini_schema(items)
        else:
            items = {"type": "string"}
        out["items"] = items if isinstance(items, dict) else {"type": "string"}
    if typ == "object":
        props = node.get("properties")
        if not isinstance(props, dict) or not props:
            text = str(out.get("description") or "JSON object")[:1000]
            return {"type": "string", "description": text + " (JSON object)"}
        out["properties"] = {key: _gemini_schema(value) for key, value in props.items()}
        required = node.get("required")
        if isinstance(required, list):
            out["required"] = [key for key in required if key in out["properties"]]
    return out


def function_declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emit every declared tool, sanitized for Gemini. Silent truncation is a second, smaller product."""
    decls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools or []:
        name = str(tool.get("name") or "")
        if not name or name in FORBIDDEN_TOOLS or name in seen:
            continue
        seen.add(name)
        decls.append(
            {
                "name": name,
                "description": str(tool.get("description") or name)[:1024],
                "parameters": gemini_parameters(tool.get("parameters") or {"type": "object"}),
            }
        )
    return decls


OnTool = Callable[[str, dict[str, Any]], ToolResult]
OnArtifact = Callable[[dict[str, Any]], str | None]
READONLY_TOOLS = frozenset({"read_file", "list_dir"})


def is_side_effect_tool(name: str) -> bool:
    return bool(name) and name not in READONLY_TOOLS and name not in FORBIDDEN_TOOLS


def function_calling_mode(*, force_tools: bool, side_effect_called: bool, has_tools: bool) -> str | None:
    """ANY until a write/search/render tool fires, then AUTO so the model can emit JSON."""
    if not has_tools:
        return None
    if force_tools and not side_effect_called:
        return "ANY"
    return "AUTO"


@dataclass
class LLMTurn:
    artifact: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    model_parts: list[dict[str, Any]] = field(default_factory=list)


class StageModel(Protocol):
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
    ) -> LLMTurn: ...


class MockGemini:
    """Deterministic tool-call sequence for tests."""

    is_mock = True

    def __init__(self, turns: list[LLMTurn]):
        self._turns = list(turns)

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
        del system, user, tools, retry, max_rounds, force_tools
        if not self._turns:
            raise LLMError("mock Gemini exhausted")
        turn = self._turns.pop(0)
        if on_tool and turn.tool_calls:
            for call in turn.tool_calls:
                name = str(call.get("name") or "")
                if name in FORBIDDEN_TOOLS:
                    raise LLMError("refused free-form shell tool")
                on_tool(name, call.get("arguments") or {})
        if turn.artifact and on_artifact:
            err = on_artifact(turn.artifact)
            if err and self._turns:
                return self.run_stage(
                    system="",
                    user=err,
                    tools=[],
                    retry=0,
                    on_tool=on_tool,
                    on_artifact=on_artifact,
                )
        return turn

    def generate_vision(self, *, system: str, user: str, images: list[Path]) -> LLMTurn:
        if self._turns:
            return self._turns.pop(0)
        return LLMTurn(artifact={"pass": True, "severity": "ok", "issues": [], "rewrite_hint": ""}, text='{"pass": true, "severity": "ok", "issues": [], "rewrite_hint": ""}')


class GeminiFlash:
    def __init__(self, api_key: str | None = None, model: str = "gemini-2.0-flash"):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
        self.model = model
        self.configured_model = model

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
            raise LLMError("GOOGLE_API_KEY missing")
        decls = function_declarations(tools)
        contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": user}]}]
        last = LLMTurn()
        rounds = max(1, int(max_rounds or 8))
        side_effect_called = False
        from runner import progress

        for round_i in range(rounds):
            mode = function_calling_mode(
                force_tools=force_tools, side_effect_called=side_effect_called, has_tools=bool(decls)
            )
            progress.note(
                f"round {round_i + 1}/{rounds} model={self.model} retry={retry} tools={len(decls)} mode={mode or 'none'}",
                kind="llm",
            )
            payload = self._generate(
                system, contents, decls, purpose="run_stage", function_calling_mode=mode
            )
            last = _parse_gemini(payload)
            if last.tool_calls:
                if on_tool is None:
                    return last
                response_parts: list[dict[str, Any]] = []
                for call in last.tool_calls:
                    name = call["name"]
                    progress.note(f"model requested tool {name}", kind="llm")
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
                    fr: dict[str, Any] = {"name": name, "response": body}
                    if call.get("id"):
                        fr["id"] = call["id"]
                    response_parts.append({"functionResponse": fr})
                from runner import tools_exec

                response_parts.append({"text": tools_exec.legal_url_reminder()})
                contents.append({"role": "model", "parts": last.model_parts or [{"text": last.text or ""}]})
                contents.append({"role": "user", "parts": response_parts})
                continue
            if last.artifact and on_artifact:
                err = on_artifact(last.artifact)
                if err:
                    contents.append({"role": "model", "parts": last.model_parts or [{"text": last.text or ""}]})
                    contents.append({"role": "user", "parts": [{"text": err}]})
                    side_effect_called = False
                    continue
            return last
        return last

    def generate_vision(self, *, system: str, user: str, images: list[Path]) -> LLMTurn:
        if not self.api_key:
            raise LLMError("GOOGLE_API_KEY missing")
        parts: list[dict[str, Any]] = [{"text": user}]
        parts.extend(image_parts(images))
        payload = self._generate(system, [{"role": "user", "parts": parts}], [], purpose="vision")
        return _parse_gemini(payload)

    def _generate(
        self,
        system: str,
        contents: list[dict[str, Any]],
        decls: list[dict[str, Any]],
        *,
        purpose: str = "run_stage",
        function_calling_mode: str | None = None,
    ) -> dict[str, Any]:
        from runner import telemetry
        from runner.gemini_limits import GeminiRateLimitError

        t0 = time.perf_counter()
        from runner import progress

        progress.note(f"waiting {self.model} {purpose} role={getattr(self, 'role', '') or 'n/a'}", kind="llm")
        try:
            payload = self._post(system, contents, decls, function_calling_mode=function_calling_mode)
        except GeminiRateLimitError as exc:
            telemetry.record_llm(
                provider="gemini",
                model=self.model,
                purpose=purpose,
                ok=False,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                error=str(exc),
            )
            raise LLMError(str(exc)) from exc
        except HTTPError as exc:
            detail = _http_body(exc)
            status, _ignored = telemetry.http_error_detail(exc)
            msg = f"{exc.reason}: {detail}" if detail else str(exc.reason)
            telemetry.record_llm(
                provider="gemini",
                model=self.model,
                purpose=purpose,
                ok=False,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
                error=msg[:800],
                http_status=status or exc.code,
            )
            raise LLMError(f"Gemini HTTP {exc.code}: {msg[:500]}") from exc
        usage = telemetry.usage_from_payload(payload)
        telemetry.record_llm(
            provider="gemini",
            model=self.model,
            purpose=purpose,
            ok=True,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            **usage,
        )
        return payload

    def _post(
        self,
        system: str,
        contents: list[dict[str, Any]],
        decls: list[dict[str, Any]],
        *,
        function_calling_mode: str | None = None,
    ) -> dict[str, Any]:
        from runner import gemini_limits
        from runner.telemetry import usage_from_payload

        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
        }
        if decls:
            body["tools"] = [{"function_declarations": decls}]
            mode = str(function_calling_mode or "AUTO").upper()
            if mode in {"ANY", "AUTO", "NONE"}:
                body["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
        estimated = gemini_limits.estimate_request_tokens(body)
        models = self._models_to_try()
        last_err: BaseException | None = None
        skip_non_lite = False
        for index, model in enumerate(models):
            if skip_non_lite and not _is_lite(model):
                continue
            self.model = model
            body["generationConfig"] = _generation_config(model)
            payload = json.dumps(body).encode()
            try:
                return self._post_model(model, payload, estimated, usage_from_payload)
            except gemini_limits.GeminiRateLimitError as exc:
                last_err = exc
                nxt = _next_atelier_model(models, index, skip_non_lite=False)
                if not self._atelier_fallback() or nxt is None:
                    raise
                _fallback_note(model, str(exc), nxt)
                continue
            except HTTPError as exc:
                last_err = exc
                detail = _http_body(exc)
                jump_lite = exc.code in {502, 503, 504} and not _is_lite(model)
                nxt = _next_atelier_model(models, index, skip_non_lite=jump_lite)
                atelier_walk = self._atelier_fallback() and _atelier_should_skip(exc, detail)
                capacity_walk = self._capacity_fallback() and _capacity_should_skip(exc, detail)
                if nxt and (atelier_walk or capacity_walk):
                    if _daily_quota(detail):
                        gemini_limits.mark_rpd_exhausted(model)
                    elif exc.code == 404 or _model_missing(detail):
                        gemini_limits.mark_unavailable(model, f"HTTP {exc.code}")
                    if jump_lite and _is_lite(nxt):
                        skip_non_lite = True
                    kind = "atelier" if atelier_walk else "planner"
                    _fallback_note(model, f"HTTP {exc.code}", nxt, kind=kind)
                    continue
                if exc.code != 429:
                    raise
                raise
        if last_err is not None:
            raise last_err
        raise LLMError("Gemini request failed")

    def _post_model(
        self,
        model: str,
        payload: bytes,
        estimated: int,
        usage_from_payload,
    ) -> dict[str, Any]:
        from runner import gemini_limits

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={self.api_key}"
        )
        last_http: HTTPError | None = None
        for attempt in range(6):
            gemini_limits.acquire(model, estimated)
            req = Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                with urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read().decode())
            except HTTPError as exc:
                last_http = exc
                detail = _http_body(exc)
                skip_retry = exc.code in {400, 404} or _daily_quota(detail)
                transient = exc.code in {502, 503, 504}
                if skip_retry:
                    raise
                if exc.code == 429:
                    if attempt >= 5:
                        raise
                    wait = _retry_after_seconds(exc) or min(60.0, 5.0 * (2**attempt))
                elif transient:
                    if attempt >= 2:
                        raise
                    wait = min(8.0, 2.0 * (2**attempt))
                else:
                    raise
                print(
                    f"[gemini-rate-limit] HTTP {exc.code}; retry in {wait:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
                gemini_limits._sleep(wait)
                continue
            usage = usage_from_payload(data)
            gemini_limits.record(model, int(usage.get("total_tokens") or estimated))
            if self._atelier_fallback():
                primary = self.configured_model
                if gemini_limits.rpd_exhausted(primary) or gemini_limits.is_unavailable(primary):
                    gemini_limits.set_atelier_sticky(model)
                else:
                    gemini_limits.clear_atelier_sticky()
            return data
        if last_http is not None:
            raise last_http
        raise LLMError("Gemini request failed")

    def _atelier_fallback(self) -> bool:
        return getattr(self, "role", "") == "atelier"

    def _capacity_fallback(self) -> bool:
        """Walk sibling Gemini IDs after persistent 502/503/504. Not used for RPD."""
        return getattr(self, "role", "") in {"planner", "visual_qa", "default"}

    def _models_to_try(self) -> list[str]:
        from runner import gemini_limits

        primary = self.configured_model
        if self._atelier_fallback():
            models = gemini_limits.atelier_models_to_try(primary)
            return models or gemini_limits.atelier_fallback_chain(primary)
        if self._capacity_fallback():
            models = gemini_limits.planner_models_to_try(primary)
            return models or gemini_limits.planner_fallback_chain(primary)
        return [self.model]


def _parse_gemini(payload: dict[str, Any]) -> LLMTurn:
    cands = payload.get("candidates") or []
    if not cands:
        raise LLMError("empty Gemini response")
    parts = (((cands[0] or {}).get("content") or {}).get("parts")) or []
    calls: list[dict[str, Any]] = []
    texts: list[str] = []
    model_parts: list[dict[str, Any]] = []
    for part in parts:
        model_parts.append(part)
        fc = part.get("functionCall") or part.get("function_call")
        if fc:
            args = fc.get("args") or fc.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args)
            name = fc.get("name")
            if name in FORBIDDEN_TOOLS:
                raise LLMError("refused free-form shell tool")
            row: dict[str, Any] = {"name": name, "arguments": args}
            cid = fc.get("id")
            if cid:
                row["id"] = cid
            calls.append(row)
        if part.get("text"):
            texts.append(part["text"])
    blob = "\n".join(texts).strip()
    return LLMTurn(artifact=_parse_json_blob(blob), tool_calls=calls, text=blob, model_parts=model_parts)


def _parse_json_blob(blob: str) -> dict[str, Any] | None:
    text = (blob or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def default_model(kind: str = "default") -> StageModel:
    if os.environ.get("STUDIO_LLM") == "mock":
        raise LLMError("STUDIO_LLM=mock requires an injected client")
    from runner.config import llm_role

    role = llm_role(kind)
    provider = role["provider"]
    model = role["model"]
    if provider in {"openai", "gpt", "openai_compat"}:
        from runner.llm_openai import OpenAIChat

        key_env = role["api_key_env"] or "OPENAI_API_KEY"
        client: Any = OpenAIChat(
            model=model,
            api_key=os.environ.get(key_env) or os.environ.get("OPENAI_API_KEY"),
            base_url=role["base_url"] or None,
            api_key_env=key_env,
        )
        client.role = kind if kind not in {"default", "planner"} else "planner"
        return client
    if provider in {"anthropic", "claude"}:
        from runner.llm_anthropic import AnthropicChat

        key_env = role["api_key_env"] or "ANTHROPIC_API_KEY"
        client = AnthropicChat(api_key=os.environ.get(key_env), model=model)
        client.role = kind if kind not in {"default", "planner"} else "planner"
        return client
    gemini = GeminiFlash(model=model)
    gemini.role = kind if kind not in {"default", "planner"} else "planner"
    return gemini


def image_parts(images: list[Path]) -> list[dict[str, Any]]:
    """Gemini generateContent REST parts. camelCase matches the public JSON API."""
    parts: list[dict[str, Any]] = []
    for path in images:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/webp" if suffix == ".webp" else "image/png"
        parts.append(
            {
                "inlineData": {
                    "mimeType": mime,
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            }
        )
    return parts


def _generation_config(model: str) -> dict[str, Any]:
    """Gemini 3 Flash rejects temperature (Google returns 503 'high demand'). Lite still wants it."""
    name = str(model or "").strip().lower()
    if _is_lite(name):
        return {"temperature": 0.4}
    if name.startswith("gemini-3"):
        return {"thinkingConfig": {"thinkingLevel": "low"}}
    return {"temperature": 0.4}


def _is_lite(model: str) -> bool:
    name = str(model or "").strip().lower()
    return "flash-lite" in name or name.endswith("-lite")


def _next_atelier_model(models: list[str], index: int, *, skip_non_lite: bool) -> str | None:
    rest = models[index + 1 :]
    if skip_non_lite:
        lite = [name for name in rest if _is_lite(name)]
        if lite:
            return lite[0]
    return rest[0] if rest else None


def _http_body(exc: HTTPError) -> str:
    cached = getattr(exc, "_studio_body", None)
    if isinstance(cached, str):
        return cached
    read = getattr(exc, "read", None)
    if not callable(read):
        return ""
    try:
        raw = read()
    except Exception:
        raw = b""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    try:
        exc._studio_body = text
    except Exception:
        pass
    return text


def _model_missing(detail: str) -> bool:
    text = (detail or "").lower()
    return "not found" in text or "not_found" in text or "model_not_found" in text


def _atelier_should_skip(exc: HTTPError, detail: str) -> bool:
    """Walk the atelier chain when this model cannot serve the request.

    404 / daily 429: this ID is gone for the day.
    502/503/504: `_post_model` already retried the same ID; Flash and Lite are
    different pools, so a persistent 503 on 3.8-flash should not kill the job.
    """
    if exc.code == 404 or _model_missing(detail):
        return True
    if exc.code == 429 and _daily_quota(detail):
        return True
    if exc.code in {502, 503, 504}:
        return True
    return False


def _capacity_should_skip(exc: HTTPError, detail: str) -> bool:
    """Planner/visual_qa may leave a down pool. Daily RPD stays fail-closed."""
    if exc.code == 404 or _model_missing(detail):
        return True
    if exc.code in {502, 503, 504}:
        return True
    return False


def _fallback_note(current: str, reason: str, nxt: str, *, kind: str = "atelier") -> None:
    print(f"[gemini-{kind}] {current} unavailable ({reason[:160]}); falling back to {nxt}", file=sys.stderr, flush=True)
    try:
        from runner import telemetry

        telemetry.record_flag(
            f"{kind}_fallback",
            f"{current} -> {nxt}",
            from_model=current,
            to_model=nxt,
            reason=reason[:200],
        )
    except Exception:
        pass


def _daily_quota(detail: str) -> bool:
    text = (detail or "").lower()
    return any(token in text for token in ("perday", "per day", "daily", "generaterequestesperday", "rpd"))


def _retry_after_seconds(exc: HTTPError) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None
