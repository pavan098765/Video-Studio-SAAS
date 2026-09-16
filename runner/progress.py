"""Live stderr progress so long jobs do not look hung.

Disable with STUDIO_PROGRESS=0. Prints are flushed immediately.
"""

from __future__ import annotations

import os
import sys


def enabled() -> bool:
    raw = (os.environ.get("STUDIO_PROGRESS") or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def note(message: str, *, kind: str = "studio") -> None:
    if not enabled():
        return
    text = " ".join(str(message).split())
    if len(text) > 420:
        text = text[:417] + "..."
    print(f"[{kind}] {text}", file=sys.stderr, flush=True)
