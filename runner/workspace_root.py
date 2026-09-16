"""Sandbox root for SaaS file tools. Always a directory under the job project."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

_root: ContextVar[Path | None] = ContextVar("saas_workspace_root", default=None)


class WorkspaceError(RuntimeError):
    pass


def set_root(path: Path) -> None:
    resolved = Path(path).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    _root.set(resolved)


def get_root() -> Path:
    root = _root.get()
    if root is None:
        raise WorkspaceError("workspace root is not set")
    return root


def resolve_in_workspace(rel: str | None) -> Path:
    root = get_root()
    raw = str(rel or "").strip() or "."
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"path escapes workspace: {raw}") from exc
    return resolved
