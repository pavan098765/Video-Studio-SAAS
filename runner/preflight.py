"""Runtime doctors for Remotion, HyperFrames, Motion Canvas, and FFmpeg."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from runner.config import repo_root
from runner.ffmpeg_bin import find_ffmpeg, find_ffprobe, prepend_to_path

_CACHE: dict[str, bool] | None = None


def reset_doctors() -> None:
    global _CACHE
    _CACHE = None


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        return shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe")
    return None


def ffmpeg_ok() -> bool:
    prepend_to_path()
    return bool(find_ffmpeg() and find_ffprobe())


def remotion_ok() -> bool:
    composer = repo_root() / "remotion-composer"
    return bool(_which("npx") and composer.is_dir() and (composer / "node_modules").is_dir())


def hyperframes_ok() -> bool:
    if not _which("npx") or not ffmpeg_ok():
        return False
    try:
        proc = subprocess.run(
            ["npx", "--no-install", "hyperframes", "--help"],
            capture_output=True,
            text=True,
            timeout=12,
            shell=(os.name == "nt"),
        )
        return proc.returncode == 0
    except Exception:
        return False


def motion_canvas_ok() -> bool:
    adapter = repo_root() / "tools" / "video" / "mc_adapter"
    return bool(
        _which("npx")
        and adapter.is_dir()
        and (adapter / "package.json").is_file()
        and (adapter / "node_modules").is_dir()
    )


def python_deps_ok() -> tuple[bool, str]:
    missing: list[str] = []
    for mod in ("yaml", "dotenv", "pydantic", "requests", "jsonschema"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return False, "pip install -r requirements.txt  (missing: " + ", ".join(missing) + ")"
    return True, sys.executable


def tts_ready() -> tuple[bool, str]:
    if (os.environ.get("ELEVENLABS_API_KEY") or "").strip():
        return True, "elevenlabs"
    if _which("piper"):
        return True, "piper"
    return False, "set ELEVENLABS_API_KEY or pip install piper-tts"


def ensure_npm_install(project: Path) -> None:
    """Install node_modules for a local package.json project if missing."""
    if not project.is_dir() or not (project / "package.json").is_file():
        raise FileNotFoundError(f"no package.json at {project}")
    if (project / "node_modules").is_dir():
        return
    npm = _which("npm")
    if not npm:
        raise FileNotFoundError(
            f"npm not on PATH. Install Node.js, then: cd {project} && npm install"
        )
    print(f"npm install -> {project}", flush=True)
    proc = subprocess.run(
        [npm, "install"],
        cwd=str(project),
        shell=(os.name == "nt"),
        timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"npm install failed in {project} (exit {proc.returncode})")
    if not (project / "node_modules").is_dir():
        raise RuntimeError(f"npm install did not create node_modules in {project}")


def ensure_demo_machine() -> None:
    """ffmpeg (+ Windows download) and Remotion / Motion Canvas npm installs."""
    from runner.ffmpeg_bin import ensure_ffmpeg

    ensure_ffmpeg()
    root = repo_root()
    ensure_npm_install(root / "remotion-composer")
    ensure_npm_install(root / "tools" / "video" / "mc_adapter")
    reset_doctors()


def machine_report() -> list[dict[str, str]]:
    """Rows for demo_studio --doctor. status is ok or miss."""
    py_ok, py_detail = python_deps_ok()
    tts_ok, tts_detail = tts_ready()
    node = _which("node")
    npm = _which("npm")
    npx = _which("npx")
    remotion = remotion_ok()
    mc = motion_canvas_ok()
    ffmpeg = ffmpeg_ok()
    return [
        {"id": "python", "status": "ok" if py_ok else "miss", "detail": py_detail},
        {"id": "tts", "status": "ok" if tts_ok else "miss", "detail": tts_detail},
        {
            "id": "node",
            "status": "ok" if node else "miss",
            "detail": node or "install Node.js LTS, then reopen the terminal",
        },
        {
            "id": "npm",
            "status": "ok" if npm else "miss",
            "detail": npm or "install Node.js (includes npm)",
        },
        {
            "id": "npx",
            "status": "ok" if npx else "miss",
            "detail": npx or "npx must be on PATH (ships with Node.js)",
        },
        {
            "id": "remotion",
            "status": "ok" if remotion else "miss",
            "detail": "remotion-composer/node_modules"
            if remotion
            else "python scripts/demo_studio.py --setup",
        },
        {
            "id": "motion_canvas",
            "status": "ok" if mc else "miss",
            "detail": "tools/video/mc_adapter/node_modules"
            if mc
            else "python scripts/demo_studio.py --setup",
        },
        {
            "id": "ffmpeg",
            "status": "ok" if ffmpeg else "miss",
            "detail": (find_ffmpeg() or "python scripts/demo_studio.py --setup"),
        },
    ]


def machine_ok(rows: list[dict[str, str]] | None = None) -> bool:
    return all(row["status"] == "ok" for row in (rows or machine_report()))


def doctors() -> dict[str, bool]:
    global _CACHE
    if _CACHE is not None:
        return dict(_CACHE)
    _CACHE = {
        "ffmpeg": ffmpeg_ok(),
        "remotion": remotion_ok(),
        "hyperframes": hyperframes_ok(),
        "motion_canvas": motion_canvas_ok(),
    }
    return dict(_CACHE)


def require_runtime(runtime: str, *, motion_required: bool = False) -> dict[str, Any]:
    info = doctors()
    ok = {
        "remotion": info["remotion"],
        "hyperframes": info["hyperframes"],
        "motion_canvas": info["motion_canvas"],
        "ffmpeg": info["ffmpeg"],
    }.get(runtime, False)
    if ok:
        return {"ok": True, "doctors": info, "runtime": runtime}
    if motion_required:
        raise RuntimeError(
            f"doctor fail: {runtime} unavailable; refusing silent slideshow (motion_required)"
        )
    return {"ok": False, "doctors": info, "runtime": runtime, "error": f"{runtime} doctor failed"}
