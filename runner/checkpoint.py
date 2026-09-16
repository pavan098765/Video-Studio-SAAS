"""Thin wrapper around OpenMontage checkpoints. Statuses pass through unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lib import checkpoint as om_checkpoint

from runner.artifacts import artifacts_dir, project_dir

_STATUS = {
    "complete": "completed",
    "completed": "completed",
    "waiting": "awaiting_human",
    "awaiting_human": "awaiting_human",
    "in_progress": "in_progress",
    "failed": "failed",
}


def init_job(work_dir: Path, pipeline: str, title: str, playbook: str | None = None) -> Path:
    proj = project_dir(work_dir)
    return om_checkpoint.init_project(
        proj.name,
        title=title[:120] or pipeline,
        pipeline_type=pipeline,
        pipeline_dir=work_dir,
        style_playbook=playbook,
    )


def write_stage(
    work_dir: Path,
    pipeline: str,
    stage: str,
    status: str = "completed",
    *,
    artifacts: dict[str, Any] | None = None,
    human_approved: bool | None = None,
    review: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
) -> Path:
    proj = project_dir(work_dir)
    om_status = _STATUS.get(status, status)
    gated = bool(om_checkpoint._stage_requires_approval(pipeline, stage))
    if human_approved is None:
        approved = bool(gated and om_status == "completed")
    else:
        approved = bool(human_approved)
    cost = None
    try:
        from runner import budget

        snap = budget.snapshot()
        if snap.get("entries"):
            cost = {
                "total_spent_usd": snap.get("budget_spent_usd") or 0,
                "total_reserved_usd": snap.get("budget_reserved_usd") or 0,
                "budget_remaining_usd": (snap.get("budget_total_usd") or 0) - (snap.get("budget_spent_usd") or 0),
            }
    except Exception:
        cost = None
    return om_checkpoint.write_checkpoint(
        work_dir,
        proj.name,
        stage,
        om_status,
        artifacts or {},
        pipeline_type=pipeline,
        human_approval_required=gated or om_status == "awaiting_human",
        human_approved=approved if om_status == "completed" else False,
        review=review,
        cost_snapshot=cost,
        error=error,
        metadata=metadata,
    )


def mark_human_approval(work_dir: Path, stage: str, approved: bool) -> None:
    artifacts_dir(work_dir).joinpath(f"approval_{stage}.json").write_text(
        f'{{"stage": "{stage}", "approved": {str(approved).lower()}}}',
        encoding="utf-8",
    )
