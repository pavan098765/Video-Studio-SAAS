"""Capability envelope at job start — OM preflight, not just runtime doctors."""

from __future__ import annotations

from typing import Any

from runner import tools_exec

ALIASES = {
    "transcriber": ("transcriber", "azure_stt", "openai_stt", "dashscope_asr"),
    "whisper": ("azure_stt", "openai_stt", "dashscope_asr"),
}


def summary() -> dict[str, Any]:
    try:
        return tools_exec.registry().provider_menu_summary()
    except Exception as exc:
        return {
            "composition_runtimes": {},
            "capabilities": [],
            "setup_offers": [],
            "runtime_warnings": [str(exc)],
        }


def _status_ok(name: str) -> bool:
    reg = tools_exec.registry()
    tool = reg.get(name)
    if tool is None:
        return False
    try:
        status = str(tool.get_status().value)
    except Exception:
        return False
    return status in {"available", "degraded"}


def _satisfied(name: str) -> bool:
    for alias in ALIASES.get(name, (name,)):
        if _status_ok(alias):
            return True
    return False


def required_names(manifest: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for n in manifest.get("required_tools") or []:
        if n:
            names.append(str(n))
    for stage in manifest.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        for n in stage.get("required_tools") or []:
            if n:
                names.append(str(n))
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


SKIP_PREFLIGHT = {"transcriber", "whisper"}


def required_missing(manifest: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for name in required_names(manifest):
        if name in SKIP_PREFLIGHT:
            continue
        if not _satisfied(name):
            missing.append(name)
    return missing
