from __future__ import annotations

from typing import Any

from lib.pipeline_loader import get_stage_order, list_pipelines, load_pipeline


def load(name: str) -> dict[str, Any]:
    if name == "auto":
        raise ValueError("resolve pipeline=auto via runner.route.resolve before load")
    return load_pipeline(name)


def stages(manifest: dict[str, Any]) -> list[str]:
    return get_stage_order(manifest)


def available() -> list[str]:
    return [n for n in list_pipelines() if n != "framework-smoke"]


def stage_entry(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    for stage in manifest.get("stages") or []:
        if stage.get("name") == name:
            return stage
    return {}


def tools_available(manifest: dict[str, Any], stage: str) -> list[str]:
    """YAML allowlist. Empty means no tools (not every tool)."""
    names = stage_entry(manifest, stage).get("tools_available")
    if names is None:
        return []
    return [str(n) for n in names if n]


def max_revisions(manifest: dict[str, Any]) -> int:
    orch = manifest.get("orchestration") or {}
    try:
        return max(1, int(orch.get("max_revisions_per_stage") or 3))
    except (TypeError, ValueError):
        return 3


def max_send_backs(manifest: dict[str, Any]) -> int:
    orch = manifest.get("orchestration") or {}
    try:
        return max(1, int(orch.get("max_send_backs") or 3))
    except (TypeError, ValueError):
        return 3
