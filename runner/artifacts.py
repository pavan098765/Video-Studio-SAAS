"""Read/write OpenMontage artifacts with jsonschema validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from schemas.artifacts import ARTIFACT_NAMES, validate_artifact

from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS


def project_dir(work_dir: Path) -> Path:
    p = work_dir / "project"
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifacts_dir(work_dir: Path) -> Path:
    p = project_dir(work_dir) / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_artifact(work_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    if name in ARTIFACT_NAMES:
        validate_artifact(name, payload)
    elif not isinstance(payload, dict):
        raise TypeError(f"{name} payload must be an object")
    path = artifacts_dir(work_dir) / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_artifact(work_dir: Path, name: str) -> dict[str, Any] | None:
    path = artifacts_dir(work_dir) / f"{name}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def stage_artifact_name(stage: str) -> str:
    return CANONICAL_STAGE_ARTIFACTS.get(stage, stage)
