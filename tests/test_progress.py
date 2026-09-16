from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_progress_note_writes_stderr(monkeypatch) -> None:
    from runner import progress

    monkeypatch.delenv("STUDIO_PROGRESS", raising=False)
    buf = StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    progress.note("hello stage", kind="stage")
    assert "[stage] hello stage" in buf.getvalue()


def test_progress_can_be_disabled(monkeypatch) -> None:
    from runner import progress

    monkeypatch.setenv("STUDIO_PROGRESS", "0")
    buf = StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    progress.note("silent", kind="stage")
    assert buf.getvalue() == ""
