"""SaaS job loop: YAML stages + Gemini tool calls + Python gates. Not color cards."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from runner import assets as asset_gather
from runner import atelier, budget, canned, chapters, checkpoint, compose, decisions, directors, edl, envelope, ep_loop, gates, ingest, mode, picture_patch, pipeline, publish, r2, reference, route, skills, stubs, telemetry, tools_exec, visual_qa
from runner import asset_director, edit_director
from runner.artifacts import project_dir, read_artifact, stage_artifact_name, write_artifact
from schemas.artifacts import ARTIFACT_NAMES, load_schema, validate_artifact
from runner.config import load_saas, repo_root
from runner.edl import PICTURE_SCENE_TYPES
from runner.ffmpeg_bin import find_ffmpeg, pin_scratch_temp, prepend_to_path
from runner.llm_gemini import LLMError, MockGemini, StageModel, default_model
from runner.preflight import doctors, hyperframes_ok, motion_canvas_ok, require_runtime, tts_ready

VIDEO_KEY_PIPELINES = {"cinematic", "avatar-spokesperson"}
MC_SCENE_TYPES = {"diagram", "map", "etymology", "code", "math", "timeline"}
ASSEMBLE_PIPELINES = {"animated-explainer", "animation", "documentary-montage", "hybrid"}
HERO_PIPELINES = {"animated-explainer", "animation"}
FOOTAGE_REQUIRED = ingest.REQUIRED_FOOTAGE
FOOTAGE_WHISPER = {
    "talking-head",
    "hybrid",
    "clip-factory",
    "podcast-repurpose",
    "localization-dub",
}
CREATIVE_STAGES = {
    "research",
    "proposal",
    "idea",
    "script",
    "scene_plan",
    "character_design",
    "rig_plan",
    "assets",
    "edit",
    "compose",
    "publish",
}
SKIP_STAGES = {
    "compose",
    "publish",
    "real_capture",
    "scene_review",
    "review",
    "qa",
}

FAMILY_BY_PIPELINE = {
    "animated-explainer": "explainer-data",
    "animation": "animation-first",
    "character-animation": "animation-first",
    "documentary-montage": "documentary-montage",
    "screen-demo": "screen-demo",
    "talking-head": "presenter",
    "hybrid": "explainer-data",
    "clip-factory": "explainer-data",
    "podcast-repurpose": "explainer-data",
    "localization-dub": "explainer-data",
    "cinematic": "cinematic-trailer",
    "avatar-spokesperson": "presenter",
}


def _lock_runtime(job: dict[str, Any], pipeline_name: str, docs: dict[str, bool] | None = None) -> dict[str, Any]:
    override = (job.get("prefs") or {}).get("render_runtime")
    if pipeline_name == "character-animation":
        runtime = "hyperframes"
    elif pipeline_name in {"clip-factory", "localization-dub", "podcast-repurpose"}:
        runtime = "ffmpeg"
    elif pipeline_name in VIDEO_KEY_PIPELINES:
        runtime = "remotion"
    else:
        runtime = "remotion"
    if override in {"remotion", "hyperframes", "ffmpeg", "motion_canvas"}:
        runtime = override
    docs = docs or {}
    if runtime == "hyperframes" and docs.get("hyperframes") is False and docs.get("remotion"):
        runtime = "remotion"
    family = FAMILY_BY_PIPELINE.get(pipeline_name, "explainer-data")
    return {
        "renderer_family": family,
        "render_runtime": runtime,
        "compose_strategy": "single_runtime",
        "reason": "locked at proposal for SaaS job",
        "engines_available": {
            "remotion": bool(docs.get("remotion")),
            "hyperframes": bool(docs.get("hyperframes")),
            "ffmpeg": bool(docs.get("ffmpeg", True)),
        },
    }


def _env_present(name: str) -> bool:
    val = (os.environ.get(name) or "").strip()
    return bool(val) and not val.startswith("dummy-")


def _machine_facts(pipeline_name: str, locked: dict[str, Any], docs: dict[str, bool], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    tts_ok, tts_provider = tts_ready()
    facts = {
        "render_engines": docs,
        "locked_render_runtime": locked.get("render_runtime"),
        "locked_renderer_family": locked.get("renderer_family"),
        "composition_mode_default": "undecided",
        "present_both_runtimes": bool(docs.get("remotion") and docs.get("hyperframes")),
        "tts": {"ready": tts_ok, "provider": tts_provider},
        "keys_present": {
            "GOOGLE_API_KEY": _env_present("GOOGLE_API_KEY") or _env_present("GEMINI_API_KEY"),
            "BRAVE_API_KEY": _env_present("BRAVE_API_KEY"),
            "ELEVENLABS_API_KEY": _env_present("ELEVENLABS_API_KEY"),
            "PEXELS_API_KEY": _env_present("PEXELS_API_KEY"),
            "ANTHROPIC_API_KEY": _env_present("ANTHROPIC_API_KEY"),
            "OPENAI_API_KEY": _env_present("OPENAI_API_KEY"),
            "FAL_KEY": _env_present("FAL_KEY"),
            "HEYGEN_API_KEY": _env_present("HEYGEN_API_KEY"),
        },
    }
    if extra:
        facts.update(extra)
    return facts


def _api_extra_skills(stage: str, pipeline_name: str, *, duration: int = 45, long_form: bool = False) -> list[str]:
    return ep_loop.extra_skills(stage, pipeline_name, duration=duration, long_form=long_form)


def _atelier_skill_text(kind: str) -> str:
    return skills.load_meta("meta/api-llm-atelier") + skills.load_picture_pack(kind)


def _ffmpeg() -> str | None:
    prepend_to_path()
    return find_ffmpeg()


def _is_mock(model: StageModel | None) -> bool:
    return (
        isinstance(model, MockGemini)
        or type(model).__name__ == "MockGemini"
        or bool(getattr(model, "is_mock", False))
    )


def _preview_frames(mp4: Path, dest_dir: Path, count: int = 2) -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    ff = _ffmpeg()
    if not ff or not mp4.is_file():
        return []
    out = dest_dir / "scene_%02d.png"
    subprocess.check_call(
        [ff, "-y", "-i", str(mp4), "-vf", f"fps={count}/10", str(out)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return [str(p) for p in sorted(dest_dir.glob("scene_*.png"))]


def _atelier_client(existing: StageModel | None) -> StageModel:
    try:
        return default_model("atelier")
    except LLMError:
        if existing is not None:
            return existing
        raise


def _visual_qa_client(existing: StageModel | None, *, canned: bool, mock: bool) -> StageModel | None:
    if canned or mock:
        return existing
    try:
        return default_model("visual_qa")
    except LLMError:
        return existing


def _storyboard_payload(
    *,
    job: dict[str, Any],
    status: str,
    compose_strategy: str,
    composition_mode: str,
    scenes: list[dict[str, Any]],
    scene_runtimes: list[dict[str, Any]],
    scene_files: list[Path],
    posters_dir: Path,
    selection: dict[str, Any],
    qa_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    qa_by_id = qa_by_id or {}
    slug = selection.get("atelier_slug")
    cid = selection.get("composition_id")
    board: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "status": status,
        "compose_strategy": compose_strategy,
        "composition_mode": composition_mode,
        "atelier_slug": slug,
        "composition_id": cid,
        "scenes": [],
    }
    for scene, runtime_row, mp4 in zip(scenes, scene_runtimes, scene_files):
        sid = str(scene.get("id") or mp4.stem)
        frames = _preview_frames(mp4, posters_dir / sid)
        qa = qa_by_id.get(sid) or {}
        board["scenes"].append(
            {
                "id": sid,
                "script_line": scene.get("description"),
                "duration": (scene.get("end_seconds") or 0) - (scene.get("start_seconds") or 0),
                "approved": False,
                "poster": frames[0] if frames else None,
                "preview": str(mp4),
                "poster_key": None,
                "preview_key": None,
                "render_runtime": runtime_row.get("render_runtime"),
                "generator": runtime_row.get("generator"),
                "composition_mode": composition_mode,
                "qa_status": qa.get("qa_status") or "skipped",
                "qa_issues": list(qa.get("issues") or []),
                "atelier_slug": slug,
                "composition_id": cid,
            }
        )
    return board


def _merge_mc_skips(report: dict[str, Any], asset_manifest: dict[str, Any]) -> dict[str, Any]:
    skips = set((asset_manifest.get("metadata") or {}).get("mc_skips") or [])
    if not skips:
        return report
    rows = list(report.get("scenes") or [])
    by_id = {str(r.get("id")): r for r in rows}
    for sid in skips:
        row = by_id.get(str(sid))
        if row is None:
            rows.append(
                {
                    "id": str(sid),
                    "pass": False,
                    "severity": "critical",
                    "qa_status": "fail",
                    "issues": ["motion canvas skipped (empty mermaid or generate/render failed)"],
                    "rewrite_hint": "Write a real mermaid diagram for this scene.",
                }
            )
            continue
        issues = list(row.get("issues") or [])
        issues.append("motion canvas skipped (empty mermaid or generate/render failed)")
        row["issues"] = issues
        row["qa_status"] = "fail"
        row["severity"] = "critical"
        row["pass"] = False
        row["rewrite_hint"] = row.get("rewrite_hint") or "Write a real mermaid/code/tex diagram or patch the atelier Sequence."
    report["scenes"] = rows
    return report


def _patch_kind(
    *,
    composition_mode: str,
    selection: dict[str, Any],
    runtime_row: dict[str, Any] | None,
    character_workspace: str | None,
    pipeline_name: str,
) -> str:
    gen = str((runtime_row or {}).get("generator") or "")
    runtime = str((runtime_row or {}).get("render_runtime") or selection.get("render_runtime") or "")
    if (
        gen == "hyperframes"
        or runtime == "hyperframes"
        or character_workspace
        or pipeline_name == "character-animation"
    ):
        return "hyperframes"
    if composition_mode == "atelier" or gen == "atelier":
        return "remotion_atelier"
    return "templated"


def _rewrite_failing_scenes(
    *,
    failing: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    scene_files: list[Path],
    scene_runtimes: list[dict[str, Any]],
    composition_mode: str,
    selection: dict[str, Any],
    client: StageModel | None,
    work_dir: Path,
    scenes_dir: Path,
    duration: int,
    prefs: dict[str, Any],
    skill_text: str,
    retries: int,
    playbook: str | None,
    pipeline_name: str,
    topic: str,
    asset_manifest: dict[str, Any],
    character_workspace: str | None,
    script: dict[str, Any] | None,
    canned_mode: bool,
    edit: dict[str, Any],
    qa_model: StageModel | None = None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Vision-patch failing picture scenes (3 rounds live). Never Explainer-swap. Salvage last file."""
    by_id = {str(s.get("id")): s for s in scenes}
    files_by_id = {str(s.get("id")): p for s, p in zip(scenes, scene_files)}
    runtime_by_id = {str(r.get("scene_id")): r for r in scene_runtimes}
    atelier_slug = selection.get("atelier_slug")
    atelier_cid = selection.get("composition_id") or "StudioPiece"
    regen_prompt = str((prefs or {}).get("regen_prompt") or "")
    hf_workspace = Path(character_workspace) if character_workspace else project_dir(work_dir) / "hyperframes"
    patched: set[str] = set()
    for row in failing:
        sid = str(row.get("id") or "")
        if not sid or sid in patched:
            continue
        scene = by_id.get(sid)
        if not scene:
            continue
        kind = _patch_kind(
            composition_mode=composition_mode,
            selection=selection,
            runtime_row=runtime_by_id.get(sid),
            character_workspace=character_workspace,
            pipeline_name=pipeline_name,
        )
        initial = files_by_id.get(sid)
        if initial is None:
            continue

        def _review(path: Path, scene_id: str = sid, sc: dict[str, Any] = scene) -> dict[str, Any]:
            return visual_qa.review_scene(
                mp4=path,
                dest_dir=scenes_dir / f"{scene_id}_qa_loop",
                scene=sc,
                model=qa_model,
                canned=False,
                delivery_promise=str((prefs or {}).get("delivery_promise") or "mixed"),
            )

        if kind == "remotion_atelier":
            if not atelier_slug or client is None:
                continue
            pack = skill_text or _atelier_skill_text("remotion")

            def _patch_remotion(qa_row: dict[str, Any], stills: list[Path], scene_id: str = sid) -> Path:
                return atelier.rerender_scene(
                    model=client,
                    work_dir=work_dir,
                    slug=str(atelier_slug),
                    composition_id=str(atelier_cid),
                    scene_id=scene_id,
                    scenes=scenes,
                    scenes_dir=scenes_dir,
                    duration=duration,
                    prefs=prefs,
                    skill_text=pack,
                    retries=retries,
                    playbook=playbook,
                    regen_prompt=regen_prompt,
                    rewrite_hint=str(qa_row.get("rewrite_hint") or row.get("rewrite_hint") or ""),
                    images=stills,
                )

            last, qa_out, _n = picture_patch.run_rounds(
                initial_path=initial,
                initial_qa=row,
                patch=_patch_remotion,
                review=None if canned_mode else _review,
                canned=canned_mode,
            )
            files_by_id[sid] = last
            _mark_runtime(last, "remotion", "atelier")
            runtime_by_id[sid] = {
                "scene_id": sid,
                "render_runtime": "remotion",
                "generator": "atelier",
            }
            if qa_out.get("qa_status") == "fail":
                row["qa_status"] = "fail"
                row.setdefault("issues", []).append("atelier rewrite salvaged last render")
            patched.add(sid)
            continue

        if kind == "hyperframes":
            if client is None or not (hf_workspace / "index.html").is_file():
                continue
            pack = _atelier_skill_text("hyperframes")

            def _patch_hf(qa_row: dict[str, Any], stills: list[Path], dest: Path = initial) -> Path:
                return atelier.rerender_kinetic(
                    model=client,
                    workspace=hf_workspace,
                    dest=dest,
                    skill_text=pack,
                    retries=retries,
                    prefs=prefs,
                    regen_prompt=regen_prompt,
                    rewrite_hint=str(qa_row.get("rewrite_hint") or row.get("rewrite_hint") or ""),
                    images=stills,
                    canned=canned_mode,
                )

            last, qa_out, _n = picture_patch.run_rounds(
                initial_path=initial,
                initial_qa=row,
                patch=_patch_hf,
                review=None if canned_mode else _review,
                canned=canned_mode,
            )
            files_by_id[sid] = last
            _mark_runtime(last, "hyperframes", "hyperframes")
            runtime_by_id[sid] = {
                "scene_id": sid,
                "render_runtime": "hyperframes",
                "generator": "hyperframes",
            }
            patched.add(sid)
            continue

        stype = str(scene.get("type") or "").lower()
        if stype in PICTURE_SCENE_TYPES:
            row.setdefault("issues", []).append("picture scene kept; Explainer title refused")
            continue
        dest = scenes_dir / f"{sid}.mp4"
        hint = str(row.get("rewrite_hint") or "")
        scene2 = dict(scene)
        if hint:
            scene2["description"] = f"{scene.get('description') or ''}\nREWRITE: {hint}"
        runtime = str((runtime_by_id.get(sid) or {}).get("render_runtime") or "remotion")
        dur = max(1, int((scene.get("end_seconds") or 5) - (scene.get("start_seconds") or 0)))
        try:
            meta = compose.render_scene(
                dest=dest,
                scene=scene2,
                runtime=runtime,
                pipeline=pipeline_name,
                topic=topic,
                asset_manifest=asset_manifest,
                seconds=dur,
                character_workspace=character_workspace,
                script=script,
                canned=canned_mode,
                prefs=prefs,
                edit=edit,
            )
        except compose.ComposeError:
            continue
        files_by_id[sid] = dest
        runtime_by_id[sid] = {
            "scene_id": sid,
            "render_runtime": meta.get("render_runtime") or runtime,
            "generator": meta.get("generator"),
        }
        patched.add(sid)
    new_files = [files_by_id.get(str(s.get("id")), p) for s, p in zip(scenes, scene_files)]
    new_runtimes = [runtime_by_id.get(str(s.get("id")), r) for s, r in zip(scenes, scene_runtimes)]
    return new_files, new_runtimes


def _debug_card(dest: Path, seconds: int, title: str) -> Path:
    """Only when STUDIO_DEBUG_CARDS=1. Never a production compose path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg required even for debug cards")
    subprocess.check_call(
        [
            ff,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x334155:s=1920x1080:d={seconds}:r=30",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={seconds}",
            "-vf",
            f"drawtext=text='DEBUG {title[:40]}':fontsize=48:fontcolor=white:x=(w-tw)/2:y=(h-th)/2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dest.with_suffix(".runtime.json").write_text(
        json.dumps({"render_runtime": "debug", "generator": "debug_card"}),
        encoding="utf-8",
    )
    return dest


def _on_tool(name: str, arguments: dict[str, Any]) -> Any:
    return tools_exec.execute(name, arguments or {})


def _prior_payload(work_dir: Path, prefs: dict[str, Any], pipeline_name: str, extra: dict[str, Any] | None = None) -> str:
    names = ["research_brief", "proposal_packet", "brief", "script", "scene_plan", "character_design", "rig_plan"]
    prior = {name: read_artifact(work_dir, name) for name in names}
    prior = {key: value for key, value in prior.items() if value}
    blob: dict[str, Any] = {"prefs": prefs, "pipeline": pipeline_name, "prior_artifacts": prior}
    if extra:
        blob.update(extra)
    return json.dumps(blob, default=str)


def _run_llm_stage(
    model: StageModel,
    *,
    stage: str,
    skill_text: str,
    user: str,
    tool_names: list[str],
    retries: int,
    max_rounds: int,
    postprocess: Any | None = None,
) -> dict[str, Any]:
    schemas = tools_exec.tool_schemas(tool_names)
    last_err = "no JSON artifact"
    artifact_name = _artifact_for_stage(stage)
    telemetry.set_role(getattr(model, "role", None) or "planner")
    with telemetry.span(stage):
        return _run_llm_stage_attempts(
            model,
            stage=stage,
            skill_text=skill_text,
            user=user,
            schemas=schemas,
            retries=retries,
            max_rounds=max_rounds,
            artifact_name=artifact_name,
            last_err=last_err,
            postprocess=postprocess,
        )


def _run_llm_stage_attempts(
    model: StageModel,
    *,
    stage: str,
    skill_text: str,
    user: str,
    schemas: list[dict[str, Any]],
    retries: int,
    max_rounds: int,
    artifact_name: str,
    last_err: str,
    postprocess: Any | None = None,
) -> dict[str, Any]:
    api_rules = (
        " API RUNNER: return ONE JSON artifact. Never call shell/bash/npx/python. "
        "Director, reviewer, and Layer 3 skills are the creative contract. "
        "This overlay only forbids unavailable tools (shell/npx)."
    )
    for attempt in range(max(1, retries)):
        turn = model.run_stage(
            system=(
                "You are the Studio job runner. Return ONE JSON artifact. "
                "Never propose shell/bash. Follow the stage skill for creative quality."
                + api_rules
            )
            + "\n\n"
            + skill_text,
            user=user if attempt == 0 else f"{user}\n\nPrevious attempt failed schema: {last_err}",
            tools=schemas,
            retry=attempt,
            on_tool=_on_tool,
            max_rounds=max_rounds,
        )
        if turn.artifact and isinstance(turn.artifact, dict):
            artifact = postprocess(turn.artifact) if callable(postprocess) else turn.artifact
            if artifact_name in ARTIFACT_NAMES:
                try:
                    validate_artifact(artifact_name, artifact)
                except ValidationError as exc:
                    last_err = str(exc)
                    continue
            return artifact
        last_err = turn.text or last_err
    raise LLMError(last_err or f"stage {stage} failed")


def _scene_runtime(
    scene: dict[str, Any],
    pipeline_name: str,
    locked: dict[str, Any],
    *,
    mc_ok: bool,
) -> str:
    stype = (scene.get("type") or "").lower()
    if pipeline_name == "character-animation":
        return "hyperframes"
    if pipeline_name in {"clip-factory", "localization-dub", "podcast-repurpose"}:
        return "ffmpeg"
    if pipeline_name == "talking-head":
        return "remotion"
    if pipeline_name in ASSEMBLE_PIPELINES and stype in MC_SCENE_TYPES:
        if mc_ok:
            return "motion_canvas"
        return locked.get("render_runtime") or "remotion"
    if pipeline_name == "documentary-montage" and stype == "broll":
        return "ffmpeg"
    if pipeline_name == "documentary-montage" and stype == "end_tag":
        return "remotion"
    return locked.get("render_runtime") or "remotion"


def _artifact_for_stage(stage: str) -> str:
    return {
        "research": "research_brief",
        "proposal": "proposal_packet",
        "idea": "brief",
        "script": "script",
        "scene_plan": "scene_plan",
        "assets": "asset_manifest",
        "edit": "edit_decisions",
        "character_design": "character_design",
        "rig_plan": "rig_plan",
        "publish": "publish_log",
    }.get(stage, stage)


def _trace_urls() -> list[str]:
    return tools_exec.traced_urls()


def _url_cited(url: str, traces: list[str]) -> bool:
    needle = url.rstrip("/")
    if not needle:
        return False
    for seen in traces:
        hay = seen.rstrip("/")
        if needle in hay or hay in needle:
            return True
    return False


def _collect_http_urls(artifact: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for _obj, _key, url in _walk_url_fields(artifact):
        found.append(url)
    return found


def _walk_url_fields(obj: Any):
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in {"source_url", "url"} and isinstance(val, str) and val.startswith(("http://", "https://")):
                yield obj, key, val
            else:
                yield from _walk_url_fields(val)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_url_fields(item)


def _gate_research_urls(artifact: dict[str, Any], *, skip: bool) -> None:
    if skip:
        return
    traces = _trace_urls()
    urls = _collect_http_urls(artifact)
    if urls and not traces:
        raise LLMError("research_brief source_url values were not produced by web_search/web_fetch")
    missing = [url for url in urls if not _url_cited(url, traces)]
    if missing:
        raise LLMError("research_brief invented URLs not present in tool traces: " + ", ".join(missing[:4]))


def _decision_log(
    job_id: str,
    pipeline_name: str,
    composition_mode: str,
    *,
    docs: dict[str, bool] | None = None,
    selected_runtime: str = "remotion",
    routed_from: str | None = None,
) -> dict[str, Any]:
    log = decisions.empty(str(job_id or "studio"))
    if routed_from:
        log = decisions.append(
            log,
            decision_id="d-pipeline",
            stage="proposal",
            category="pipeline_selection",
            subject="Production pipeline",
            options=[
                decisions.option(routed_from, routed_from, 0.4, "Job requested auto"),
                decisions.option(pipeline_name, pipeline_name, 0.9, "Matched topic, footage, and duration"),
            ],
            selected=pipeline_name,
            reason=f"auto routed {routed_from} → {pipeline_name}",
            project_id=str(job_id or "studio"),
        )
    remotion_ok = bool((docs or {}).get("remotion", True))
    hf_ok = bool((docs or {}).get("hyperframes"))
    runtime_opts = [
        decisions.option(
            "remotion",
            "Remotion",
            0.8 if remotion_ok else 0.2,
            "React timeline; Explainer and atelier TSX",
            None if remotion_ok else "runtime not available on this machine",
        ),
        decisions.option(
            "hyperframes",
            "HyperFrames",
            0.7 if hf_ok else 0.2,
            "HTML/GSAP kinetic and character acting",
            None if hf_ok else "runtime not available on this machine",
        ),
        decisions.option("ffmpeg", "FFmpeg", 0.6, "Trim, stitch, dub, and clip-factory assemble"),
    ]
    log = decisions.append(
        log,
        decision_id="d-render-runtime",
        stage="proposal",
        category="render_runtime_selection",
        subject="Composition runtime",
        options=runtime_opts,
        selected=selected_runtime if selected_runtime in {"remotion", "hyperframes", "ffmpeg"} else "remotion",
        reason=f"Locked render_runtime={selected_runtime} (both engines listed when installed)",
        project_id=str(job_id or "studio"),
    )
    log = decisions.append(
        log,
        decision_id="d-composition-mode",
        stage="proposal",
        category="composition_mode",
        subject="templated vs atelier authoring",
        options=[
            decisions.option("templated", "templated Explainer / stock cut types", 0.5, "Batch and footage-led work"),
            decisions.option("atelier", "hand-authored Remotion TSX or HyperFrames HTML", 0.5, "Hero explainers"),
        ],
        selected=composition_mode,
        reason=f"Locked composition_mode={composition_mode} for {pipeline_name}",
        project_id=str(job_id or "studio"),
    )
    return log


def _playbook_name(proposal: dict[str, Any] | None, prefs: dict[str, Any]) -> str | None:
    plan = (proposal or {}).get("production_plan") or {}
    return plan.get("playbook") or prefs.get("style_playbook")


def _mark_runtime(path: Path, runtime: str, generator: str) -> None:
    path.with_suffix(".runtime.json").write_text(
        json.dumps({"render_runtime": runtime, "generator": generator}),
        encoding="utf-8",
    )


def _upload_checkpoints(work_dir: Path, prefix: str) -> None:
    root = project_dir(work_dir)
    for path in list((root / "scenes").glob("*.mp4")) + list((root / "artifacts").glob("*.json")):
        if not path.is_file():
            continue
        key = prefix.rstrip("/") + "/" + path.relative_to(root).as_posix()
        try:
            r2.put_file(key, path, "application/octet-stream")
        except Exception:
            continue


def _hydrate_from_r2(work_dir: Path, job: dict[str, Any]) -> None:
    blob = job.get("r2") or {}
    gets = blob.get("get") or blob.get("get_urls") or {}
    if not isinstance(gets, dict) or not gets:
        return
    root = project_dir(work_dir)
    for rel, url in gets.items():
        dest = root / str(rel).lstrip("/").replace("\\", "/")
        if dest.is_file():
            continue
        try:
            r2.get_file(str(url), dest)
        except Exception:
            continue


def _fail(error: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload = {"status": "failed", "error": error, "reason": reason}
    payload.update(extra)
    return _finish_job(payload)


def _finish_job(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        budget.persist()
    except Exception:
        pass
    try:
        status = payload.get("status")
        if status:
            telemetry.set_meta(status=status, error=payload.get("error"), reason=payload.get("reason"))
        if status == "failed":
            telemetry.record_flag("job", str(payload.get("error") or payload.get("reason") or "failed"))
        snap = telemetry.current()
        if snap is not None:
            payload["telemetry"] = snap.snapshot()
        telemetry.persist()
    except Exception:
        pass
    return payload


def _run_job(job: dict[str, Any], *, work_dir: Path, model: StageModel | None = None) -> dict[str, Any]:
    load_saas()
    prepend_to_path()
    work_dir = Path(work_dir).resolve()
    pipeline_requested = str(job.get("pipeline") or "auto")
    pipeline_name = route.resolve(job)
    prefs = job.get("prefs") or {}
    topic = str(prefs.get("topic") or pipeline_name)
    duration = chapters.clamp_duration(int(prefs.get("duration_seconds") or 45))
    canned_mode = bool(job.get("canned")) or str(job.get("job_id", "")).startswith("fixture-")
    review = bool(job.get("review_mode", True))
    continue_after = bool(job.get("continue_after_review"))
    regen_id = job.get("regenerate_scene_id")
    debug_cards = os.environ.get("STUDIO_DEBUG_CARDS") == "1"
    mock = _is_mock(model)
    client = model
    telemetry.attach(job_id=str(job.get("job_id") or ""), pipeline=pipeline_name, work_dir=work_dir)
    telemetry.set_meta(canned=canned_mode, review=review, topic=topic, duration_seconds=duration, pipeline_requested=pipeline_requested)
    from runner import progress

    progress.note(
        f"job {job.get('job_id') or ''} pipeline={pipeline_name} topic={topic!r} duration={duration}s canned={canned_mode}",
        kind="job",
    )

    if pipeline_name in VIDEO_KEY_PIPELINES and not directors.has_video_api_keys() and not ingest.has_media(job):
        return _fail(
            "delivery_promise",
            f"{pipeline_name} requires video keys; refusing silent slideshow",
            delivery_promise=prefs.get("delivery_promise") or "mixed",
        )

    if not canned_mode:
        from runner import om_agent

        return om_agent.run(job, work_dir=work_dir, model=model)

    manifest = pipeline.load(pipeline_name)
    work_dir.mkdir(parents=True, exist_ok=True)
    pin_scratch_temp(work_dir / ".tmp")
    proj = project_dir(work_dir)
    marker = {
        "version": "1.0",
        "project_id": str(job.get("job_id") or proj.name),
        "title": topic[:120],
        "pipeline_type": pipeline_name,
    }
    (proj / "project.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    checkpoint.init_job(work_dir, pipeline_name, topic, prefs.get("style_playbook"))
    budget.attach(prefs, canned=canned_mode, work_dir=work_dir)
    _hydrate_from_r2(work_dir, job)
    docs = doctors()
    selection = _lock_runtime(job, pipeline_name, docs)
    if regen_id:
        prev_sel = read_artifact(work_dir, "render_runtime_selection") or {}
        for key in ("atelier_slug", "composition_id", "master_path", "composition_mode"):
            if prev_sel.get(key):
                selection[key] = prev_sel[key]
    write_artifact(work_dir, "render_runtime_selection", selection)
    motion_required = prefs.get("delivery_promise") == "motion_led" or pipeline_name == "character-animation"
    locked_runtime = selection["render_runtime"]
    cap = envelope.summary()
    missing_tools = envelope.required_missing(manifest)
    write_artifact(
        work_dir,
        "capability_envelope",
        {
            "version": "1.0",
            "composition_runtimes": (cap or {}).get("composition_runtimes") or {},
            "runtime_warnings": (cap or {}).get("runtime_warnings") or [],
            "required_missing": missing_tools,
        },
    )
    telemetry.set_meta(render_runtime=locked_runtime, doctors=docs)
    if missing_tools and not canned_mode and not mock:
        return _fail("preflight", "required tools unavailable: " + ", ".join(missing_tools[:8]), doctors=docs)
    pre = require_runtime(locked_runtime, motion_required=motion_required)
    if not pre.get("ok"):
        return _fail("doctor", pre.get("error") or f"{locked_runtime} unavailable", runtime=locked_runtime, doctors=docs)

    chapter_plan = chapters.plan(duration, prefs, pipeline_name)
    long_form = chapters.should_chapter(duration, prefs, pipeline_name)
    write_artifact(work_dir, "chapter_plan", {"version": "1.0", "chapters": chapter_plan})

    ref_notes = None if canned_mode else reference.analyze(job, work_dir)

    footage: list[Path] = []
    transcript: dict[str, Any] | None = None
    if pipeline_name in FOOTAGE_REQUIRED:
        footage = ingest.ingest(job, work_dir)
        if not footage:
            return _fail(
                "delivery_promise" if pipeline_name in VIDEO_KEY_PIPELINES else "footage",
                f"{pipeline_name} needs asset_keys or asset_urls",
            )
        if pipeline_name in FOOTAGE_WHISPER:
            transcript = directors.transcribe_footage(
                work_dir,
                footage,
                canned=canned_mode,
                public_audio_url=ingest.public_get_url(job),
            )
            if transcript is None:
                return _fail("whisper_api", f"{pipeline_name} requires Whisper API (no local Whisper)")
            try:
                write_artifact(
                    work_dir,
                    "source_media_review",
                    {
                        "version": "1.0",
                        "files": [
                            {
                                "path": str(footage[0]),
                                "media_type": "video",
                                "reviewed": True,
                                "transcript_summary": str((transcript or {}).get("text") or (transcript or {}).get("transcript") or "")[:800],
                            }
                        ],
                        "summary": str((transcript or {}).get("text") or "Whisper API transcript")[:500],
                        "planning_implications": ["Script and scene in/out points must follow the transcript."],
                    },
                )
            except ValidationError:
                pass

    if continue_after and (project_dir(work_dir) / "scenes").exists():
        scene_plan = read_artifact(work_dir, "scene_plan") or {"scenes": []}
        scene_files = sorted((project_dir(work_dir) / "scenes").glob("*.mp4"))
        if not scene_files:
            return _fail("resume", "no scene mp4s to continue")
        final_path = project_dir(work_dir) / "renders" / "final.mp4"
        selection = read_artifact(work_dir, "render_runtime_selection") or {}
        strategy = str(selection.get("compose_strategy") or "single_runtime")
        master = Path(str(selection.get("master_path") or ""))
        with telemetry.span("stitch"):
            compose.assemble_final(
                strategy=strategy,
                scene_files=scene_files,
                dest=final_path,
                master=master if master.is_file() else None,
            )
            gates.ffprobe_mp4(final_path)
        write_artifact(
            work_dir,
            "render_report",
            {
                "version": "1.0",
                "outputs": [
                    {
                        "path": str(final_path),
                        "format": "mp4",
                        "codec": "h264",
                        "audio_codec": "aac",
                        "resolution": "1920x1080",
                        "fps": 30,
                        "duration_seconds": duration,
                    }
                ],
                "metadata": {"continue": True, "generator": "video_stitch"},
            },
        )
        return _finish_job({
            "status": "done",
            "work_dir": str(work_dir),
            "final_mp4": str(final_path),
            "compose_strategy": "scene_assemble",
        })

    retries = pipeline.max_revisions(manifest)
    playbook = prefs.get("style_playbook")
    if canned_mode and not regen_id:
        bundle = canned.for_pipeline(pipeline_name, topic)
        bundle["edit_decisions"]["render_runtime"] = (
            selection["render_runtime"] if selection["render_runtime"] != "motion_canvas" else "remotion"
        )
        canned.write_canned(work_dir, bundle)
        write_artifact(work_dir, "research_brief", stubs.research_brief(topic))
        write_artifact(
            work_dir,
            "proposal_packet",
            stubs.proposal_packet(topic, pipeline_name, selection["render_runtime"]),
        )
        if not read_artifact(work_dir, "brief"):
            write_artifact(work_dir, "brief", stubs.brief(topic, duration))
        for st in pipeline.stages(manifest):
            if st in SKIP_STAGES:
                continue
            name = stage_artifact_name(st)
            payload = read_artifact(work_dir, name)
            ckpt_arts = {name: payload} if isinstance(payload, dict) else {}
            checkpoint.write_stage(work_dir, pipeline_name, st, artifacts=ckpt_arts)

    footage = footage or ingest.ingest(job, work_dir)
    if pipeline_name in FOOTAGE_REQUIRED and not footage:
        return _fail(
            "delivery_promise" if pipeline_name in VIDEO_KEY_PIPELINES else "footage",
            f"{pipeline_name} needs asset_keys or asset_urls",
        )

    if pipeline_name in VIDEO_KEY_PIPELINES and directors.has_video_api_keys() and not footage:
        clips = directors.generate_shots(
            topic,
            project_dir(work_dir) / "footage" / "shots",
            read_artifact(work_dir, "scene_plan"),
            duration,
        )
        if clips:
            footage = clips
        else:
            generated = directors.maybe_generate_video(topic, project_dir(work_dir) / "footage" / "gen.mp4")
            if generated:
                footage = [generated]
            else:
                return _fail("delivery_promise", "video_selector failed")

    scene_plan = read_artifact(work_dir, "scene_plan") or {"scenes": []}
    script = read_artifact(work_dir, "script")
    proposal = read_artifact(work_dir, "proposal_packet")
    research = read_artifact(work_dir, "research_brief")
    brief = read_artifact(work_dir, "brief")
    playbook = _playbook_name(proposal, prefs)
    composition_mode = mode.resolve(pipeline_name, prefs, proposal, canned=canned_mode)
    telemetry.set_meta(composition_mode=composition_mode)

    if pipeline_name == "screen-demo" and not canned_mode:
        want_real = str(prefs.get("screen_mode") or "").lower() == "real_capture" or mode.screen_is_gui(topic)
        if want_real:
            captured = directors.try_real_capture(project_dir(work_dir) / "footage" / "capture.mp4", topic)
            if captured:
                footage = footage or [captured]
                telemetry.set_meta(screen_mode="real_capture")
            elif str(prefs.get("screen_mode") or "").lower() == "real_capture":
                return _fail("real_capture", "screen_recorder/cap_recorder failed for requested real_capture")
            else:
                telemetry.set_meta(screen_mode="synthetic_terminal")
        else:
            telemetry.set_meta(screen_mode="synthetic_terminal")

    if not regen_id:
        try:
            with telemetry.span("assets"):
                director_manifest = None
                skip: set[str] = set()
                if not canned_mode and not mock and client is not None:
                    director_manifest = asset_director.run(
                        client,
                        work_dir=work_dir,
                        manifest=manifest,
                        pipeline_name=pipeline_name,
                        playbook=playbook,
                        duration=duration,
                        long_form=long_form,
                        topic=topic,
                        prefs=prefs,
                        retries=retries,
                    )
                    skip = asset_director.skip_types(director_manifest)
                locale = str(prefs.get("target_locale") or prefs.get("locale") or prefs.get("language") or "")
                manifest_payload = asset_gather.gather(
                    work_dir,
                    pipeline=pipeline_name,
                    scene_plan=scene_plan,
                    script=script,
                    footage=footage,
                    canned=canned_mode,
                    topic=topic,
                    duration=duration,
                    require_karaoke=(not canned_mode)
                    and (not mock)
                    and pipeline_name in {"animated-explainer", "animation", "hybrid"},
                    require_music=(not canned_mode)
                    and (not mock)
                    and pipeline_name in {"animated-explainer", "animation"}
                    and prefs.get("music") is not False,
                    voice_id=prefs.get("voice_id"),
                    research=research,
                    skip_types=skip,
                    language_code=locale or None,
                    transcript=transcript,
                )
                manifest_payload = asset_director.merge(manifest_payload, director_manifest)
        except (asset_gather.AssetError, RuntimeError) as exc:
            return _fail("assets", str(exc))
        write_artifact(work_dir, "asset_manifest", manifest_payload)
        checkpoint.write_stage(work_dir, pipeline_name, "assets")
        log = read_artifact(work_dir, "decision_log")
        try:
            write_artifact(
                work_dir,
                "decision_log",
                decisions.from_assets(
                    log,
                    asset_manifest=manifest_payload,
                    playbook=playbook,
                    project_id=str(job.get("job_id") or "studio"),
                ),
            )
        except ValidationError:
            pass

    character_workspace = None
    if pipeline_name == "character-animation":
        if not hyperframes_ok():
            return _fail("doctor", "character-animation requires HyperFrames", runtime="hyperframes")
        try:
            with telemetry.span("character"):
                if mode.wants_ink(topic, brief or proposal, scene_plan):
                    chain = directors.run_character_chain(work_dir, topic, scene_plan, brief=brief or proposal)
                    checkpoint.write_stage(work_dir, pipeline_name, "character_design")
                else:
                    existing = read_artifact(work_dir, "character_design")
                    designed = directors.run_character_design(work_dir, topic, llm_artifact=existing)
                    checkpoint.write_stage(work_dir, pipeline_name, "character_design")
                    chain = directors.run_character_rig(
                        work_dir,
                        topic,
                        scene_plan,
                        brief=brief or proposal,
                        design=designed.get("raw") or designed.get("character_design") or existing,
                    )
                    if chain.get("edit_decisions") is None:
                        rig_art = read_artifact(work_dir, "rig_plan")
                        if not rig_art:
                            try:
                                write_artifact(
                                    work_dir,
                                    "rig_plan",
                                    directors.normalize_rig(None, topic),
                                )
                            except ValidationError:
                                pass
        except directors.CharacterError as exc:
            return _fail("character", str(exc))
        character_workspace = chain.get("workspace")
        checkpoint.write_stage(work_dir, pipeline_name, "rig_plan")

    asset_manifest = read_artifact(work_dir, "asset_manifest")
    if regen_id and composition_mode != "atelier":
        assets = asset_manifest.get("assets") if isinstance(asset_manifest, dict) else None
        if not asset_manifest or not assets:
            return _fail("assets", "templated regenerate_scene_id requires a non-empty asset_manifest")
    asset_manifest = asset_manifest or {"assets": []}
    scenes = list(scene_plan.get("scenes") or [])
    if pipeline_name == "documentary-montage" and scenes:
        if not any((s.get("type") or "").lower() == "end_tag" for s in scenes):
            last_end = max((s.get("end_seconds") or 0) for s in scenes)
            scenes.append(
                {
                    "id": "end_tag",
                    "type": "end_tag",
                    "description": "Documentary end card",
                    "start_seconds": last_end,
                    "end_seconds": last_end + 2,
                }
            )
            scene_plan["scenes"] = scenes
            write_artifact(work_dir, "scene_plan", scene_plan)

    if not canned_mode and pipeline_name in HERO_PIPELINES and motion_canvas_ok() and not regen_id:
        asset_manifest = compose.insert_motion_canvas(
            dest_dir=project_dir(work_dir) / "assets" / "video",
            scene_plan=scene_plan,
            asset_manifest=asset_manifest,
            topic=topic,
        )
        write_artifact(work_dir, "asset_manifest", asset_manifest)

    try:
        with telemetry.span("edit"):
            compiled = edl.compile_edit(
                runtime=selection["render_runtime"],
                family=selection["renderer_family"],
                composition_mode=composition_mode,
                scenes=scenes,
                asset_manifest=asset_manifest,
                research=research,
                script=script,
                topic=topic,
                playbook=playbook,
            )
            edit = compiled
            if not canned_mode and not mock and client is not None:
                edit = edit_director.run(
                    client,
                    manifest=manifest,
                    pipeline_name=pipeline_name,
                    playbook=playbook,
                    duration=duration,
                    long_form=long_form,
                    retries=retries,
                    compiled=compiled,
                )
            write_artifact(work_dir, "edit_decisions", edit)
    except ValidationError as exc:
        return _fail("schema", str(exc), stage="edit")
    checkpoint.write_stage(work_dir, pipeline_name, "edit")

    try:
        gates.slideshow(scene_plan, edit)
    except gates.GateError:
        if prefs.get("delivery_promise") == "motion_led":
            raise

    posters_dir = project_dir(work_dir) / "storyboard"
    scenes_dir = project_dir(work_dir) / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    mc_ok = motion_canvas_ok()
    scene_runtimes: list[dict[str, Any]] = []
    scene_files: list[Path] = []
    warnings: list[str] = []
    client = model or (None if canned_mode else default_model())
    atelier_skill = ""
    if composition_mode == "atelier" and (
        locked_runtime == "hyperframes" or pipeline_name == "character-animation"
    ):
        atelier_skill = _atelier_skill_text("hyperframes")
    elif composition_mode == "atelier":
        atelier_skill = _atelier_skill_text("remotion")
    if atelier_skill:
        telemetry.set_meta(atelier_skill_chars=len(atelier_skill))

    hero_master = (
        not canned_mode
        and not regen_id
        and pipeline_name in HERO_PIPELINES
        and not debug_cards
    )
    atelier_regen = bool(
        regen_id
        and composition_mode == "atelier"
        and selection.get("atelier_slug")
        and not debug_cards
    )
    if atelier_regen:
        if client is None:
            return _fail("atelier", "atelier regen requires Gemini")
        master = project_dir(work_dir) / "renders" / "atelier_master.mp4"
        generator = "atelier"
        runtime_name = "remotion"
        try:
            with telemetry.span("atelier_regen"):
                telemetry.set_role("atelier")
                dest = atelier.rerender_scene(
                    model=_atelier_client(client),
                    work_dir=work_dir,
                    slug=str(selection["atelier_slug"]),
                    composition_id=str(selection.get("composition_id") or "StudioPiece"),
                    scene_id=str(regen_id),
                    scenes=scenes,
                    scenes_dir=scenes_dir,
                    duration=duration,
                    prefs=prefs,
                    skill_text=atelier_skill,
                    retries=retries,
                    playbook=playbook,
                    regen_prompt=str(prefs.get("regen_prompt") or ""),
                )
            for scene in scenes:
                sid = scene.get("id") or f"sc{len(scene_runtimes) + 1}"
                existing = scenes_dir / f"{sid}.mp4"
                if str(sid) == str(regen_id):
                    scene_files.append(dest)
                    _mark_runtime(dest, runtime_name, generator)
                    scene_runtimes.append(
                        {"scene_id": sid, "render_runtime": runtime_name, "generator": generator}
                    )
                elif existing.is_file():
                    scene_files.append(existing)
                    meta_path = existing.with_suffix(".runtime.json")
                    runtime_keep = runtime_name
                    gen_keep = generator
                    if meta_path.is_file():
                        prev = json.loads(meta_path.read_text(encoding="utf-8"))
                        runtime_keep = prev.get("render_runtime", runtime_name)
                        gen_keep = prev.get("generator", generator)
                    scene_runtimes.append(
                        {"scene_id": sid, "render_runtime": runtime_keep, "generator": gen_keep}
                    )
                else:
                    scene_files.append(dest)
                    scene_runtimes.append(
                        {"scene_id": sid, "render_runtime": runtime_name, "generator": generator}
                    )
        except (atelier.AtelierError, compose.ComposeError, LLMError) as exc:
            return _fail("atelier", str(exc))
    elif hero_master:
        master = project_dir(work_dir) / "renders" / "master.mp4"
        master.parent.mkdir(parents=True, exist_ok=True)
        generator = "remotion"
        runtime_name = "remotion"
        try:
            if composition_mode == "atelier" and locked_runtime == "hyperframes":
                with telemetry.span("atelier"):
                    telemetry.set_role("atelier")
                    workspace = project_dir(work_dir) / "hyperframes"
                    workspace.mkdir(parents=True, exist_ok=True)
                    html = atelier.author_kinetic_html(
                        _atelier_client(client) if client is not None else default_model("atelier"),
                        topic=topic,
                        script=script,
                        duration=float(duration),
                        retries=retries,
                        skill_text=atelier_skill or _atelier_skill_text("hyperframes"),
                    )
                    if atelier.is_kinetic_stub(html):
                        return _fail("atelier", "kinetic HTML was a title-stack stub")
                    (workspace / "index.html").write_text(html, encoding="utf-8")
                    result = tools_exec.execute(
                        "hyperframes_compose",
                        {
                            "operation": "render_existing",
                            "workspace_path": str(workspace),
                            "output_path": str(master),
                            "skip_contrast": True,
                            "strict_check": False,
                            "quality": "draft",
                        },
                    )
                    if not result.success or not master.is_file():
                        raise compose.ComposeError(result.error or "hyperframes atelier render failed")
                    generator = "hyperframes"
                    runtime_name = "hyperframes"
                    character_workspace = str(workspace)
                    selection["composition_id"] = "root"
                    selection["master_path"] = str(master)
            elif composition_mode == "atelier":
                if client is None:
                    return _fail("atelier", "atelier_author requires Gemini")
                with telemetry.span("atelier"):
                    telemetry.set_role("atelier")
                    chapter_masters: list[Path] = []
                    last_authored: dict[str, Any] | None = None
                    windows = chapter_plan if long_form and len(chapter_plan) > 1 else [
                        {"id": "ch1", "start_seconds": 0, "end_seconds": duration, "target_seconds": duration}
                    ]
                    for ch in windows:
                        ch_scenes = chapters.slice_scenes(scenes, float(ch["start_seconds"]), float(ch["end_seconds"]))
                        if not ch_scenes:
                            ch_scenes = scenes
                        authored = atelier.run_atelier(
                            model=_atelier_client(client),
                            work_dir=work_dir,
                            job_id=f"{job.get('job_id') or 'studio'}-{ch.get('id')}",
                            topic=topic,
                            pipeline=pipeline_name,
                            duration=int(ch.get("target_seconds") or duration),
                            prefs=prefs,
                            scene_plan={"scenes": ch_scenes},
                            script=script,
                            asset_manifest=asset_manifest,
                            research=research,
                            skill_text=atelier_skill,
                            retries=retries,
                            playbook=playbook,
                        )
                        last_authored = authored
                        chapter_masters.append(Path(authored["master"]))
                    master = Path(last_authored["master"]) if last_authored else master
                    if len(chapter_masters) > 1:
                        stitched = project_dir(work_dir) / "renders" / "chapters_stitched.mp4"
                        compose.assemble_final(
                            strategy="scene_assemble",
                            scene_files=chapter_masters,
                            dest=stitched,
                            master=None,
                        )
                        master = stitched
                    generator = "atelier"
                    selection["atelier_slug"] = (last_authored or {}).get("slug")
                    selection["composition_id"] = (last_authored or {}).get("composition_id")
                    selection["master_path"] = str(master)
                    selection["chapter_masters"] = [str(p) for p in chapter_masters]
            else:
                with telemetry.span("compose"):
                    compose.render_explainer(
                        dest=master,
                        edit=edit,
                        asset_manifest=asset_manifest,
                        seconds=duration,
                        prefs=prefs,
                    )
                    vo = next(
                        (
                            Path(a["path"])
                            for a in (asset_manifest.get("assets") or [])
                            if a.get("type") == "narration" and Path(str(a.get("path"))).is_file()
                        ),
                        None,
                    )
                    compose.ensure_audio(master, vo, duration, canned=False)
                    generator = "remotion"
                    selection["master_path"] = str(master)
            scene_files = compose.split_master(master, scenes, scenes_dir)
            if not scene_files:
                return _fail("compose", "split_master produced no scene files")
            for scene, dest in zip(scenes, scene_files):
                _mark_runtime(dest, runtime_name, generator)
                scene_runtimes.append(
                    {
                        "scene_id": scene.get("id"),
                        "render_runtime": runtime_name,
                        "generator": generator,
                    }
                )
        except (atelier.AtelierError, compose.ComposeError, LLMError) as exc:
            return _fail("atelier" if composition_mode == "atelier" else "compose", str(exc))
    else:
        with telemetry.span("compose_scenes"):
            from runner import progress

            progress.note(f"compose {len(scenes)} scenes", kind="render")
            for scene in scenes:
                sid = scene.get("id") or f"sc{len(scene_runtimes) + 1}"
                progress.note(f"compose scene {len(scene_runtimes) + 1}/{len(scenes)} {sid}", kind="render")
                runtime = _scene_runtime(scene, pipeline_name, selection, mc_ok=mc_ok)
                dur = max(1, int((scene.get("end_seconds") or 5) - (scene.get("start_seconds") or 0)))
                dest = scenes_dir / f"{sid}.mp4"
                if regen_id and regen_id != sid and dest.is_file():
                    scene_files.append(dest)
                    meta_path = dest.with_suffix(".runtime.json")
                    runtime_name = runtime
                    if meta_path.is_file():
                        runtime_name = json.loads(meta_path.read_text(encoding="utf-8")).get("render_runtime", runtime)
                    scene_runtimes.append({"scene_id": sid, "render_runtime": runtime_name})
                    continue
                if debug_cards:
                    _debug_card(dest, dur, f"{pipeline_name} {sid}")
                    meta = {"render_runtime": runtime, "generator": "debug_card"}
                else:
                    try:
                        meta = compose.render_scene(
                            dest=dest,
                            scene=scene,
                            runtime=runtime,
                            pipeline=pipeline_name,
                            topic=topic,
                            asset_manifest=asset_manifest,
                            seconds=dur,
                            character_workspace=character_workspace,
                            script=script,
                            canned=canned_mode,
                            prefs=prefs,
                            edit=edit,
                        )
                    except compose.ComposeError as exc:
                        stype = str(scene.get("type") or "").lower()
                        if runtime == "motion_canvas" or stype in PICTURE_SCENE_TYPES:
                            warnings.append(f"picture scene {sid} failed: {exc}; no Explainer fallback")
                            telemetry.record_flag(
                                "picture_missing" if not (dest.is_file() and dest.stat().st_size > 32) else "picture_salvage",
                                f"picture scene {sid} failed: {exc}; no Explainer fallback",
                                scene_id=sid,
                            )
                            if dest.is_file() and dest.stat().st_size > 32:
                                meta = {"render_runtime": runtime, "generator": "picture_salvage"}
                            else:
                                dest.with_suffix(".runtime.json").write_text(
                                    json.dumps(
                                        {
                                            "render_runtime": runtime,
                                            "generator": "picture_missing",
                                        }
                                    ),
                                    encoding="utf-8",
                                )
                                meta = {"render_runtime": runtime, "generator": "picture_missing"}
                        else:
                            return _fail("compose", str(exc), scene_id=sid)
                scene_files.append(dest)
                scene_runtimes.append(
                    {
                        "scene_id": sid,
                        "render_runtime": meta.get("render_runtime") or runtime,
                        "generator": meta.get("generator"),
                        "waitUntil": meta.get("waitUntil"),
                    }
                )

    if pipeline_name == "cinematic" and not canned_mode and directors.has_video_api_keys() and not regen_id and not hero_master:
        clips = [Path(a["path"]) for a in (asset_manifest.get("assets") or []) if a.get("type") == "video" and Path(str(a.get("path"))).is_file()]
        if clips:
            cin = project_dir(work_dir) / "renders" / "cinematic.mp4"
            cin_edit = edl.cinematic_edit(clips=clips, topic=topic, duration=duration)
            compose.render_cinematic(dest=cin, edit=cin_edit, seconds=duration, prefs=prefs)
            selection["master_path"] = str(cin)
            scene_files = compose.split_master(cin, scenes or [{"id": "sc1", "start_seconds": 0, "end_seconds": duration}], scenes_dir)
            scene_runtimes = [
                {"scene_id": (scenes[i].get("id") if i < len(scenes) else f"sc{i+1}"), "render_runtime": "remotion", "generator": "cinematic"}
                for i, _ in enumerate(scene_files)
            ]
            for dest in scene_files:
                _mark_runtime(dest, "remotion", "cinematic")
            edit = cin_edit
            write_artifact(work_dir, "edit_decisions", edit)

    runtimes_used = {row["render_runtime"] for row in scene_runtimes if row.get("render_runtime") not in {None, "debug"}}
    if not runtimes_used:
        return _fail("compose", "no scene runtimes")
    if runtimes_used == {locked_runtime}:
        compose_strategy = "single_runtime"
    elif locked_runtime in runtimes_used or (
        locked_runtime == "remotion" and "motion_canvas" in runtimes_used
    ):
        compose_strategy = "scene_assemble"
    else:
        return _fail("silent_swap", f"locked {locked_runtime} but rendered {sorted(runtimes_used)}")
    selection["compose_strategy"] = compose_strategy
    selection["scene_runtimes"] = scene_runtimes
    selection["composition_mode"] = composition_mode
    telemetry.set_meta(compose_strategy=compose_strategy)
    write_artifact(work_dir, "render_runtime_selection", selection)
    write_artifact(work_dir, "scene_runtimes", {"version": "1.0", "scenes": scene_runtimes})

    r2_prefix = (job.get("r2") or {}).get("put_base")
    if r2_prefix:
        _upload_checkpoints(work_dir, r2_prefix)

    qa_model = _visual_qa_client(client, canned=canned_mode, mock=mock)
    with telemetry.span("visual_qa"):
        telemetry.set_role("visual_qa")
        qa_report = visual_qa.review_scenes(
            scenes=scenes,
            scene_files=scene_files,
            posters_dir=posters_dir,
            model=qa_model,
            canned=canned_mode,
            delivery_promise=str(prefs.get("delivery_promise") or "mixed"),
        )
    qa_report = _merge_mc_skips(qa_report, asset_manifest)
    failing = visual_qa.failing_hints(qa_report)
    if failing:
        with telemetry.span("atelier_rewrite"):
            telemetry.set_role("atelier")
            scene_files, scene_runtimes = _rewrite_failing_scenes(
                failing=failing,
                scenes=scenes,
                scene_files=scene_files,
                scene_runtimes=scene_runtimes,
                composition_mode=composition_mode,
                selection=selection,
                client=_atelier_client(client) if client is not None else client,
                work_dir=work_dir,
                scenes_dir=scenes_dir,
                duration=duration,
                prefs=prefs,
                skill_text=atelier_skill,
                retries=retries,
                playbook=playbook,
                pipeline_name=pipeline_name,
                topic=topic,
                asset_manifest=asset_manifest,
                character_workspace=character_workspace,
                script=script,
                canned_mode=canned_mode,
                edit=edit,
                qa_model=qa_model,
            )
        with telemetry.span("visual_qa_rewrite"):
            telemetry.set_role("visual_qa")
            qa_report = visual_qa.review_scenes(
                scenes=scenes,
                scene_files=scene_files,
                posters_dir=posters_dir,
                model=qa_model,
                canned=canned_mode,
                delivery_promise=str(prefs.get("delivery_promise") or "mixed"),
            )
        qa_report = _merge_mc_skips(qa_report, asset_manifest)
        qa_report["rewrites"] = picture_patch.VISION_PATCH_ROUNDS
        telemetry.record_flag("visual_qa_rewrite", f"{len(failing)} scene(s) patched")
        # Salvage last files even if QA still fails; do not abort the job.
    try:
        write_artifact(work_dir, "visual_qa", qa_report)
    except ValidationError:
        pass
    for row in qa_report.get("scenes") or []:
        sev = str(row.get("severity") or "")
        if row.get("pass") is False or sev in {"fail", "error"}:
            telemetry.record_flag(
                "visual_qa",
                f"{row.get('id')} {sev or 'fail'}",
                scene_id=row.get("id"),
                severity=sev,
            )
    qa_by_id = {str(row.get("id")): row for row in (qa_report.get("scenes") or [])}

    board_status = "review" if (review and not continue_after) or regen_id else "done"
    storyboard = _storyboard_payload(
        job=job,
        status=board_status,
        compose_strategy=compose_strategy,
        composition_mode=composition_mode,
        scenes=scenes,
        scene_runtimes=scene_runtimes,
        scene_files=scene_files,
        posters_dir=posters_dir,
        selection=selection,
        qa_by_id=qa_by_id,
    )
    write_artifact(work_dir, "storyboard", storyboard)

    if review and not continue_after:
        checkpoint.mark_human_approval(work_dir, "scene_review", False)
        checkpoint.write_stage(work_dir, pipeline_name, "scene_review", "waiting")
        return _finish_job({
            "status": "review",
            "work_dir": str(work_dir),
            "storyboard": storyboard,
            "scene_runtimes": scene_runtimes,
            "compose_strategy": compose_strategy,
        })

    if regen_id:
        return _finish_job({
            "status": "review",
            "work_dir": str(work_dir),
            "regenerate_scene_id": regen_id,
            "storyboard": storyboard,
            "scene_runtimes": scene_runtimes,
            "compose_strategy": compose_strategy,
        })

    renders = project_dir(work_dir) / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    final_path = renders / "final.mp4"
    stitch_files = [p for p in scene_files if p.is_file() and p.stat().st_size > 32]
    if not stitch_files and not Path(str(selection.get("master_path") or "")).is_file():
        return _fail("compose", "no picture to deliver")
    with telemetry.span("stitch"):
        master = Path(str(selection.get("master_path") or ""))
        compose.assemble_final(
            strategy=compose_strategy,
            scene_files=stitch_files,
            dest=final_path,
            master=master if master.is_file() else None,
        )

        music = next((Path(a["path"]) for a in (asset_manifest.get("assets") or []) if a.get("type") == "music" and Path(str(a.get("path"))).is_file()), None)
        vo = next((Path(a["path"]) for a in (asset_manifest.get("assets") or []) if a.get("type") == "narration" and Path(str(a.get("path"))).is_file()), None)
        width, height = mode.platform_size(prefs.get("platform_profile"))
        try:
            sine_flag = compose.ensure_audio(final_path, vo, duration, canned=canned_mode, width=width, height=height)
            if sine_flag == "sine_mux":
                warnings.append("sine_mux")
                telemetry.record_flag("audio", "sine_mux")
        except compose.ComposeError as exc:
            return _fail("compose", str(exc))
        if music and vo:
            mixed = final_path.with_name("mixed.mp4")
            mixed_out = compose.mix_music_under_vo(
                final_path, vo, music, mixed, duration, width=width, height=height
            )
            if mixed_out is not None:
                final_path = mixed_out

    if compose_strategy == "scene_assemble" and prefs.get("captions"):
        warnings.append("scene_assemble captions are per-scene; no single karaoke timeline")
        telemetry.record_flag("captions", "scene_assemble captions are per-scene; no single karaoke timeline")

    frames = _preview_frames(final_path, posters_dir)
    if _ffmpeg() and final_path.is_file() and final_path.stat().st_size > 32:
        gates.ffprobe_mp4(final_path)
        gates.audio_levels(final_path)

    generators = sorted({row.get("generator") or row.get("render_runtime") for row in scene_runtimes})
    write_artifact(
        work_dir,
        "render_report",
        {
            "version": "1.0",
            "outputs": [
                {
                    "path": str(final_path),
                    "format": "mp4",
                    "codec": "h264",
                    "audio_codec": "aac",
                    "resolution": "1920x1080",
                    "fps": 30,
                    "duration_seconds": duration,
                }
            ],
            "warnings": warnings,
            "metadata": {
                "runtime": locked_runtime,
                "compose_strategy": compose_strategy,
                "composition_mode": composition_mode,
                "generators": generators,
                "scene_runtimes": scene_runtimes,
            },
        },
    )
    checkpoint.write_stage(work_dir, pipeline_name, "compose")
    if pipeline_name in {"clip-factory", "podcast-repurpose"}:
        clip_dir = project_dir(work_dir) / "exports" / "clips"
        clip_dir.mkdir(parents=True, exist_ok=True)
        for i, clip in enumerate(stitch_files):
            dest_clip = clip_dir / f"clip_{i + 1:02d}.mp4"
            try:
                shutil.copy2(clip, dest_clip)
            except OSError:
                continue
    if pipeline_name != "documentary-montage":
        try:
            publish.run(
                work_dir,
                pipeline=pipeline_name,
                final_mp4=final_path,
                title=str((script or {}).get("title") or topic),
                chapters=chapter_plan,
                duration=duration,
            )
        except Exception as exc:
            telemetry.record_flag("publish", str(exc))
    checkpoint.write_stage(work_dir, pipeline_name, "publish")

    if r2_prefix:
        try:
            r2.put_file(r2_prefix.rstrip("/") + "/final.mp4", final_path, "video/mp4")
        except Exception as exc:
            return _fail("r2", f"r2 put: {exc}")
        _upload_checkpoints(work_dir, r2_prefix)

    return _finish_job({
        "status": "done",
        "work_dir": str(work_dir),
        "final_mp4": str(final_path),
        "posters": frames,
        "render_runtime_selection": selection,
        "scene_runtimes": scene_runtimes,
        "compose_strategy": compose_strategy,
        "generators": generators,
        "doctors": docs,
    })


def run_job(job: dict[str, Any], *, work_dir: Path, model: StageModel | None = None) -> dict[str, Any]:
    try:
        return _run_job(job, work_dir=work_dir, model=model)
    except budget.BudgetCapError as exc:
        telemetry.record_flag("budget", str(exc))
        return _fail("budget", str(exc))
    finally:
        try:
            budget.persist()
        except Exception:
            pass
        try:
            telemetry.persist()
        except Exception:
            pass
        budget.clear()
        telemetry.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--work-dir", default="")
    args = parser.parse_args(argv)
    job_path = Path(args.job)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    work = Path(args.work_dir) if args.work_dir else repo_root() / "work" / job["job_id"]
    result = run_job(job, work_dir=work)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in {"done", "review"} else 1


if __name__ == "__main__":
    sys.exit(main())
