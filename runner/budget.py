"""Wire OpenMontage CostTracker to prefs.budget_cap_usd for SaaS jobs."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

from lib.config_model import BudgetMode
from tools.base_tool import ToolResult
from tools.cost_tracker import ApprovalRequiredError, BudgetExceededError, CostTracker

_ENTRY_KEYS = (
    "id",
    "tool",
    "operation",
    "status",
    "timestamp",
    "estimated_usd",
    "reserved_usd",
    "actual_usd",
    "details",
)

_tracker: ContextVar[CostTracker | None] = ContextVar("studio_cost_tracker", default=None)
_exceeded: ContextVar[str | None] = ContextVar("studio_budget_exceeded", default=None)
_work: ContextVar[Path | None] = ContextVar("studio_budget_work", default=None)


class BudgetCapError(RuntimeError):
    """Job would exceed prefs.budget_cap_usd in cap mode."""


def attach(prefs: dict[str, Any] | None, *, canned: bool, work_dir: Path | None = None) -> CostTracker:
    prefs = prefs or {}
    cap = prefs.get("budget_cap_usd")
    total = float(cap) if cap is not None else 10.0
    mode = BudgetMode.CAP if cap is not None and not canned else BudgetMode.OBSERVE
    log_path = None
    if work_dir is not None:
        try:
            from runner.artifacts import project_dir

            log_path = project_dir(Path(work_dir)) / "cost_log.json"
        except Exception:
            log_path = Path(work_dir) / "project" / "cost_log.json"
    tracker = CostTracker(
        budget_total_usd=max(0.0, total),
        reserve_pct=0.0,
        single_action_approval_usd=max(total, 1.0) + 1.0,
        require_approval_for_new_paid_tool=False,
        mode=mode,
        cost_log_path=log_path,
    )
    _tracker.set(tracker)
    _exceeded.set(None)
    _work.set(Path(work_dir) if work_dir is not None else None)
    return tracker


def current() -> CostTracker | None:
    return _tracker.get()


def exceeded_message() -> str | None:
    return _exceeded.get()


def clear() -> None:
    _tracker.set(None)
    _exceeded.set(None)
    _work.set(None)


def snapshot(tracker: CostTracker | None = None) -> dict[str, Any]:
    tracker = tracker or current()
    if tracker is None:
        return {"version": "1.0", "budget_total_usd": 0, "budget_reserved_usd": 0, "budget_spent_usd": 0, "entries": []}
    entries = []
    for raw in tracker.entries:
        row = {key: raw[key] for key in _ENTRY_KEYS if key in raw}
        entries.append(row)
    return {
        "version": "1.0",
        "budget_total_usd": round(tracker.budget_total_usd, 4),
        "budget_reserved_usd": round(tracker.budget_reserved_usd, 4),
        "budget_spent_usd": round(tracker.budget_spent_usd, 4),
        "entries": entries,
    }


def persist(work_dir: Path | None = None) -> None:
    tracker = current()
    dest_root = work_dir or _work.get()
    if tracker is None or dest_root is None:
        return
    try:
        from runner.artifacts import write_artifact

        write_artifact(dest_root, "cost_log", snapshot(tracker))
    except Exception:
        dest = Path(dest_root) / "project" / "artifacts" / "cost_log.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        import json

        dest.write_text(json.dumps(snapshot(tracker), indent=2), encoding="utf-8")


def reserve_tool(tool: Any, name: str, inputs: dict[str, Any]) -> str | None:
    tracker = current()
    if tracker is None:
        return None
    try:
        est = float(tool.estimate_cost(inputs) or 0.0)
    except Exception:
        est = 0.0
    entry_id = tracker.estimate(name, "execute", est)
    if est <= 0:
        return entry_id
    try:
        tracker.reserve(entry_id)
    except BudgetExceededError as exc:
        msg = str(exc)
        _exceeded.set(msg)
        raise BudgetCapError(msg) from exc
    except ApprovalRequiredError as exc:
        msg = str(exc)
        _exceeded.set(msg)
        raise BudgetCapError(msg) from exc
    return entry_id


def reconcile_tool(entry_id: str | None, result: ToolResult) -> None:
    tracker = current()
    if tracker is None or not entry_id:
        return
    actual = float(getattr(result, "cost_usd", 0.0) or 0.0)
    try:
        tracker.reconcile(entry_id, actual, success=bool(result.success))
    except Exception:
        pass
