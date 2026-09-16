"""Execute OpenMontage tools. Availability comes from the registry, not a SaaS ban list."""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from typing import Any

from tools.base_tool import ToolResult
from tools.tool_registry import ToolRegistry

from runner.budget import BudgetCapError, reconcile_tool, reserve_tool
from runner import telemetry

_registry: ToolRegistry | None = None
_traces: ContextVar[list[dict[str, Any]] | None] = ContextVar("studio_tool_traces", default=None)


def registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _registry.discover("tools")
    elif _registry.get("read_file") is None:
        _registry._discovered_packages.discard("tools")
        _registry.discover("tools")
    return _registry


def begin_trace() -> None:
    _traces.set([])


def traces() -> list[dict[str, Any]]:
    return list(_traces.get() or [])


def traced_urls() -> list[str]:
    seen: list[str] = []
    for row in traces():
        for url in row.get("urls") or []:
            text = str(url)
            if text and text not in seen:
                seen.append(text)
    return seen


def remember_urls(urls: list[str], name: str = "research_corpus") -> None:
    bucket = _traces.get()
    if bucket is None:
        return
    cleaned = [str(url) for url in urls if str(url).startswith("http")]
    if not cleaned:
        return
    bucket.append({"name": name, "success": True, "query": name, "urls": cleaned, "error": None})


def legal_url_reminder() -> str:
    urls = traced_urls()
    if not urls:
        return (
            "No LEGAL_SOURCE_URLS yet. Call web_search (and web_fetch on the best hits) "
            "before writing the research_brief. Do not invent URLs from training memory."
        )
    return (
        "LEGAL_SOURCE_URLS — copy these character-for-character into "
        "data_points.source_url, sources[].url, landscape.existing_content[].url, "
        "and any other url/source_url fields. Invented URLs fail the job.\n"
        + json.dumps(urls[:40])
    )


def execute(name: str, inputs: dict[str, Any]) -> ToolResult:
    t0 = time.perf_counter()
    query = (inputs or {}).get("query") or (inputs or {}).get("url") or (inputs or {}).get("operation")
    from runner import progress

    progress.note(f"start {name}" + (f" {query}" if query else ""), kind="tool")
    try:
        result = _execute(name, inputs)
    except BudgetCapError as exc:
        telemetry.record_tool(
            name=name,
            ok=False,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            error=f"budget_cap: {exc}",
            query=query,
        )
        raise
    telemetry.record_tool(
        name=name,
        ok=bool(result.success),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        error=result.error or "",
        query=query,
    )
    return result


def _execute(name: str, inputs: dict[str, Any]) -> ToolResult:
    tool = registry().get(name)
    if tool is None:
        result = ToolResult(success=False, error=f"unknown tool {name}")
        _record(name, inputs, result)
        return result
    try:
        entry_id = reserve_tool(tool, name, inputs)
    except BudgetCapError as exc:
        result = ToolResult(success=False, error=f"budget_cap: {exc}")
        _record(name, inputs, result)
        raise
    result = tool.execute(coerce_tool_inputs(tool, inputs or {}))
    reconcile_tool(entry_id, result)
    _record(name, inputs, result)
    return result


def coerce_tool_inputs(tool: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    """Undo Gemini sanitization: JSON object strings, stringified numeric enums."""
    schema = getattr(tool, "input_schema", None) or {}
    coerced = _coerce_against_schema(inputs, schema)
    return coerced if isinstance(coerced, dict) else dict(inputs or {})


def _coerce_against_schema(value: Any, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return value
    typ = schema.get("type")
    if typ == "object":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
        if isinstance(value, dict):
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            return {key: _coerce_against_schema(item, props[key]) if key in props else item for key, item in value.items()}
        return value
    if typ == "array" and isinstance(value, list):
        item_schema = schema.get("items") or {}
        return [_coerce_against_schema(item, item_schema) for item in value]
    if typ == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if typ == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if typ == "boolean" and isinstance(value, str):
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    return value


def _record(name: str, inputs: dict[str, Any], result: ToolResult) -> None:
    bucket = _traces.get()
    if bucket is None:
        return
    urls: list[str] = []
    data = result.data if isinstance(result.data, dict) else {}
    if name == "web_search":
        for row in data.get("results") or []:
            if isinstance(row, dict) and row.get("url"):
                urls.append(str(row["url"]))
    if name == "web_fetch" and data.get("url"):
        urls.append(str(data["url"]))
    bucket.append(
        {
            "name": name,
            "success": bool(result.success),
            "query": (inputs or {}).get("query") or (inputs or {}).get("url"),
            "urls": urls,
            "error": result.error,
        }
    )


def tool_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Empty list means no tools. None means every registered tool."""
    if names is not None and len(names) == 0:
        return []
    schemas: list[dict[str, Any]] = []
    for tool_name, tool in registry()._tools.items():
        if names is not None and tool_name not in names:
            continue
        schema = getattr(tool, "input_schema", None) or {"type": "object"}
        best = getattr(tool, "best_for", None)
        desc = best[0] if isinstance(best, list) and best else tool_name
        schemas.append(
            {
                "name": tool_name,
                "description": desc,
                "parameters": schema,
            }
        )
    return schemas


def layer3_skill_names(tool_names: list[str]) -> list[str]:
    names: list[str] = []
    for tool_name in tool_names:
        tool = registry().get(tool_name)
        if tool is None:
            continue
        for skill in getattr(tool, "agent_skills", None) or []:
            if skill and skill not in names:
                names.append(str(skill))
    return names
