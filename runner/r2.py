"""R2 via presigned URLs or boto3. VM never stores long-lived secrets in the snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


def put_bytes(url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    req = Request(url, data=data, method="PUT")
    req.add_header("Content-Type", content_type)
    with urlopen(req, timeout=120) as resp:
        resp.read()


def put_file(url: str, path: Path, content_type: str = "application/octet-stream") -> None:
    put_bytes(url, path.read_bytes(), content_type)


def get_file(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, method="GET")
    with urlopen(req, timeout=120) as resp:
        dest.write_bytes(resp.read())
    return dest


def put_json(url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    put_bytes(url, body, "application/json")
