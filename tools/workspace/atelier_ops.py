"""Constrained atelier / HyperFrames helpers — IDE npx replacement."""

from __future__ import annotations

import subprocess
import sys
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


class AtelierStill(BaseTool):
    name = "atelier_still"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "workspace"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    best_for = ["render one Remotion still per atelier scene"]
    input_schema = {
        "type": "object",
        "required": ["composition_id"],
        "properties": {
            "composition_id": {"type": "string"},
            "slug": {"type": "string", "description": "Unused when the job workspace is set; stills write there"},
            "scene_id": {"type": "string"},
            "project_path": {"type": "string", "description": "Optional path inside the job workspace"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=512, disk_mb=32, network_required=False)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        from runner.config import repo_root
        from runner.workspace_root import WorkspaceError, get_root, resolve_in_workspace

        script = repo_root() / "scripts" / "atelier_snapshots.py"
        if not script.is_file():
            return ToolResult(success=False, error="atelier_snapshots.py missing", duration_seconds=round(time.time() - started, 2))
        composition_id = str(inputs.get("composition_id") or "").strip()
        if not composition_id:
            return ToolResult(success=False, error="composition_id required", duration_seconds=round(time.time() - started, 2))
        raw_proj = str(inputs.get("project_path") or "").strip()
        try:
            if raw_proj:
                proj = resolve_in_workspace(raw_proj)
            else:
                proj = get_root()
        except WorkspaceError as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=round(time.time() - started, 2))
        slug = str(inputs.get("slug") or proj.name).strip() or proj.name
        cmd = [
            sys.executable,
            str(script),
            slug,
            "--composition-id",
            composition_id,
            "--project-dir",
            str(proj),
        ]
        scene_id = str(inputs.get("scene_id") or "").strip()
        if scene_id:
            cmd.extend(["--only", scene_id])
        proc = subprocess.run(cmd, cwd=str(repo_root()), capture_output=True, text=True, timeout=1800)
        snap = proj / "snapshots"
        pngs = sorted(str(p) for p in snap.glob("*.png")) if snap.is_dir() else []
        ok = proc.returncode == 0 and bool(pngs)
        return ToolResult(
            success=ok,
            data={"slug": slug, "project_dir": str(proj), "snapshots": pngs, "stdout": (proc.stdout or "")[-1500:]},
            artifacts=pngs,
            error=None if ok else ((proc.stderr or proc.stdout or "atelier_still failed")[-1500:]),
            duration_seconds=round(time.time() - started, 2),
        )


class TypecheckAtelier(BaseTool):
    name = "typecheck_atelier"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "workspace"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    best_for = ["tsc check an atelier Remotion project"]
    input_schema = {
        "type": "object",
        "required": ["project_path"],
        "properties": {"project_path": {"type": "string"}},
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=0, network_required=False)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        WorkspaceError, resolve_in_workspace = _io()
        try:
            proj = resolve_in_workspace(str(inputs.get("project_path") or ""))
        except WorkspaceError as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=round(time.time() - started, 2))
        from runner.atelier import AtelierError, typecheck

        try:
            typecheck(proj)
        except AtelierError as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=round(time.time() - started, 2))
        return ToolResult(success=True, data={"project_path": str(proj)}, duration_seconds=round(time.time() - started, 2))


def _hf(operation: str, inputs: dict[str, Any]) -> ToolResult:
    started = time.time()
    WorkspaceError, resolve_in_workspace = _io()
    raw = str(inputs.get("workspace_path") or "")
    try:
        workspace = resolve_in_workspace(raw) if raw else None
    except WorkspaceError as exc:
        return ToolResult(success=False, error=str(exc), duration_seconds=round(time.time() - started, 2))
    if workspace is None:
        return ToolResult(success=False, error="workspace_path required", duration_seconds=round(time.time() - started, 2))
    from tools.video.hyperframes_compose import HyperFramesCompose

    result = HyperFramesCompose().execute({"operation": operation, "workspace_path": str(workspace)})
    result.duration_seconds = round(time.time() - started, 2)
    return result


class HyperframesLint(BaseTool):
    name = "hyperframes_lint"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "workspace"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    best_for = ["hyperframes lint without a free-form npx shell"]
    input_schema = {
        "type": "object",
        "required": ["workspace_path"],
        "properties": {"workspace_path": {"type": "string"}},
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=0, network_required=False)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return _hf("lint", inputs)


class HyperframesValidate(BaseTool):
    name = "hyperframes_validate"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "workspace"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    best_for = ["hyperframes validate without a free-form npx shell"]
    input_schema = {
        "type": "object",
        "required": ["workspace_path"],
        "properties": {"workspace_path": {"type": "string"}},
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=0, network_required=False)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        return _hf("validate", inputs)
