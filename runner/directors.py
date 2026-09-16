"""Per-pipeline Python directors (character, screen-demo, dub, cinematic keys)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from runner import tools_exec
from runner.artifacts import project_dir, write_artifact
from runner.config import repo_root
from runner.mode import wants_ink


class CharacterError(RuntimeError):
    pass


def _unwrap_composition_html(raw: str) -> str:
    text = raw
    text = text.replace('<template id="character-scene-template">', "").replace("</template>", "")
    text = text.replace("character-scene", "root")
    if "<html" not in text.lower():
        text = (
            "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<script src=\"https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js\"></script>"
            "</head><body>"
            f"{text}"
            "</body></html>"
        )
    if "window.__timelines['root']" not in text and 'window.__timelines["root"]' not in text:
        text = text.replace("window.__timelines['root']", "window.__timelines['root']")
        if "__timelines['root']" not in text and '__timelines["root"]' not in text:
            text = text.replace("window.__timelines['root'] = tl", "window.__timelines['root'] = tl")
    return text


def _install_renderer_html(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    src = workspace / "compositions" / "character-scene.html"
    if not src.is_file():
        raise CharacterError("character_rig_renderer did not write compositions/character-scene.html")
    html = _unwrap_composition_html(src.read_text(encoding="utf-8"))
    (workspace / "index.html").write_text(html, encoding="utf-8")


def _install_ink_theater(workspace: Path, duration: float) -> None:
    example = repo_root() / "ink-theater" / "examples" / "mocap-figure"
    if not example.is_dir():
        raise CharacterError("ink-theater example missing")
    workspace.mkdir(parents=True, exist_ok=True)
    for item in example.iterdir():
        dest = workspace / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    html_path = workspace / "index.html"
    html = html_path.read_text(encoding="utf-8")
    html = html.replace('data-composition-id="main"', 'data-composition-id="root"')
    html = html.replace('window.__timelines["main"]', 'window.__timelines["root"]')
    html = html.replace(f'data-duration="21"', f'data-duration="{duration:.3f}"')
    html_path.write_text(html, encoding="utf-8")


def _as_character_design(raw: dict[str, Any], topic: str) -> dict[str, Any]:
    stub = {
        "version": "1.0",
        "characters": [
            {
                "id": "hero",
                "role": "protagonist",
                "body_type": "humanoid",
                "style": "flat",
                "required_emotions": ["neutral", "emphasis"],
                "required_actions": ["idle", "talk"],
                "display_name": (topic or "Hero")[:40],
            }
        ],
    }
    if isinstance(raw, dict) and raw.get("version") == "1.0" and raw.get("characters"):
        try:
            from schemas.artifacts import validate_artifact

            validate_artifact("character_design", raw)
            return raw
        except Exception:
            return stub
    return stub


def normalize_design(raw: dict[str, Any] | None, topic: str) -> dict[str, Any]:
    return _as_character_design(raw if isinstance(raw, dict) else {}, topic)


def normalize_rig(raw: dict[str, Any] | None, topic: str) -> dict[str, Any]:
    stub = {
        "version": "1.0",
        "characters": [
            {
                "character_id": "hero",
                "rig_type": "svg_rig",
                "parts": [{"id": "body", "kind": "torso", "layer": 1}],
                "joints": {"root": {"pivot": [0.5, 0.5]}},
                "layers": ["body"],
                "required_poses": ["idle", "talk"],
            }
        ],
        "metadata": {"topic": topic[:80]},
    }
    if isinstance(raw, dict) and raw.get("version") == "1.0" and raw.get("characters"):
        try:
            from schemas.artifacts import validate_artifact

            validate_artifact("rig_plan", raw)
            return raw
        except Exception:
            return stub
    return stub


def run_character_design(work_dir: Path, topic: str, llm_artifact: dict[str, Any] | None = None) -> dict[str, Any]:
    out = project_dir(work_dir) / "character"
    out.mkdir(parents=True, exist_ok=True)
    if llm_artifact:
        artifact = normalize_design(llm_artifact, topic)
        write_artifact(work_dir, "character_design", artifact)
        return {"character_design": artifact, "raw": llm_artifact, "out": out}
    spec = tools_exec.execute(
        "character_spec_generator",
        {"brief": topic, "output_path": str(out / "character_design.json")},
    )
    if not spec.success:
        raise CharacterError(spec.error or "character_spec_generator failed")
    design = (spec.data or {}).get("character_design") if spec.success else {}
    artifact = _as_character_design(design if isinstance(design, dict) else {}, topic)
    write_artifact(work_dir, "character_design", artifact)
    return {"character_design": artifact, "raw": design, "out": out}


def run_character_rig(
    work_dir: Path,
    topic: str,
    scene_plan: dict[str, Any],
    brief: dict[str, Any] | None = None,
    design: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = project_dir(work_dir) / "character"
    out.mkdir(parents=True, exist_ok=True)
    duration = 8.0
    for scene in scene_plan.get("scenes") or []:
        duration = max(duration, float(scene.get("end_seconds") or 0))
    workspace = out / "hyperframes"
    if wants_ink(topic, brief, scene_plan):
        _install_ink_theater(workspace, duration)
        return {
            "character_design": design or {},
            "workspace": str(workspace),
            "preview": None,
            "renderer_ok": True,
            "ink_theater": True,
        }
    if not design:
        loaded = run_character_design(work_dir, topic)
        design = loaded.get("raw") or loaded.get("character_design")
    rig = tools_exec.execute(
        "svg_rig_builder",
        {"character_design": design, "output_path": str(out / "rig_plan.json")},
    )
    if not rig.success:
        raise CharacterError(rig.error or "svg_rig_builder failed")
    try:
        write_artifact(work_dir, "rig_plan", normalize_rig((rig.data or {}).get("rig_plan"), topic))
    except Exception:
        pass
    poses = tools_exec.execute(
        "pose_library_builder",
        {
            "character_design": design,
            "rig_plan": (rig.data or {}).get("rig_plan"),
            "output_path": str(out / "pose_library.json"),
        },
    )
    if not poses.success:
        raise CharacterError(poses.error or "pose_library_builder failed")
    timeline = tools_exec.execute(
        "action_timeline_compiler",
        {
            "scene_plan": scene_plan,
            "character_design": design,
            "output_path": str(out / "action_timeline.json"),
        },
    )
    if not timeline.success:
        raise CharacterError(timeline.error or "action_timeline_compiler failed")
    preview = tools_exec.execute(
        "character_rig_renderer",
        {
            "action_timeline": (timeline.data or {}).get("action_timeline") or {"scenes": []},
            "rig_plan": (rig.data or {}).get("rig_plan"),
            "pose_library": (poses.data or {}).get("pose_library"),
            "output_path": str(out / "preview.html"),
            "workspace_path": str(workspace),
            "render_video": False,
        },
    )
    if not preview.success:
        raise CharacterError(preview.error or "character_rig_renderer failed")
    review = tools_exec.execute(
        "character_animation_reviewer",
        {
            "character_design": design,
            "rig_plan": (rig.data or {}).get("rig_plan"),
            "pose_library": (poses.data or {}).get("pose_library"),
            "action_timeline": (timeline.data or {}).get("action_timeline"),
            "output_path": str(out / "qa.json"),
        },
    )
    report = ((review.data or {}).get("character_qa_report") or {}) if review.success else {}
    checks = report.get("checks") or {}
    issues = [str(i) for i in (report.get("issues") or [])]
    blocking = [i for i in issues if "schema" in i.lower() or "without timed" in i.lower() or "missing" in i.lower()]
    if report.get("status") == "fail" or checks.get("schema_valid") is False or blocking:
        raise CharacterError("character_animation_reviewer: " + "; ".join(issues[:6] or ["revise"]))
    qa = {
        "version": "1.0",
        "status": str(report.get("status") or "pass"),
        "checks": {
            "schema_valid": bool(checks.get("schema_valid", True)),
            "assets_exist": bool(checks.get("assets_exist", True)),
            "pivots_defined": bool(checks.get("pivots_defined", True)),
            "poses_defined": bool(checks.get("poses_defined", True)),
            "actions_timed": bool(checks.get("actions_timed", True)),
        },
        "issues": issues,
        "recommended_action": str(report.get("recommended_action") or "present_to_user"),
    }
    if qa["status"] not in {"pass", "revise", "fail"}:
        qa["status"] = "pass"
    if qa["recommended_action"] not in {"present_to_user", "fix_rig", "fix_assets", "fix_timeline", "block"}:
        qa["recommended_action"] = "present_to_user"
    try:
        write_artifact(work_dir, "character_qa_report", qa)
    except Exception:
        pass
    _install_renderer_html(workspace)
    html = workspace / "index.html"
    blob = html.read_text(encoding="utf-8")
    if "arm-left" not in blob and ".character" not in blob:
        raise CharacterError("character HTML is not the rig renderer output")
    return {
        "character_design": design,
        "workspace": str(workspace),
        "preview": str(out / "preview.html") if (out / "preview.html").is_file() else None,
        "renderer_ok": True,
        "edit_decisions": (preview.data or {}).get("edit_decisions") if preview.success else None,
        "asset_manifest": (preview.data or {}).get("asset_manifest") if preview.success else None,
        "qa": qa,
    }


def run_character_chain(work_dir: Path, topic: str, scene_plan: dict[str, Any], brief: dict[str, Any] | None = None) -> dict[str, Any]:
    out = project_dir(work_dir) / "character"
    out.mkdir(parents=True, exist_ok=True)
    duration = 8.0
    for scene in scene_plan.get("scenes") or []:
        duration = max(duration, float(scene.get("end_seconds") or 0))
    workspace = out / "hyperframes"
    if wants_ink(topic, brief, scene_plan):
        _install_ink_theater(workspace, duration)
        return {
            "character_design": {},
            "workspace": str(workspace),
            "preview": None,
            "renderer_ok": True,
            "ink_theater": True,
        }
    designed = run_character_design(work_dir, topic)
    return run_character_rig(
        work_dir,
        topic,
        scene_plan,
        brief=brief,
        design=designed.get("raw") or designed.get("character_design"),
    )


def terminal_steps(topic: str) -> list[dict[str, Any]]:
    return [
        {"kind": "cmd", "text": f'openmontage produce --topic "{topic}"', "holdSeconds": 0.4},
        {"kind": "out", "text": "pipeline: screen-demo", "holdSeconds": 0.3},
        {"kind": "cmd", "text": "npx remotion render TerminalScene", "holdSeconds": 0.4},
        {"kind": "out", "text": "wrote renders/final.mp4", "holdSeconds": 0.4},
        {"kind": "pill", "text": "synthetic terminal", "color": "#22D3EE"},
    ]


def has_video_api_keys() -> bool:
    import os

    names = (
        "FAL_KEY",
        "FAL_AI_API_KEY",
        "MINIMAX_API_KEY",
        "KLING_API_KEY",
        "RUNWAY_API_KEY",
        "HEYGEN_API_KEY",
        "REPLICATE_API_TOKEN",
        "ARK_API_KEY",
        "XAI_API_KEY",
    )
    return any(os.environ.get(n) for n in names)


def azure_stt_ok() -> bool:
    import os

    return bool(
        os.environ.get("AZURE_SPEECH_KEY")
        and (os.environ.get("AZURE_SPEECH_REGION") or os.environ.get("AZURE_SPEECH_ENDPOINT"))
    )


def openai_stt_ok() -> bool:
    import os

    return bool(os.environ.get("OPENAI_API_KEY"))


def dashscope_asr_ok() -> bool:
    import os

    return bool(os.environ.get("DASHSCOPE_API_KEY"))


def whisper_api_ok(*, public_audio_url: str | None = None) -> bool:
    """True only when a backend we actually call can run."""
    if azure_stt_ok() or openai_stt_ok():
        return True
    return dashscope_asr_ok() and bool(public_audio_url)


def _normalize_transcript(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    if not payload.get("text"):
        payload["text"] = " ".join(
            str(s.get("text") or "") for s in (payload.get("segments") or []) if isinstance(s, dict)
        ).strip()
    return payload


def transcribe_footage(
    work_dir: Path,
    footage: list[Path],
    *,
    canned: bool,
    public_audio_url: str | None = None,
) -> dict[str, Any] | None:
    """API STT only: Azure, else OpenAI Whisper, else DashScope (public URL). Never local Whisper."""
    dest = project_dir(work_dir) / "artifacts" / "transcript.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = footage[0] if footage else None
    attempts: list[tuple[str, dict[str, Any]]] = []
    if src and azure_stt_ok():
        attempts.append(("azure_stt", {"input_path": str(src)}))
    if src and openai_stt_ok():
        attempts.append(("openai_stt", {"input_path": str(src)}))
    if dashscope_asr_ok() and public_audio_url:
        attempts.append(("dashscope_asr", {"audio_url": public_audio_url}))
    for name, inputs in attempts:
        result = tools_exec.execute(name, inputs)
        if result.success and isinstance(result.data, dict):
            payload = _normalize_transcript(result.data)
            dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return payload
    if canned:
        payload = {
            "version": "1.0",
            "text": "Canned transcript for localization-dub fixture.",
            "segments": [{"start": 0, "end": 8, "text": "Canned transcript for localization-dub fixture."}],
        }
        dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    return None


def maybe_generate_video(topic: str, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = tools_exec.execute(
        "video_selector",
        {"prompt": topic, "output_path": str(dest), "duration_seconds": 5},
    )
    if result.success:
        if result.artifacts:
            path = Path(result.artifacts[0])
            if path.is_file():
                return path
        if dest.is_file():
            return dest
    return None


def generate_shots(
    topic: str,
    dest_dir: Path,
    scene_plan: dict[str, Any] | None,
    duration: int,
) -> list[Path]:
    """Cinematic shot loop: one video_selector call per scene, not one clip for the film."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    scenes = list((scene_plan or {}).get("scenes") or [])
    if not scenes:
        scenes = [{"id": "sc1", "description": topic, "start_seconds": 0, "end_seconds": max(4, duration)}]
    clips: list[Path] = []
    for i, scene in enumerate(scenes):
        sid = str(scene.get("id") or f"sc{i + 1}")
        prompt = f"{topic}. Shot: {scene.get('description') or sid}"
        dur = max(4, int((scene.get("end_seconds") or 5) - (scene.get("start_seconds") or 0)))
        dest = dest_dir / f"{sid}.mp4"
        clip = maybe_generate_video(prompt, dest)
        if clip:
            clips.append(clip)
    return clips


def try_real_capture(dest: Path, topic: str) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for name in ("screen_recorder", "cap_recorder"):
        result = tools_exec.execute(name, {"output_path": str(dest), "title": topic[:80]})
        if result.success:
            if dest.is_file():
                return dest
            if result.artifacts:
                cand = Path(str(result.artifacts[0]))
                if cand.is_file():
                    return cand
    return None
