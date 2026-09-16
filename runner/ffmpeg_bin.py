"""Resolve ffmpeg / ffprobe for local demos and the SaaS runner."""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from runner.config import repo_root

GYAN_ESSENTIALS = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _local_bin() -> Path:
    return repo_root() / "work" / "tools" / "ffmpeg" / "bin"


def _candidates(name: str) -> list[Path]:
    exe = _exe(name)
    env_key = "FFMPEG_BINARY" if name == "ffmpeg" else "FFPROBE_BINARY"
    env = os.environ.get(env_key) or os.environ.get(name.upper())
    paths: list[Path] = []
    if env:
        paths.append(Path(env))
    paths.append(_local_bin() / exe)
    which = shutil.which(name)
    if which:
        paths.append(Path(which))
    paths.extend(
        [
            Path(r"C:\ProgramData\chocolatey\bin") / exe,
            Path(r"C:\ffmpeg\bin") / exe,
            Path(r"C:\Program Files\ffmpeg\bin") / exe,
        ]
    )
    tools = repo_root() / "work" / "tools"
    if tools.is_dir():
        paths.extend(sorted(tools.glob(f"**/bin/{exe}")))
    return paths


def _imageio_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        pass
    return None


def find_ffmpeg() -> str | None:
    for path in _candidates("ffmpeg"):
        if path.is_file():
            return str(path)
    return _imageio_ffmpeg()


def find_ffprobe() -> str | None:
    for path in _candidates("ffprobe"):
        if path.is_file():
            return str(path)
    ff = find_ffmpeg()
    if ff:
        sibling = Path(ff).with_name(_exe("ffprobe"))
        if sibling.is_file():
            return str(sibling)
    return None


def prepend_to_path() -> str | None:
    """Put the resolved ffmpeg bin first so subprocess `ffmpeg`/`ffprobe` work."""
    ff = find_ffmpeg()
    if not ff:
        return None
    bin_dir = str(Path(ff).parent)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    return ff


def pin_scratch_temp(path: Path | None = None) -> Path:
    """Keep Remotion/HyperFrames/Chrome temp off tiny C: drives (Windows)."""
    target = path or (repo_root() / "work" / "tmp")
    target.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(target)
    os.environ["TMP"] = str(target)
    os.environ["TMPDIR"] = str(target)
    import tempfile

    tempfile.tempdir = str(target)
    return target


def ensure_ffmpeg() -> str:
    """Find ffmpeg+ffprobe, or download a Windows essentials build under work/tools/.

    imageio-ffmpeg is encode-only (no ffprobe). Prefer a real pair so gates can run.
    """
    found = prepend_to_path()
    if found and find_ffprobe():
        return found
    if os.name != "nt":
        raise FileNotFoundError("ffmpeg/ffprobe not on PATH. Install ffmpeg, then retry.")
    dest_bin = _local_bin()
    dest_bin.mkdir(parents=True, exist_ok=True)
    zip_path = repo_root() / "work" / "tools" / "ffmpeg-essentials.zip"
    extract_dir = repo_root() / "work" / "tools" / "ffmpeg-extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    if not zip_path.is_file() or zip_path.stat().st_size < 1_000_000:
        import urllib.request

        print(f"Downloading ffmpeg essentials -> {zip_path}")
        urllib.request.urlretrieve(GYAN_ESSENTIALS, zip_path)
    print(f"Extracting {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    found_exe: Path | None = None
    for cand in extract_dir.rglob("ffmpeg.exe"):
        found_exe = cand
        break
    if not found_exe:
        raise FileNotFoundError("downloaded zip did not contain ffmpeg.exe")
    src_bin = found_exe.parent
    for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
        src = src_bin / name
        if src.is_file():
            shutil.copy2(src, dest_bin / name)
    found = prepend_to_path()
    if found and find_ffprobe():
        return found
    raise FileNotFoundError("ffmpeg install failed (need ffmpeg + ffprobe)")
