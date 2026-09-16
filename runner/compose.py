"""Per-scene compositor dispatch. Never writes production color cards."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from runner import tools_exec
from runner.edl import PICTURE_SCENE_TYPES, captions_from_timestamps, load_word_timestamps, terminal_steps_from_script
from runner.ffmpeg_bin import find_ffmpeg, find_ffprobe, prepend_to_path
from runner.mode import output_profile, platform_size, screen_is_gui
from runner.preflight import remotion_ok


class ComposeError(RuntimeError):
    pass


def _ffmpeg() -> str:
    prepend_to_path()
    ff = find_ffmpeg()
    if not ff:
        raise ComposeError("ffmpeg missing")
    return ff


def _media_duration(path: Path) -> float:
    probe = find_ffprobe()
    if not probe or not path.is_file():
        return 0.0
    proc = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float((proc.stdout or "").strip() or 0)
    except ValueError:
        return 0.0


def _has_audio(path: Path) -> bool:
    probe = find_ffprobe()
    if not probe or not path.is_file():
        return False
    proc = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool((proc.stdout or "").strip())


def _mean_volume_db(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        ff = _ffmpeg()
    except ComposeError:
        return None
    proc = subprocess.run(
        [ff, "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    blob = (proc.stderr or "") + (proc.stdout or "")
    for line in blob.splitlines():
        if "mean_volume:" not in line:
            continue
        try:
            return float(line.split("mean_volume:")[1].split("dB")[0].strip())
        except (IndexError, ValueError):
            return None
    return None


def _audible(path: Path) -> bool:
    """True only when an audio stream exists and is louder than digital silence.

    Remotion often writes a silent AAC track. Treating that as 'has audio'
    skipped VO mux and canned sine, producing finals below the volume gate.
    """
    if not _has_audio(path):
        return False
    vol = _mean_volume_db(path)
    if vol is None:
        return False
    return vol > -50.0


VF = (
    "scale=1920:1080:force_original_aspect_ratio=decrease:out_color_matrix=bt709:out_range=tv,"
    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,eq=contrast=1.02,format=yuv420p"
)


def mux_audio(
    video: Path,
    audio: Path | None,
    dest: Path,
    seconds: int,
    *,
    allow_sine: bool = False,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    ff = _ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:out_color_matrix=bt709:out_range=tv,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,eq=contrast=1.02,format=yuv420p"
    )
    cmd = [ff, "-y", "-i", str(video)]
    if audio and audio.is_file():
        cmd += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
    elif allow_sine:
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={seconds}:sample_rate=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    else:
        raise ComposeError("narration missing; refusing sine mux")
    cmd += [
        "-t",
        str(seconds),
        "-vf",
        vf,
        "-af",
        "aresample=48000",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-c:a",
        "aac",
        "-shortest",
        str(dest),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dest


def normalize_clip(src: Path, dest: Path, seconds: int, *, width: int = 1920, height: int = 1080) -> Path:
    ff = _ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:out_color_matrix=bt709:out_range=tv,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,eq=contrast=1.02,format=yuv420p"
    )
    subprocess.check_call(
        [
            ff,
            "-y",
            "-i",
            str(src),
            "-t",
            str(max(1, seconds)),
            "-vf",
            vf,
            "-af",
            "aresample=48000",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return dest


def _encode_delivery(src: Path, dest: Path) -> Path:
    """Normalize a single master to the delivery encode. Not a stitch."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        return dest
    ff = _ffmpeg()
    subprocess.check_call(
        [
            ff,
            "-y",
            "-i",
            str(src),
            "-vf",
            VF,
            "-af",
            "aresample=48000",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-c:a",
            "aac",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return dest


def assemble_final(
    *,
    strategy: str,
    scene_files: list[Path],
    dest: Path,
    master: Path | None = None,
) -> Path:
    """OpenMontage compose: one runtime timeline is the final; stitch only mixed engines."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    from runner import progress

    progress.note(f"assemble_final strategy={strategy} clips={len(scene_files)} dest={dest.name}", kind="render")
    clips = [p for p in scene_files if p.is_file() and p.stat().st_size > 32]
    if strategy == "single_runtime":
        src = master if master is not None and master.is_file() and master.stat().st_size > 32 else None
        if src is None and len(clips) == 1:
            src = clips[0]
        if src is None and len(clips) >= 2:
            return stitch(clips, dest)
        if src is None:
            raise ComposeError("single_runtime has no master to deliver")
        return _encode_delivery(src, dest)
    if len(clips) < 2:
        raise ComposeError("scene_assemble requires at least 2 clips")
    return stitch(clips, dest)


def stitch(scene_files: list[Path], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    clips = [p for p in scene_files if p.is_file() and p.stat().st_size > 32]
    if len(clips) < 2:
        raise ComposeError("scene_assemble requires at least 2 clips")
    raw = dest.with_suffix(".concat.mp4")
    result = tools_exec.execute(
        "video_stitch",
        {
            "operation": "stitch",
            "clips": [str(p) for p in clips],
            "output_path": str(raw),
            "auto_normalize": True,
            "transition": "cut",
        },
    )
    src = raw if raw.is_file() and raw.stat().st_size > 32 else None
    if not src:
        ff = _ffmpeg()
        lst = dest.parent / "concat.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in clips), encoding="utf-8")
        subprocess.check_call(
            [ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(raw)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        src = raw
    return _encode_delivery(src, dest)


def mix_music_under_vo(
    video: Path,
    vo: Path,
    music: Path,
    dest: Path,
    seconds: int,
    *,
    width: int = 1920,
    height: int = 1080,
) -> Path | None:
    """Duck a music bed under narration and replace the stitched video's audio."""
    bed = dest.with_name(dest.stem + "_bed.wav")
    mix = tools_exec.execute(
        "audio_mixer",
        {
            "operation": "full_mix",
            "tracks": [
                {"path": str(vo), "role": "speech"},
                {"path": str(music), "role": "music", "volume": 0.3},
            ],
            "ducking": {"enabled": True, "music_volume_during_speech": 0.15},
            "normalize": True,
            "output_path": str(bed),
            "target_duration": float(max(1, seconds)),
        },
    )
    if not mix.success or not bed.is_file():
        return None
    mux_audio(video, bed, dest, seconds, allow_sine=False, width=width, height=height)
    return dest if dest.is_file() and dest.stat().st_size > 32 else None


def split_master(master: Path, scenes: list[dict[str, Any]], dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    ff = _ffmpeg()
    files: list[Path] = []
    for scene in scenes:
        sid = scene.get("id") or f"sc{len(files)+1}"
        start = float(scene.get("start_seconds") or 0)
        end = float(scene.get("end_seconds") or (start + 1))
        dur = max(0.2, end - start)
        dest = dest_dir / f"{sid}.mp4"
        subprocess.check_call(
            [
                ff,
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                str(master),
                "-vf",
                VF,
                "-af",
                "aresample=48000",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        files.append(dest)
    return files


MC_INSERT_TYPES = {"diagram", "map", "etymology", "code", "math", "timeline"}


def _timestamps_for_scene(asset_manifest: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    timestamps: list[dict[str, Any]] = []
    for row in load_word_timestamps(asset_manifest):
        if not isinstance(row, dict):
            continue
        try:
            word_end = float(row.get("end") or row.get("end_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if word_end < start or word_end > end + 0.05:
            continue
        item = dict(row)
        item["end"] = word_end - start
        try:
            item["start"] = max(0.0, float(item.get("start") or 0) - start)
        except (TypeError, ValueError):
            pass
        timestamps.append(item)
    return timestamps


def insert_motion_canvas(
    *,
    dest_dir: Path,
    scene_plan: dict[str, Any],
    asset_manifest: dict[str, Any],
    topic: str,
) -> dict[str, Any]:
    """Render MC inserts into assets; Explainer/atelier then play them as backgroundVideo."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    assets = list(asset_manifest.get("assets") or [])
    for scene in scene_plan.get("scenes") or []:
        stype = str(scene.get("type") or "").lower()
        if stype not in MC_INSERT_TYPES:
            continue
        sid = str(scene.get("id") or "sc")
        seconds = max(1, int((scene.get("end_seconds") or 4) - (scene.get("start_seconds") or 0)))
        start = float(scene.get("start_seconds") or 0)
        end = float(scene.get("end_seconds") or (start + seconds))
        ws = dest_dir / f"mc_{sid}"
        tmp = dest_dir / f"mc_{sid}.mp4"
        timestamps = _timestamps_for_scene(asset_manifest, start, end)
        mermaid = _asset_meta(asset_manifest, sid, "mermaid") or str(scene.get("mermaid") or "")
        if stype in {"diagram", "map", "etymology", "timeline"} and not mermaid.strip():
            meta = asset_manifest.setdefault("metadata", {})
            skips = list(meta.get("mc_skips") or [])
            skips.append(sid)
            meta["mc_skips"] = skips
            continue
        gen_inputs: dict[str, Any] = {
            "operation": "generate",
            "workspace_path": str(ws),
            "scene_type": stype,
            "title": str(scene.get("description") or topic),
            "duration_seconds": seconds,
            "timestamps": timestamps,
            "mermaid": mermaid,
            "code_text": _asset_meta(asset_manifest, sid, "code") or str(scene.get("code_snippet") or ""),
            "formula": _asset_meta(asset_manifest, sid, "formula") or str(scene.get("formula_tex") or ""),
            "allow_mc_fixture": False,
        }
        generated = tools_exec.execute("motion_canvas_compose", gen_inputs)
        if not generated.success:
            meta = asset_manifest.setdefault("metadata", {})
            skips = list(meta.get("mc_skips") or [])
            skips.append(sid)
            meta["mc_skips"] = skips
            continue
        rendered = tools_exec.execute(
            "motion_canvas_compose",
            {**gen_inputs, "operation": "render", "output_path": str(tmp)},
        )
        src = Path(rendered.artifacts[0]) if rendered.success and rendered.artifacts else tmp
        if not rendered.success or not src.is_file() or src.stat().st_size < 32:
            meta = asset_manifest.setdefault("metadata", {})
            skips = list(meta.get("mc_skips") or [])
            skips.append(sid)
            meta["mc_skips"] = skips
            continue
        assets.append(
            {
                "id": f"mc_{sid}",
                "type": "animation",
                "path": str(src),
                "source_tool": "motion_canvas_compose",
                "scene_id": sid,
            }
        )
    updated = dict(asset_manifest)
    updated["assets"] = assets
    return updated


def _first_asset(manifest: dict[str, Any], *, types: set[str], scene_id: str | None = None) -> Path | None:
    shared = {None, "", "all", "*"}
    for row in manifest.get("assets") or []:
        if row.get("type") not in types:
            continue
        asset_sid = row.get("scene_id")
        if scene_id and asset_sid not in {scene_id, *shared}:
            continue
        path = Path(str(row.get("path") or ""))
        if path.is_file():
            return path
    if scene_id:
        return None
    for row in manifest.get("assets") or []:
        if row.get("type") in types:
            path = Path(str(row.get("path") or ""))
            if path.is_file():
                return path
    return None


def _asset_meta(manifest: dict[str, Any], scene_id: str, key: str) -> str:
    meta = manifest.get("metadata") or {}
    bucket = meta.get(key)
    if isinstance(bucket, dict) and scene_id in bucket and bucket[scene_id]:
        return str(bucket[scene_id])
    for row in manifest.get("assets") or []:
        if row.get("scene_id") == scene_id and row.get(key):
            return str(row.get(key))
    return ""


def _captions(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return captions_from_timestamps(load_word_timestamps(manifest))


def _size(prefs: dict[str, Any] | None) -> tuple[int, int]:
    return platform_size((prefs or {}).get("platform_profile"))


def _remotion(
    composition_id: str,
    props: dict[str, Any],
    dest: Path,
    seconds: int,
    *,
    width: int = 1920,
    height: int = 1080,
    profile: str | None = None,
) -> Path:
    if not remotion_ok():
        raise ComposeError("remotion doctor failed")
    frames = max(30, int(seconds * 30))
    result = tools_exec.execute(
        "video_compose",
        {
            "operation": "remotion_render",
            "composition_id": composition_id,
            "composition_data": props,
            "output_path": str(dest),
            "duration_frames": frames,
            "width": width,
            "height": height,
            "profile": profile,
        },
    )
    if not result.success or not dest.is_file():
        raise ComposeError(result.error or "remotion_render failed")
    return dest


def render_explainer(
    *,
    dest: Path,
    edit: dict[str, Any],
    asset_manifest: dict[str, Any],
    seconds: int,
    prefs: dict[str, Any] | None = None,
) -> Path:
    width, height = _size(prefs)
    payload = json.loads(json.dumps(edit))
    payload.setdefault("captions", _captions(asset_manifest))
    result = tools_exec.execute(
        "video_compose",
        {
            "operation": "render",
            "edit_decisions": payload,
            "asset_manifest": asset_manifest,
            "output_path": str(dest),
            "output_profile": output_profile((prefs or {}).get("platform_profile")),
            "width": width,
            "height": height,
            "duration_frames": max(30, int(seconds * 30)),
        },
    )
    if not result.success or not dest.is_file():
        # Direct Explainer remotion_render if high-level render failed on review checks.
        _remotion("Explainer", payload, dest, seconds, width=width, height=height)
    return dest


def render_cinematic(
    *,
    dest: Path,
    edit: dict[str, Any],
    seconds: int,
    prefs: dict[str, Any] | None = None,
) -> Path:
    width, height = _size(prefs)
    _remotion(
        "CinematicRenderer",
        edit,
        dest,
        seconds,
        width=width,
        height=height,
        profile=output_profile((prefs or {}).get("platform_profile")),
    )
    return dest


def ensure_audio(
    dest: Path,
    audio: Path | None,
    seconds: int,
    *,
    canned: bool,
    width: int = 1920,
    height: int = 1080,
) -> str | None:
    """Attach real audio when the file is silent. Returns 'sine_mux' only for canned tests."""
    if _audible(dest):
        return None
    if audio and audio.is_file():
        tmp = dest.with_suffix(".aud.mp4")
        mux_audio(dest, audio, tmp, seconds, allow_sine=False, width=width, height=height)
        shutil.move(str(tmp), str(dest))
        return None
    if canned:
        tmp = dest.with_suffix(".aud.mp4")
        mux_audio(dest, None, tmp, seconds, allow_sine=True, width=width, height=height)
        shutil.move(str(tmp), str(dest))
        return "sine_mux"
    raise ComposeError("rendered video has no audio")


def render_scene(
    *,
    dest: Path,
    scene: dict[str, Any],
    runtime: str,
    pipeline: str,
    topic: str,
    asset_manifest: dict[str, Any],
    seconds: int,
    character_workspace: str | None = None,
    script: dict[str, Any] | None = None,
    canned: bool = False,
    prefs: dict[str, Any] | None = None,
    edit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    from runner import progress

    progress.note(
        f"scene {scene.get('id') or dest.stem} runtime={runtime} {seconds}s -> {dest.name}",
        kind="render",
    )
    stype = (scene.get("type") or "").lower()
    title = str(scene.get("description") or topic)
    audio = _first_asset(asset_manifest, types={"narration", "audio"})
    width, height = _size(prefs)
    generator = runtime
    tmp = dest.with_suffix(".raw.mp4")
    captions = _captions(asset_manifest)

    if runtime == "ffmpeg":
        src = _first_asset(asset_manifest, types={"video"}, scene_id=scene.get("id"))
        if not src:
            src = _first_asset(asset_manifest, types={"video"})
        if not src:
            raise ComposeError(f"ffmpeg scene {scene.get('id')} has no footage")
        trimmed = dest.with_suffix(".trim.mp4")
        start_s = float(scene.get("start_seconds") or 0)
        end_s = float(scene.get("end_seconds") or (start_s + seconds))
        cut = tools_exec.execute(
            "video_trimmer",
            {
                "operation": "cut",
                "input_path": str(src),
                "output_path": str(trimmed),
                "start_seconds": start_s,
                "end_seconds": max(start_s + 0.2, end_s),
                "codec": "libx264",
            },
        )
        clip = Path(cut.artifacts[0]) if cut.success and cut.artifacts else src
        if not clip.is_file():
            clip = src
        if pipeline == "localization-dub" and audio:
            mux_audio(clip, audio, dest, seconds, allow_sine=canned, width=width, height=height)
        else:
            normalize_clip(clip, dest, seconds, width=width, height=height)
            ensure_audio(dest, audio, seconds, canned=canned, width=width, height=height)
        meta = {"render_runtime": "ffmpeg", "generator": "ffmpeg"}
        dest.with_suffix(".runtime.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    if runtime == "hyperframes":
        workspace = Path(character_workspace or dest.parent / "hf")
        html = workspace / "index.html"
        if not html.is_file():
            raise ComposeError("hyperframes workspace missing index.html")
        result = tools_exec.execute(
            "hyperframes_compose",
            {
                "operation": "render_existing",
                "workspace_path": str(workspace),
                "output_path": str(tmp),
                "skip_contrast": True,
                "strict_check": False,
                "quality": "draft",
            },
        )
        src = Path(result.artifacts[0]) if result and result.success and result.artifacts else tmp
        if not result or not result.success or not src.is_file() or src.stat().st_size < 32:
            raise ComposeError(result.error if result else "hyperframes render failed")
        if audio:
            mux_audio(src, audio, dest, seconds, allow_sine=False, width=width, height=height)
        else:
            if src != dest:
                shutil.copy2(src, dest)
            ensure_audio(dest, audio, seconds, canned=canned, width=width, height=height)
        meta = {"render_runtime": "hyperframes", "generator": "hyperframes"}
        dest.with_suffix(".runtime.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    if runtime == "motion_canvas":
        ws = dest.parent / f"mc_{scene.get('id') or 'scene'}"
        sid = str(scene.get("id") or "")
        start = float(scene.get("start_seconds") or 0)
        timestamps = _timestamps_for_scene(asset_manifest, start, start + seconds)
        mermaid = _asset_meta(asset_manifest, sid, "mermaid") or str(scene.get("mermaid") or "")
        gen_inputs: dict[str, Any] = {
            "operation": "generate",
            "workspace_path": str(ws),
            "scene_type": stype,
            "title": title,
            "duration_seconds": seconds,
            "timestamps": timestamps,
            "mermaid": mermaid,
            "code_text": _asset_meta(asset_manifest, sid, "code") or str(scene.get("code_snippet") or ""),
            "formula": _asset_meta(asset_manifest, sid, "formula") or str(scene.get("formula_tex") or ""),
            "allow_mc_fixture": False,
        }
        tools_exec.execute("motion_canvas_compose", gen_inputs)
        rendered = tools_exec.execute(
            "motion_canvas_compose",
            {**gen_inputs, "operation": "render", "output_path": str(tmp)},
        )
        if rendered.success and (tmp.is_file() or (rendered.artifacts and Path(rendered.artifacts[0]).is_file())):
            src = Path(rendered.artifacts[0]) if rendered.artifacts else tmp
            if audio:
                mux_audio(src, audio, dest, seconds, allow_sine=False, width=width, height=height)
            else:
                shutil.copy2(src, dest)
                ensure_audio(dest, audio, seconds, canned=canned, width=width, height=height)
            meta = {
                "render_runtime": "motion_canvas",
                "generator": "motion_canvas",
                "waitUntil": True,
                "workspace": str(ws),
            }
            dest.with_suffix(".runtime.json").write_text(json.dumps(meta), encoding="utf-8")
            return meta
        salvage = _first_asset(asset_manifest, types={"animation"}, scene_id=sid)
        if salvage and salvage.is_file():
            if audio:
                mux_audio(salvage, audio, dest, seconds, allow_sine=False, width=width, height=height)
            else:
                normalize_clip(salvage, dest, seconds, width=width, height=height)
                ensure_audio(dest, audio, seconds, canned=canned, width=width, height=height)
            meta = {
                "render_runtime": "motion_canvas",
                "generator": "picture_passthrough",
                "waitUntil": True,
                "workspace": str(ws),
            }
            dest.with_suffix(".runtime.json").write_text(json.dumps(meta), encoding="utf-8")
            return meta
        raise ComposeError(f"motion canvas failed for {sid}; refusing Explainer fallback")

    if runtime == "remotion":
        if stype == "end_tag":
            props = {"text": title.upper()[:80], "palette": "cool_offwhite_on_black"}
            _remotion("EndTag", props, tmp, seconds, width=width, height=height)
        elif stype == "screen_recording" or pipeline == "screen-demo":
            if screen_is_gui(topic, scene):
                still = _first_asset(asset_manifest, types={"image", "diagram"})
                props = {
                    "cuts": [
                        {
                            "id": scene.get("id") or "gui",
                            "source": str(still) if still else "generated",
                            "in_seconds": 0,
                            "out_seconds": seconds,
                            "type": "screenshot_scene",
                            "text": title[:120],
                            "screenshotSteps": [
                                {"label": title[:40], "holdSeconds": max(0.4, seconds / 3)},
                            ],
                        }
                    ],
                    "captions": captions,
                }
                _remotion("Explainer", props, tmp, seconds, width=width, height=height)
            else:
                props = {"title": topic[:48], "steps": terminal_steps_from_script(script, topic)}
                _remotion("TerminalScene", props, tmp, seconds, width=width, height=height)
        elif stype == "talking_head" or pipeline == "talking-head":
            video = _first_asset(asset_manifest, types={"video"})
            if not video:
                raise ComposeError("TalkingHead needs footage")
            src_dur = _media_duration(video)
            talk_seconds = seconds
            if src_dur > 0:
                talk_seconds = max(1, min(seconds, max(1, int(src_dur) - 1)))
            safe = dest.with_suffix(".thsrc.mp4")
            subprocess.check_call(
                [
                    _ffmpeg(),
                    "-y",
                    "-i",
                    str(video),
                    "-t",
                    str(talk_seconds),
                    "-vf",
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    "30",
                    "-movflags",
                    "+faststart",
                    "-an",
                    str(safe),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            props = {"videoSrc": str(safe), "captions": captions, "overlays": []}
            _remotion("TalkingHead", props, tmp, talk_seconds, width=width, height=height)
            seconds = talk_seconds
            if not audio:
                audio = video
        elif pipeline == "cinematic":
            payload = edit or {"cuts": [], "renderer_family": "cinematic-trailer"}
            _remotion("CinematicRenderer", payload, tmp, seconds, width=width, height=height)
        else:
            cut = None
            for row in (edit or {}).get("cuts") or []:
                if str(row.get("id")) == str(scene.get("id")):
                    cut = dict(row)
                    break
            if cut is None:
                cut = {
                    "id": scene.get("id") or "cut",
                    "source": "generated",
                    "in_seconds": 0,
                    "out_seconds": seconds,
                    "type": "text_card",
                    "text": title[:180],
                    "title": topic[:80],
                    "heroSubtitle": title[:120],
                }
            cut.setdefault("out_seconds", seconds)
            cut["in_seconds"] = 0
            cut["out_seconds"] = seconds
            cut_type = str(cut.get("type") or "")
            catalog_ok = (
                (cut_type == "bar_chart" and cut.get("chartData"))
                or (cut_type == "stat_card" and cut.get("stat"))
                or (cut_type == "terminal_scene" and cut.get("steps"))
                or (cut_type == "hero_title" and cut.get("text"))
                or (cut_type == "screenshot_scene")
                or (
                    cut_type in {"text_card", "callout"}
                    and stype not in PICTURE_SCENE_TYPES
                    and cut.get("text")
                )
            )
            picture_src = Path(str(cut.get("source") or ""))
            if catalog_ok:
                audio_props: dict[str, Any] = {}
                props = {"cuts": [cut], "captions": captions, "audio": audio_props}
                _remotion("Explainer", props, tmp, seconds, width=width, height=height)
            elif stype in PICTURE_SCENE_TYPES or cut_type == "picture":
                if picture_src.is_file() and picture_src.suffix.lower() in {".mp4", ".webm", ".mov", ".m4v"}:
                    if audio:
                        mux_audio(picture_src, audio, dest, seconds, allow_sine=False, width=width, height=height)
                    else:
                        normalize_clip(picture_src, dest, seconds, width=width, height=height)
                        ensure_audio(dest, audio, seconds, canned=canned, width=width, height=height)
                    meta = {
                        "render_runtime": "remotion",
                        "generator": "picture_passthrough",
                        "waitUntil": stype in {"diagram", "map", "code", "math", "timeline", "etymology"},
                    }
                    dest.with_suffix(".runtime.json").write_text(json.dumps(meta), encoding="utf-8")
                    return meta
                if picture_src.is_file() and picture_src.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    still_cut = dict(cut)
                    still_cut["type"] = "picture"
                    still_cut["text"] = ""
                    still_cut["source"] = str(picture_src)
                    still_cut.pop("backgroundImage", None)
                    still_cut.pop("backgroundVideo", None)
                    _remotion("Explainer", {"cuts": [still_cut], "captions": captions}, tmp, seconds, width=width, height=height)
                    generator = "picture_passthrough"
                else:
                    raise ComposeError(
                        f"picture scene {scene.get('id')} has no clip; refusing Explainer title"
                    )
            else:
                audio_props = {}
                props = {"cuts": [cut], "captions": captions, "audio": audio_props}
                _remotion("Explainer", props, tmp, seconds, width=width, height=height)
        src = tmp if tmp.is_file() else dest
        if src != dest:
            shutil.copy2(src, dest)
        ensure_audio(dest, audio, seconds, canned=canned, width=width, height=height)
        meta = {
            "render_runtime": "remotion",
            "generator": generator,
            "waitUntil": stype in {"diagram", "map", "code", "math", "timeline", "etymology"},
        }
        dest.with_suffix(".runtime.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    raise ComposeError(f"unsupported runtime {runtime}")
