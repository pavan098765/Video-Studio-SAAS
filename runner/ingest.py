"""Resolve job asset_keys into local footage files."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from runner.ffmpeg_bin import find_ffmpeg, prepend_to_path

FOOTAGE_PIPELINES = {
    "talking-head",
    "clip-factory",
    "hybrid",
    "podcast-repurpose",
    "localization-dub",
}

REQUIRED_FOOTAGE = {
    "talking-head",
    "clip-factory",
    "hybrid",
    "podcast-repurpose",
    "localization-dub",
}


def _dummy_clip(dest: Path, seconds: int = 12) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
        dest = dest.with_suffix(".mp4")
    prepend_to_path()
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg required to materialize footage")
    subprocess.check_call(
        [
            ff,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-shortest",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return dest


def resolve_key(raw: str, dest_dir: Path, *, canned: bool, index: int) -> Path | None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"source_{index:02d}{Path(raw).suffix or '.mp4'}"
    path = Path(raw)
    if path.is_file():
        shutil.copy(path, dest)
        return dest
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            with urlopen(raw, timeout=60) as resp:
                dest.write_bytes(resp.read())
            return dest
        except Exception:
            if canned:
                return _dummy_clip(dest)
            return None
    if os.path.isfile(raw):
        shutil.copy(raw, dest)
        return dest
    if canned:
        return _dummy_clip(dest)
    return None


def local_keys(job: dict[str, Any]) -> list[str]:
    keys = list(job.get("asset_keys") or job.get("assetKeys") or [])
    return [k for k in keys if isinstance(k, str) and k.strip()]


def public_get_url(job: dict[str, Any]) -> str | None:
    for row in job.get("asset_urls") or []:
        if not isinstance(row, dict):
            continue
        url = row.get("get_url") or row.get("getUrl")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def source_specs(job: dict[str, Any]) -> list[str]:
    specs = list(local_keys(job))
    for row in job.get("asset_urls") or []:
        if isinstance(row, str) and row.strip():
            specs.append(row.strip())
            continue
        if not isinstance(row, dict):
            continue
        get_url = row.get("get_url") or row.get("getUrl")
        r2_key = row.get("r2_key") or row.get("r2Key")
        if isinstance(get_url, str) and get_url.strip():
            specs.append(get_url.strip())
        elif isinstance(r2_key, str) and Path(r2_key).is_file():
            specs.append(r2_key)
    return specs


def has_media(job: dict[str, Any]) -> bool:
    return bool(source_specs(job))


def ingest(job: dict[str, Any], work_dir: Path) -> list[Path]:
    specs = source_specs(job)
    canned = bool(job.get("canned")) or str(job.get("job_id", "")).startswith("fixture-")
    pipeline = job.get("pipeline") or ""
    dest_dir = work_dir / "project" / "footage"
    files: list[Path] = []
    for i, key in enumerate(specs):
        resolved = resolve_key(key, dest_dir, canned=canned, index=i)
        if resolved and resolved.is_file():
            files.append(resolved)
    if not files and canned and pipeline in FOOTAGE_PIPELINES:
        files.append(_dummy_clip(dest_dir / "source_00.mp4"))
    return files
