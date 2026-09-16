"""Constrained project file tools — IDE replacement. No free-form shell."""

from __future__ import annotations

import time
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


def _io():
    from runner.workspace_root import WorkspaceError, resolve_in_workspace

    return WorkspaceError, resolve_in_workspace


class ReadFile(BaseTool):
    name = "read_file"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "workspace"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    best_for = ["read a file inside the current OpenMontage project"]
    not_good_for = ["paths outside the project workspace"]
    input_schema = {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "description": "Relative path under the project root"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=32, disk_mb=0, network_required=False)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        WorkspaceError, resolve_in_workspace = _io()
        try:
            path = resolve_in_workspace(str(inputs.get("path") or ""))
        except WorkspaceError as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=round(time.time() - started, 2))
        if not path.is_file():
            return ToolResult(success=False, error=f"not a file: {path}", duration_seconds=round(time.time() - started, 2))
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        offset = int(inputs.get("offset") or 0)
        limit = inputs.get("limit")
        chunk = lines[offset:] if limit is None else lines[offset : offset + int(limit)]
        return ToolResult(
            success=True,
            data={"path": str(path), "content": "\n".join(chunk), "line_count": len(lines)},
            artifacts=[str(path)],
            duration_seconds=round(time.time() - started, 2),
        )


class WriteFile(BaseTool):
    name = "write_file"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "workspace"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    best_for = ["author atelier TSX/HTML and project artifacts inside the workspace"]
    not_good_for = ["paths outside the project workspace"]
    input_schema = {
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=32, disk_mb=8, network_required=False)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        WorkspaceError, resolve_in_workspace = _io()
        try:
            path = resolve_in_workspace(str(inputs.get("path") or ""))
        except WorkspaceError as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=round(time.time() - started, 2))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(inputs.get("content") or "")
        path.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            data={"path": str(path), "bytes": len(content.encode("utf-8"))},
            artifacts=[str(path)],
            duration_seconds=round(time.time() - started, 2),
        )


class ListDir(BaseTool):
    name = "list_dir"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "workspace"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    best_for = ["list files in the current OpenMontage project"]
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative directory; default is project root"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=32, disk_mb=0, network_required=False)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        WorkspaceError, resolve_in_workspace = _io()
        try:
            path = resolve_in_workspace(str(inputs.get("path") or "."))
        except WorkspaceError as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=round(time.time() - started, 2))
        if not path.exists():
            return ToolResult(success=False, error=f"missing: {path}", duration_seconds=round(time.time() - started, 2))
        if path.is_file():
            entries = [path.name]
        else:
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return ToolResult(
            success=True,
            data={"path": str(path), "entries": entries[:500]},
            duration_seconds=round(time.time() - started, 2),
        )
