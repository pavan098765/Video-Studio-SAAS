"""Pin pytest temp onto the repo work drive (Windows C: is often too small)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "work" / "pytest-tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = str(SCRATCH)
os.environ["TMP"] = str(SCRATCH)
os.environ["TMPDIR"] = str(SCRATCH)
tempfile.tempdir = str(SCRATCH)

# Local .env may hold dummy-* placeholders for checkout. Do not treat them as live keys in tests.
for _name, _value in list(os.environ.items()):
    if isinstance(_value, str) and _value.startswith("dummy-"):
        os.environ.pop(_name, None)
