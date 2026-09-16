"""Canned/fixture vision-patch helper. Live compose iterates write_file + atelier_still instead."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from runner import budget

VISION_PATCH_ROUNDS = 3
STOP_QA = frozenset({"pass", "warn"})

PatchFn = Callable[[dict[str, Any], list[Path]], Path]
ReviewFn = Callable[[Path], dict[str, Any]]


def run_rounds(
    *,
    initial_path: Path,
    initial_qa: dict[str, Any],
    patch: PatchFn,
    review: ReviewFn | None,
    canned: bool = False,
    rounds: int = VISION_PATCH_ROUNDS,
) -> tuple[Path, dict[str, Any], int]:
    """Patch up to `rounds` times. Stop on pass/warn. Keep last successful path (or original)."""
    last = initial_path
    qa = initial_qa
    calls = 0
    limit = 1 if canned else max(1, rounds)
    for i in range(limit):
        from runner import progress

        progress.note(f"vision patch round {i + 1}/{limit} qa={qa.get('qa_status')}", kind="atelier")
        if budget.exceeded_message():
            break
        stills = [Path(str(p)) for p in (qa.get("stills") or []) if Path(str(p)).is_file()]
        try:
            last = patch(qa, stills if not canned else [])
            calls += 1
        except Exception:
            break
        if canned or review is None:
            break
        qa = review(last)
        if str(qa.get("qa_status") or "") in STOP_QA:
            break
    return last, qa, calls
