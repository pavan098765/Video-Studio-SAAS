"""OpenMontage API agent loop. Python executes tools and persists; the LLM owns every YAML stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from lib import checkpoint as om_checkpoint
from lib.pipeline_loader import get_stage_human_approval_default, get_stage_sub_stages
from lib.source_media_review import review_source_media
from schemas.artifacts import ARTIFACT_NAMES, load_schema, validate_artifact

from runner import budget, chapters, checkpoint, envelope, ep_loop, ingest, pipeline, progress, reference, route, skills, telemetry, tools_exec
from runner.artifacts import project_dir, read_artifact, write_artifact
from runner.config import load_saas
from runner.ffmpeg_bin import pin_scratch_temp, prepend_to_path
from runner.llm_gemini import LLMError, MockGemini, StageModel, default_model
from runner.preflight import doctors
from runner.workspace_root import set_root

FILE_TOOLS = ["read_file", "write_file", "list_dir"]
COMPOSE_TOOLS = FILE_TOOLS + ["atelier_still", "typecheck_atelier", "hyperframes_lint", "hyperframes_validate"]
REFERENCE_TOOLS = FILE_TOOLS + [
    "video_analyzer",
    "transcript_fetcher",
    "video_downloader",
    "scene_detect",
    "frame_sampler",
]
ATELIER_STAGES = frozenset(
    {"assets", "compose", "character_design", "rig_plan", "edit", "publish"}
)
SHORTLIST_STAGES = frozenset({"proposal", "idea"})
FOOTAGE_REQUIRED = ingest.REQUIRED_FOOTAGE
VIDEO_KEY_PIPELINES = {"cinematic", "avatar-spokesperson"}

STAGE_ARTIFACT = {
    "research": "research_brief",
    "proposal": "proposal_packet",
    "idea": "brief",
    "script": "script",
    "scene_plan": "scene_plan",
    "assets": "asset_manifest",
    "edit": "edit_decisions",
    "character_design": "character_design",
    "rig_plan": "rig_plan",
    "compose": "render_report",
    "publish": "publish_log",
}

PRIOR_NAMES = [
    "research_brief",
    "proposal_packet",
    "brief",
    "script",
    "scene_plan",
    "character_design",
    "rig_plan",
    "pose_library",
    "action_timeline",
    "asset_manifest",
    "edit_decisions",
    "source_media_review",
    "video_analysis_brief",
    "decision_log",
    "final_review",
    "character_qa_report",
    "capability_envelope",
]


def artifact_name(stage: str) -> str:
    return STAGE_ARTIFACT.get(stage, stage)


def model_role(stage: str) -> str:
    """Compose/assets/atelier work uses the atelier-tier API model — not Flash Lite."""
    return "atelier" if stage in ATELIER_STAGES else "planner"


def reviewer_role(stage: str) -> str:
    return "atelier" if stage == "compose" else "visual_qa"


def _is_mock(model: StageModel | None) -> bool:
    return (
        isinstance(model, MockGemini)
        or type(model).__name__ == "MockGemini"
        or bool(getattr(model, "is_mock", False))
    )


def _fail(error: str, reason: str, **extra: Any) -> dict[str, Any]:
    from runner.loop import _fail as loop_fail

    return loop_fail(error, reason, **extra)


def _finish(payload: dict[str, Any]) -> dict[str, Any]:
    from runner.loop import _finish_job

    return _finish_job(payload)


def _on_tool(name: str, arguments: dict[str, Any]) -> Any:
    return tools_exec.execute(name, arguments or {})


def max_rounds_for(stage: str) -> int:
    if stage in {"assets", "compose"} or stage.endswith(".sample"):
        return 48
    if stage in {"research", "edit", "publish"}:
        return 24
    return 16


def stage_tools(manifest: dict[str, Any], stage: str) -> list[str]:
    names = list(pipeline.tools_available(manifest, stage))
    extra = COMPOSE_TOOLS if stage == "compose" else FILE_TOOLS
    for name in extra:
        if name not in names:
            names.append(name)
    return names


def stage_produces(manifest: dict[str, Any], stage: str) -> list[str]:
    produces = pipeline.stage_entry(manifest, stage).get("produces") or []
    names = [str(item) for item in produces if item]
    primary = artifact_name(stage)
    if primary not in names:
        names.insert(0, primary)
    return names


def interpret_review(review: dict[str, Any] | None) -> dict[str, Any]:
    """CHAI gate. Critical findings require proposed_fix."""
    if not isinstance(review, dict):
        return {"verdict": "revise", "feedback": "reviewer returned no JSON", "target": ""}
    verdict = str(review.get("verdict") or review.get("decision") or "").lower()
    findings = review.get("findings") if isinstance(review.get("findings"), list) else []
    for row in findings:
        if not isinstance(row, dict):
            continue
        sev = str(row.get("severity") or "").lower()
        if sev == "critical" and not str(row.get("proposed_fix") or "").strip():
            return {
                "verdict": "revise",
                "feedback": f"critical finding {row.get('id') or ''} missing proposed_fix",
                "target": str(review.get("stage") or ""),
            }
    if verdict in {"pass", "pass_with_warnings"}:
        return {"verdict": "pass", "feedback": str(review.get("feedback") or ""), "target": str(review.get("target") or review.get("stage") or "")}
    if verdict in {"send_back", "sendback"}:
        return {
            "verdict": "send_back",
            "feedback": str(review.get("feedback") or review.get("reason") or "send_back"),
            "target": str(review.get("target") or review.get("stage") or ""),
        }
    if verdict in {"revise", "fail"}:
        return {
            "verdict": "revise",
            "feedback": str(review.get("feedback") or review.get("reason") or "revise"),
            "target": str(review.get("target") or review.get("stage") or ""),
        }
    if any(str((f or {}).get("severity") or "").lower() == "critical" for f in findings if isinstance(f, dict)):
        return {
            "verdict": "revise",
            "feedback": str(review.get("feedback") or "critical reviewer findings"),
            "target": str(review.get("stage") or ""),
        }
    return {"verdict": "pass", "feedback": "", "target": str(review.get("stage") or "")}


def schema_prompt(name: str) -> str:
    """Full artifact schema. Truncating this is how Flash Lite invents field names."""
    try:
        return json.dumps(load_schema(name), default=str)
    except FileNotFoundError:
        return ""


def review_siblings_satisfy(
    gate: dict[str, Any],
    siblings: dict[str, Any] | None,
    *,
    stage: str,
    engines: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cursor OM can read sibling files. Do not fail CHAI for a log Python already wrote."""
    if not isinstance(gate, dict) or str(gate.get("verdict") or "") == "pass":
        return gate
    log = (siblings or {}).get("decision_log") if isinstance((siblings or {}).get("decision_log"), dict) else None
    target = str(gate.get("target") or "").lower()
    feedback = str(gate.get("feedback") or "").lower()
    mentions_log = "decision_log" in target or "decision_log" in feedback or "decision log" in feedback
    if mentions_log and log and not shortlist_missing(stage, log, engines or {}):
        out = dict(gate)
        out["verdict"] = "pass"
        out["resolved_by"] = "workspace_decision_log"
        out["blocking"] = False
        return out
    return gate


def stamp_human_gate(
    artifact: dict[str, Any],
    *,
    artifact_name: str,
    prefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SaaS stand-in for the Cursor OM human turn on proposal_packet.

    The director writes ``approval.status: pending``. After the user says go,
    OM updates only schema-legal keys. Auto-decide is that second write: it
    replaces ``approval`` entirely so director audit fields never persist.
    """
    if artifact_name != "proposal_packet" or not isinstance(artifact, dict):
        return artifact
    approval: dict[str, Any] = {"status": "approved"}
    cap = (prefs or {}).get("budget_cap_usd")
    if isinstance(cap, (int, float)) and cap >= 0:
        approval["approved_budget_usd"] = float(cap)
    stamped = dict(artifact)
    stamped["approval"] = approval
    return stamped


def split_payload(raw: dict[str, Any], primary: str, produces: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Peel YAML sibling artifacts off the model JSON before schema-validating the primary."""
    extras: dict[str, dict[str, Any]] = {}
    src = raw.get("artifacts") if isinstance(raw.get("artifacts"), dict) else raw
    if not isinstance(src, dict):
        return raw, extras
    wanted = set(produces) | {"decision_log", "pose_library", "action_timeline", "final_review", "character_qa_report"}
    if primary in src and isinstance(src[primary], dict):
        for key, value in src.items():
            if key in wanted and key != primary and isinstance(value, dict):
                extras[key] = value
        return src[primary], extras
    blob = dict(raw)
    blob.pop("artifacts", None)
    for key in list(blob):
        if key in wanted and key != primary and isinstance(blob[key], dict):
            extras[key] = blob.pop(key)
    return blob, extras


def _both_runtimes_logged(artifact: dict[str, Any], engines: dict[str, Any], log: dict[str, Any] | None = None) -> bool:
    remotion = bool((engines or {}).get("remotion"))
    hyper = bool((engines or {}).get("hyperframes"))
    if not (remotion and hyper):
        return True
    blob = log if isinstance(log, dict) else {}
    nested = artifact.get("decision_log") if isinstance(artifact.get("decision_log"), dict) else {}
    rows = list(blob.get("decisions") or []) + list(nested.get("decisions") or [])
    for row in rows:
        if row.get("category") != "render_runtime_selection":
            continue
        ids = {str((opt or {}).get("option_id") or "") for opt in (row.get("options_considered") or []) if isinstance(opt, dict)}
        if "remotion" in ids and "hyperframes" in ids:
            return True
    plan = artifact.get("production_plan") if isinstance(artifact.get("production_plan"), dict) else {}
    considered = plan.get("runtimes_considered") or artifact.get("runtimes_considered")
    if isinstance(considered, list) and "remotion" in considered and "hyperframes" in considered:
        return True
    return False


def _shortlist_categories(stage: str, engines: dict[str, Any]) -> list[str]:
    if stage not in SHORTLIST_STAGES:
        return []
    names = ["composition_mode", "playbook_selection", "budget_tradeoff"]
    if stage == "proposal":
        names.insert(0, "concept_selection")
    if bool((engines or {}).get("remotion")) and bool((engines or {}).get("hyperframes")):
        names.append("render_runtime_selection")
    return names


def _logged_categories(log: dict[str, Any] | None) -> set[str]:
    return {str(row.get("category") or "") for row in (log or {}).get("decisions") or [] if isinstance(row, dict)}


def _user_payload(*, work_dir: Path, prefs: dict[str, Any], pipeline_name: str, extra: dict[str, Any]) -> str:
    prior = {name: read_artifact(work_dir, name) for name in PRIOR_NAMES}
    prior = {key: value for key, value in prior.items() if value}
    blob: dict[str, Any] = {"prefs": prefs, "pipeline": pipeline_name, "prior_artifacts": prior}
    blob.update(extra)
    return json.dumps(blob, default=str)


def _clients(injected: StageModel | None) -> dict[str, StageModel]:
    if injected is not None:
        return {"planner": injected, "atelier": injected, "visual_qa": injected}
    return {
        "planner": default_model("planner"),
        "atelier": default_model("atelier"),
        "visual_qa": default_model("visual_qa"),
    }


def _client_for(clients: dict[str, StageModel], stage: str) -> StageModel:
    return clients[model_role(stage)]


def _reviewer_client(clients: dict[str, StageModel], stage: str) -> StageModel:
    return clients[reviewer_role(stage)]


def _persist_log(work_dir: Path, log: dict[str, Any]) -> dict[str, Any]:
    from runner import decisions

    project_id = str(project_dir(work_dir).name)
    blob = dict(log or decisions.empty(project_id))
    blob["version"] = "1.0"
    blob["project_id"] = str(blob.get("project_id") or project_id)
    write_artifact(work_dir, "decision_log", blob)
    return blob


def _append_log(
    work_dir: Path,
    *,
    decision_id: str,
    stage: str,
    category: str,
    subject: str,
    options: list[dict[str, Any]],
    selected: str,
    reason: str,
) -> dict[str, Any]:
    from runner import decisions

    project_id = str(project_dir(work_dir).name)
    log = read_artifact(work_dir, "decision_log") or decisions.empty(project_id)
    if decisions.current(log, category, subject) and decisions.current(log, category, subject).get("selected") == selected:
        return log
    log = decisions.append(
        log,
        decision_id=decision_id,
        stage=stage,
        category=category,
        subject=subject,
        options=options,
        selected=selected,
        reason=reason,
        project_id=project_id,
    )
    return _persist_log(work_dir, log)


def _note_model_fallback(work_dir: Path, stage: str, client: StageModel) -> None:
    from runner import decisions

    used = str(getattr(client, "model", "") or "")
    configured = str(getattr(client, "configured_model", "") or used)
    if not used or not configured or used == configured:
        return
    _append_log(
        work_dir,
        decision_id=f"d-fallback-{stage}-{used}",
        stage=stage,
        category="fallback_decision",
        subject=f"API model for {stage}",
        options=[
            decisions.option(configured, configured, 0.9, "Configured role model"),
            decisions.option(used, used, 0.6, "Provider fallback after the configured model failed"),
        ],
        selected=used,
        reason=f"Fell back from {configured} to {used}",
    )


def materialize_shortlist(
    work_dir: Path,
    *,
    stage: str,
    artifact: dict[str, Any],
    engines: dict[str, Any],
    prefs: dict[str, Any],
    pipeline_name: str = "",
) -> dict[str, Any]:
    """Auto-decide writes the OM shortlist. Completing a gate without it is forbidden."""
    from runner import decisions

    project_id = str(project_dir(work_dir).name)
    log = read_artifact(work_dir, "decision_log") or decisions.empty(project_id)
    plan = artifact.get("production_plan") if isinstance(artifact.get("production_plan"), dict) else {}
    cats = _logged_categories(log)

    if "concept_selection" not in cats and stage == "proposal":
        concepts = [c for c in (artifact.get("concept_options") or []) if isinstance(c, dict)]
        selected = str((artifact.get("selected_concept") or {}).get("concept_id") or "")
        if concepts and selected:
            options = [
                decisions.option(
                    str(c.get("id") or f"c{i}"),
                    str(c.get("title") or c.get("id") or f"c{i}"),
                    0.9 if str(c.get("id")) == selected else 0.5,
                    str(c.get("why_this_works") or c.get("hook") or "concept"),
                )
                for i, c in enumerate(concepts, start=1)
            ]
            log = decisions.append(
                log,
                decision_id="d-concept",
                stage=stage,
                category="concept_selection",
                subject="Selected concept",
                options=options or [decisions.option(selected, selected, 0.8, "selected")],
                selected=selected,
                reason=str((artifact.get("selected_concept") or {}).get("rationale") or "Auto-decided concept"),
                project_id=project_id,
            )

    runtime = str(plan.get("render_runtime") or prefs.get("render_runtime") or "remotion")
    if runtime not in {"remotion", "hyperframes", "ffmpeg"}:
        runtime = "remotion"
    if "render_runtime_selection" not in _logged_categories(log):
        options = [
            decisions.option("remotion", "Remotion", 0.85 if runtime == "remotion" else 0.7, "React scene runtime"),
            decisions.option("hyperframes", "HyperFrames", 0.85 if runtime == "hyperframes" else 0.7, "HTML/CSS/GSAP runtime"),
        ]
        if not (engines.get("remotion") and engines.get("hyperframes")):
            options = [decisions.option(runtime, runtime, 0.8, "Only available runtime")]
        log = decisions.append(
            log,
            decision_id="d-runtime",
            stage=stage,
            category="render_runtime_selection",
            subject="Render runtime",
            options=options,
            selected=runtime,
            reason=f"Auto-decided render_runtime={runtime}",
            project_id=project_id,
        )

    mode = str(plan.get("composition_mode") or prefs.get("composition_mode") or "")
    if mode not in {"templated", "atelier"}:
        mode = "atelier" if pipeline_name in {"animated-explainer", "animation", "character-animation", "cinematic"} else "templated"
    if "composition_mode" not in _logged_categories(log):
        log = decisions.append(
            log,
            decision_id="d-mode",
            stage=stage,
            category="composition_mode",
            subject="Composition authoring mode",
            options=[
                decisions.option("atelier", "Atelier (hand-authored)", 0.85 if mode == "atelier" else 0.55, "Fresh composition, no stock Explainer reuse"),
                decisions.option("templated", "Templated (stock scenes)", 0.85 if mode == "templated" else 0.55, "Assemble cut.type scenes"),
            ],
            selected=mode,
            reason=f"Auto-decided composition_mode={mode}",
            project_id=project_id,
        )

    playbook = str(plan.get("playbook") or artifact.get("style") or prefs.get("style_playbook") or "clean-professional")
    if "playbook_selection" not in _logged_categories(log):
        log = decisions.append(
            log,
            decision_id="d-playbook",
            stage=stage,
            category="playbook_selection",
            subject="Style playbook",
            options=[
                decisions.option(playbook, playbook, 0.85, "Locked taste/playbook"),
                decisions.option("clean-professional", "clean-professional", 0.5, "Neutral default"),
            ],
            selected=playbook,
            reason=f"Auto-decided playbook={playbook}",
            project_id=project_id,
        )

    if "budget_tradeoff" not in _logged_categories(log):
        estimate = artifact.get("cost_estimate") if isinstance(artifact.get("cost_estimate"), dict) else {}
        verdict = str(estimate.get("budget_verdict") or "no_budget_set")
        log = decisions.append(
            log,
            decision_id="d-budget",
            stage=stage,
            category="budget_tradeoff",
            subject="Production budget",
            options=[
                decisions.option("proceed", "Proceed at estimated cost", 0.8, verdict),
                decisions.option("downgrade", "Downgrade providers", 0.4, "Only if over budget"),
            ],
            selected="proceed" if verdict != "over_budget" else "downgrade",
            reason=f"Auto-decided budget_verdict={verdict}",
            project_id=project_id,
        )
    return _persist_log(work_dir, log)


def shortlist_missing(stage: str, log: dict[str, Any] | None, engines: dict[str, Any]) -> list[str]:
    need = _shortlist_categories(stage, engines)
    have = _logged_categories(log)
    return [name for name in need if name not in have]


def _stub_pose_library(rig: dict[str, Any] | None) -> dict[str, Any]:
    chars: list[dict[str, Any]] = []
    for row in (rig or {}).get("characters") or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("character_id") or row.get("id") or "hero")
        poses: dict[str, Any] = {}
        for pose in row.get("required_poses") or ["idle"]:
            poses[str(pose)] = {"description": str(pose), "hold_frames": 4, "transition": "ease"}
        if not poses:
            poses["idle"] = {"description": "idle", "hold_frames": 4}
        chars.append({"character_id": cid, "poses": poses})
    if not chars:
        chars = [{"character_id": "hero", "poses": {"idle": {"description": "idle", "hold_frames": 4}}}]
    return {"version": "1.0", "characters": chars}


def _stub_action_timeline(plan: dict[str, Any] | None) -> dict[str, Any]:
    scenes: list[dict[str, Any]] = []
    for sc in (plan or {}).get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        sid = str(sc.get("id") or sc.get("scene_id") or "sc1")
        start = float(sc.get("start_seconds") or 0)
        end = float(sc.get("end_seconds") or start + 1)
        raw_actions = sc.get("actions") if isinstance(sc.get("actions"), list) else []
        actions: list[dict[str, Any]] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            actions.append(
                {
                    "at_seconds": float(item.get("at_seconds") or start),
                    "character_id": str(item.get("character_id") or "hero"),
                    "action": str(item.get("action") or "idle"),
                }
            )
        if not actions:
            actions = [{"at_seconds": start, "character_id": "hero", "action": "idle"}]
        scenes.append({"scene_id": sid, "start_seconds": start, "end_seconds": max(end, start + 0.1), "actions": actions})
    if not scenes:
        scenes = [
            {
                "scene_id": "sc1",
                "start_seconds": 0,
                "end_seconds": 1,
                "actions": [{"at_seconds": 0, "character_id": "hero", "action": "idle"}],
            }
        ]
    return {"version": "1.0", "fps": 30, "scenes": scenes}


def _stub_final_review(output_path: str, stills: list[str], *, status: str = "pass") -> dict[str, Any]:
    sampled = stills[:8] or ["snapshots/qa_00.png", "snapshots/qa_01.png", "snapshots/qa_02.png", "snapshots/qa_03.png"]
    return {
        "version": "1.0",
        "output_path": output_path or "renders/final.mp4",
        "status": status if status in {"pass", "revise", "fail"} else "pass",
        "checks": {
            "technical_probe": {"valid_container": True, "issues": []},
            "visual_spotcheck": {
                "frames_sampled": max(4, len(sampled)),
                "frame_paths": sampled,
                "black_frames_detected": False,
                "broken_overlays": False,
                "missing_assets": False,
                "unreadable_text": False,
                "issues": [],
            },
            "audio_spotcheck": {
                "narration_present": True,
                "music_present": False,
                "unexpected_silence": False,
                "clipping_detected": False,
                "mix_intelligible": True,
                "issues": [],
            },
            "promise_preservation": {
                "delivery_promise_honored": True,
                "runtime_swap_detected": False,
                "silent_downgrade_detected": False,
                "issues": [],
            },
            "subtitle_check": {
                "subtitles_expected": False,
                "subtitles_present": False,
                "coverage_ratio": 0,
                "timing_drift_detected": False,
                "issues": [],
            },
        },
        "issues_found": [],
        "recommended_action": "present_to_user" if status == "pass" else "re_render",
    }


def _stub_character_qa() -> dict[str, Any]:
    return {
        "version": "1.0",
        "status": "pass",
        "checks": {
            "schema_valid": True,
            "assets_exist": True,
            "pivots_defined": True,
            "poses_defined": True,
            "actions_timed": True,
            "motion_detected": True,
            "browser_preview_checked": False,
            "frame_samples_checked": True,
        },
        "issues": [],
        "recommended_action": "present_to_user",
    }


def _synthesize(
    name: str,
    work_dir: Path,
    artifact: dict[str, Any],
    stills: list[Path],
    *,
    mock: bool,
) -> dict[str, Any] | None:
    if name == "decision_log":
        return read_artifact(work_dir, "decision_log")
    if not mock:
        return None
    if name == "pose_library":
        return _stub_pose_library(artifact if artifact.get("characters") else read_artifact(work_dir, "rig_plan"))
    if name == "action_timeline":
        return _stub_action_timeline(read_artifact(work_dir, "scene_plan"))
    if name == "final_review":
        final = _find_final(work_dir, read_artifact(work_dir, "render_report") or artifact)
        return _stub_final_review(str(final) if final else "renders/final.mp4", [str(p) for p in stills])
    if name == "character_qa_report":
        return _stub_character_qa()
    return None


def persist_produces(
    work_dir: Path,
    *,
    produces: list[str],
    primary: str,
    artifact: dict[str, Any],
    extras: dict[str, dict[str, Any]],
    mock: bool,
    stills: list[Path],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    written: dict[str, dict[str, Any]] = {primary: artifact}
    write_artifact(work_dir, primary, artifact)
    missing: list[str] = []
    for name in produces:
        if name == primary:
            continue
        payload = extras.get(name) or _synthesize(name, work_dir, artifact, stills, mock=mock)
        if payload is None:
            if not mock:
                missing.append(name)
            continue
        try:
            write_artifact(work_dir, name, payload)
            written[name] = payload
        except (ValidationError, TypeError):
            if mock:
                continue
            missing.append(name)
    log = read_artifact(work_dir, "decision_log")
    if isinstance(log, dict):
        written["decision_log"] = log
    return written, missing


def _run_llm(
    model: StageModel,
    *,
    stage: str,
    skill_text: str,
    user: str,
    tool_names: list[str],
    retries: int,
    artifact: str,
    produces: list[str],
    prefs: dict[str, Any] | None = None,
    work_dir: Path | None = None,
    mock: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    schemas = tools_exec.tool_schemas(tool_names)
    last_err = "no JSON artifact"
    telemetry.set_role(getattr(model, "role", None) or model_role(stage))
    schema = schema_prompt(artifact)
    system = (
        "You are the OpenMontage stage director for this SaaS job. "
        "Return schema-valid JSON for this stage. If YAML produces multiple artifacts, "
        "include them as sibling keys. Never call shell/bash/npx/python. "
        "Call declared tools yourself. Do not wait for a human. "
        "Leave proposal approval pending; the runner stamps the human gate.\n\n"
        + skill_text
    )
    if schema:
        system += f"\n\nJSON schema for {artifact} (full, do not invent extra or renamed keys):\n{schema}"
    writers = [name for name in tool_names if name not in {"read_file", "list_dir"}]
    force_tools = (not mock) and bool(writers)

    def on_artifact(raw: dict[str, Any]) -> str | None:
        if mock or work_dir is None:
            return None
        primary, extras = split_payload(raw, artifact, produces)
        primary = stamp_human_gate(primary, artifact_name=artifact, prefs=prefs)
        try:
            if artifact in ARTIFACT_NAMES:
                validate_artifact(artifact, primary)
            for extra_name, extra_payload in extras.items():
                if extra_name in ARTIFACT_NAMES:
                    validate_artifact(extra_name, extra_payload)
        except ValidationError as exc:
            return f"schema invalid: {exc}"
        missing_files = claimed_files_missing(stage, primary, work_dir)
        if missing_files:
            return (
                "claimed files missing from disk — call YAML tools, do not invent paths: "
                + "; ".join(missing_files)
            )
        for name in produces:
            if name in {artifact, "decision_log"}:
                continue
            if name not in extras:
                return (
                    f"YAML produces {name} as a sibling JSON key. Include it after calling the stage tools. "
                    "Do not omit it and do not wait for Python to invent it."
                )
        return None

    extra_kwargs: dict[str, Any] = {}
    if not mock:
        extra_kwargs["on_artifact"] = on_artifact
        extra_kwargs["force_tools"] = force_tools
    with telemetry.span(stage):
        for attempt in range(max(1, retries)):
            turn = model.run_stage(
                system=system,
                user=user if attempt == 0 else f"{user}\n\nPrevious attempt failed schema: {last_err}",
                tools=schemas,
                retry=attempt,
                on_tool=_on_tool,
                max_rounds=max_rounds_for(stage),
                **extra_kwargs,
            )
            raw = turn.artifact if isinstance(turn.artifact, dict) else None
            if not raw:
                last_err = turn.text or last_err
                continue
            primary, extras = split_payload(raw, artifact, produces)
            primary = stamp_human_gate(primary, artifact_name=artifact, prefs=prefs)
            try:
                if artifact in ARTIFACT_NAMES:
                    validate_artifact(artifact, primary)
                for extra_name, extra_payload in extras.items():
                    if extra_name in ARTIFACT_NAMES:
                        validate_artifact(extra_name, extra_payload)
            except ValidationError as exc:
                last_err = str(exc)
                continue
            if not mock and work_dir is not None:
                missing_files = claimed_files_missing(stage, primary, work_dir)
                if missing_files:
                    last_err = "claimed files missing from disk — call YAML tools, do not invent paths: " + "; ".join(
                        missing_files
                    )
                    continue
                missing_prod = [
                    name for name in produces if name not in {artifact, "decision_log"} and name not in extras
                ]
                if missing_prod:
                    last_err = "missing YAML sibling artifacts: " + ", ".join(missing_prod)
                    continue
            return primary, extras
        raise LLMError(last_err or f"stage {stage} failed")


def _collect_stills(work_dir: Path, report: dict[str, Any] | None) -> list[Path]:
    proj = project_dir(work_dir)
    snaps = sorted((proj / "snapshots").glob("*.png"))
    if snaps:
        return snaps[:8]
    final = _find_final(work_dir, report)
    if final is None:
        return []
    try:
        from runner import visual_qa

        return visual_qa.extract_stills(final, proj / "snapshots" / "qa", count=4)
    except Exception:
        return []


def _reviewer_pass(
    model: StageModel,
    *,
    stage: str,
    artifact: dict[str, Any],
    mock: bool,
    stills: list[Path],
    siblings: dict[str, Any] | None = None,
    engines: dict[str, Any] | None = None,
) -> dict[str, Any]:
    structural = ep_loop.review_stage(stage, artifact)
    if structural["verdict"] != "pass":
        structural["source"] = "structural"
        structural["blocking"] = True
        return structural
    if mock:
        return {"verdict": "pass", "feedback": "", "target": stage, "findings": [], "source": "mock", "blocking": False}
    if stage == "compose" and not stills:
        return {
            "verdict": "revise",
            "feedback": "compose produced no inspectable frames — call video_compose so an mp4 exists, then sample stills",
            "target": stage,
            "findings": [
                {
                    "id": "compose-no-stills",
                    "severity": "critical",
                    "description": "No render stills on disk. A JSON render_report is not a video.",
                    "proposed_fix": "Call video_compose or hyperframes_compose, then atelier_still / inspect frames.",
                }
            ],
            "source": "stills",
            "blocking": True,
        }
    if stage == "compose" and stills and hasattr(model, "generate_vision"):
        try:
            turn = model.generate_vision(  # type: ignore[attr-defined]
                system=(
                    "You are the OpenMontage reviewer looking at actual stills/frames. "
                    "Apply CHAI. Return JSON {verdict, findings, feedback, target, proposed_fix}. "
                    "Critical if black/empty frames, unreadable type, stock Explainer look, broken rig."
                ),
                user=json.dumps({"stage": stage, "stills": [str(p) for p in stills], "artifact_keys": list(artifact)}, default=str)[:8000],
                images=stills[:8],
            )
            vision = turn.artifact if isinstance(turn.artifact, dict) else None
            if vision:
                vision.setdefault("stage", stage)
                if str(vision.get("pass")).lower() == "false" or str(vision.get("severity") or "").lower() == "critical":
                    vision.setdefault("verdict", "revise")
                    findings = vision.get("findings") if isinstance(vision.get("findings"), list) else []
                    if not findings:
                        vision["findings"] = [
                            {
                                "id": "vision-1",
                                "severity": "critical",
                                "description": str(vision.get("feedback") or vision.get("rewrite_hint") or "stills failed visual QA"),
                                "proposed_fix": str(vision.get("rewrite_hint") or vision.get("proposed_fix") or "rewrite the failing scene and re-render stills"),
                            }
                        ]
                gated = interpret_review(vision)
                gated["source"] = "vision"
                gated["blocking"] = gated["verdict"] != "pass"
                if gated["verdict"] != "pass":
                    return gated
        except LLMError as exc:
            return {
                "verdict": "revise",
                "feedback": f"vision reviewer failed: {exc}",
                "target": stage,
                "findings": [],
                "source": "vision",
                "blocking": True,
            }
    skill = skills.load_meta("meta/reviewer")
    payload = {
        "stage": stage,
        "sibling_artifacts": siblings or {},
        "still_paths": [str(p) for p in stills],
        "artifact": artifact,
    }
    user = json.dumps(payload, default=str)
    try:
        turn = model.run_stage(
            system=(
                "You are the OpenMontage reviewer. Apply CHAI: Accurate, Complete, Constructive. "
                "Every critical finding MUST include proposed_fix. "
                "Return JSON: {version, stage, verdict, findings, feedback, target}. "
                "verdict is pass, revise, or send_back. Do not call tools. "
                "sibling_artifacts.decision_log is already on disk — do not fail the stage for a missing log if that sibling lists both runtimes and composition modes."
            )
            + skill,
            user=user,
            tools=[],
            retry=0,
            on_tool=None,
            max_rounds=2,
        )
    except LLMError as exc:
        return {
            "verdict": "revise",
            "feedback": f"reviewer LLM failed: {exc}",
            "target": stage,
            "findings": [],
            "source": "chai",
            "blocking": False,
        }
    review = turn.artifact if isinstance(turn.artifact, dict) else {"verdict": "pass", "stage": stage, "findings": []}
    review.setdefault("stage", stage)
    gated = interpret_review(review)
    gated["source"] = "chai"
    gated["blocking"] = False
    return review_siblings_satisfy(gated, siblings, stage=stage, engines=engines)


def _exhausted_review_gate(gate: dict[str, Any], *, mock: bool, stage: str) -> dict[str, Any]:
    """Live jobs fail closed. Mock/canned may stamp pass_with_warnings for non-blocking CHAI."""
    if gate.get("verdict") == "pass":
        return gate
    if mock and not gate.get("blocking"):
        telemetry.record_flag("ep_pass_with_warnings", f"{stage}: {gate.get('feedback') or 'exhausted revisions'}")
        return {**gate, "verdict": "pass", "pass_with_warnings": True}
    return {**gate, "blocking": True}


def _existing_file(work_dir: Path, raw: str | Path, *, min_bytes: int = 1) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text)
    candidates = [path]
    if not path.is_absolute():
        proj = project_dir(work_dir)
        candidates.extend([proj / path, work_dir / path])
    for cand in candidates:
        try:
            if cand.is_file() and cand.stat().st_size >= min_bytes:
                return cand
        except OSError:
            continue
    return None


def claimed_files_missing(stage: str, artifact: dict[str, Any] | None, work_dir: Path) -> list[str]:
    """YAML success_criteria as disk facts — same class as jsonschema, not creative review."""
    blob = artifact if isinstance(artifact, dict) else {}
    missing: list[str] = []
    base = stage.split(".", 1)[0]
    if base == "compose":
        final = _find_final(work_dir, blob)
        if final is None:
            paths = [str((row or {}).get("path") or "") for row in blob.get("outputs") or []]
            claimed = ", ".join(item for item in paths if item) or "no outputs"
            missing.append(f"render output not on disk ({claimed})")
        else:
            try:
                from runner.ffmpeg_bin import find_ffprobe
                from runner.gates import GateError, ffprobe_mp4

                if find_ffprobe():
                    ffprobe_mp4(final)
            except Exception as exc:
                missing.append(f"ffprobe failed for {final}: {exc}")
    if base == "assets":
        assets = blob.get("assets") if isinstance(blob.get("assets"), list) else []
        if not assets:
            missing.append("asset_manifest.assets is empty")
        for row in assets:
            raw = str((row or {}).get("path") or "")
            if _existing_file(work_dir, raw) is None:
                missing.append(raw or str((row or {}).get("id") or "asset"))
    return missing


def _find_final(work_dir: Path, report: dict[str, Any] | None) -> Path | None:
    proj = project_dir(work_dir)
    for candidate in (
        proj / "renders" / "final.mp4",
        proj / "renders" / "master.mp4",
        proj / "renders" / "atelier_master.mp4",
    ):
        found = _existing_file(work_dir, candidate, min_bytes=32)
        if found is not None:
            return found
    for row in (report or {}).get("outputs") or []:
        found = _existing_file(work_dir, str((row or {}).get("path") or ""), min_bytes=32)
        if found is not None:
            return found
    scenes = sorted((proj / "scenes").glob("*.mp4"))
    for path in reversed(scenes):
        found = _existing_file(work_dir, path, min_bytes=32)
        if found is not None:
            return found
    return None


def _source_media_payload(paths: list[Path]) -> dict[str, Any]:
    files = []
    for path in paths:
        suffix = path.suffix.lower()
        media = "video" if suffix in {".mp4", ".mov", ".webm", ".mkv"} else "audio" if suffix in {".mp3", ".wav", ".m4a"} else "image"
        files.append(
            {
                "path": str(path),
                "media_type": media,
                "reviewed": True,
                "content_summary": f"User-supplied {media} at {path.name}",
                "usable_for": ["source footage"] if media == "video" else ["source audio"] if media == "audio" else ["reference image"],
            }
        )
    return {
        "version": "1.0",
        "files": files,
        "summary": "; ".join(f["content_summary"] for f in files) or "No source media",
        "planning_implications": ["Treat supplied media as source, not decoration"] if files else ["No source media — fully generated production"],
    }


def run(job: dict[str, Any], *, work_dir: Path, model: StageModel | None = None) -> dict[str, Any]:
    load_saas()
    prepend_to_path()
    work_dir = Path(work_dir).resolve()
    pipeline_requested = str(job.get("pipeline") or "auto")
    pipeline_name = route.resolve(job)
    prefs = job.get("prefs") or {}
    topic = str(prefs.get("topic") or pipeline_name)
    requested_duration = int(prefs.get("duration_seconds") or 45)
    duration = chapters.clamp_duration(requested_duration)
    review_mode = bool(job.get("review_mode", False))
    continue_after = bool(job.get("continue_after_review"))
    clients = _clients(model)
    mock = _is_mock(clients["planner"])
    telemetry.attach(job_id=str(job.get("job_id") or ""), pipeline=pipeline_name, work_dir=work_dir)
    telemetry.set_meta(canned=False, review=review_mode, topic=topic, duration_seconds=duration, pipeline_requested=pipeline_requested)
    progress.note(
        f"om_agent {job.get('job_id') or ''} pipeline={pipeline_name} topic={topic!r} duration={duration}s",
        kind="job",
    )

    from runner import directors

    if pipeline_name in VIDEO_KEY_PIPELINES and not directors.has_video_api_keys() and not ingest.has_media(job):
        return _fail(
            "delivery_promise",
            f"{pipeline_name} requires video keys; refusing silent slideshow",
            delivery_promise=prefs.get("delivery_promise") or "mixed",
        )

    manifest = pipeline.load(pipeline_name)
    work_dir.mkdir(parents=True, exist_ok=True)
    pin_scratch_temp(work_dir / ".tmp")
    proj = project_dir(work_dir)
    set_root(proj)
    checkpoint.init_job(work_dir, pipeline_name, topic, prefs.get("style_playbook"))
    budget.attach(prefs, canned=False, work_dir=work_dir)
    from runner.loop import _hydrate_from_r2, _upload_checkpoints

    _hydrate_from_r2(work_dir, job)

    docs = doctors()
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
            "preflight": "blocked" if missing_tools else "passed",
        },
    )
    if missing_tools and not mock:
        return _fail("preflight", "required tools unavailable: " + ", ".join(missing_tools[:8]), doctors=docs)

    from runner import decisions

    engines = {
        "remotion": bool(docs.get("remotion") if docs else ((cap or {}).get("composition_runtimes") or {}).get("remotion")),
        "hyperframes": bool(docs.get("hyperframes") if docs else ((cap or {}).get("composition_runtimes") or {}).get("hyperframes")),
        "ffmpeg": bool((docs or {}).get("ffmpeg", True)),
    }
    _append_log(
        work_dir,
        decision_id="d-pipeline",
        stage="proposal" if "proposal" in pipeline.stages(manifest) else pipeline.stages(manifest)[0],
        category="pipeline_selection",
        subject="Production pipeline",
        options=[
            decisions.option(pipeline_name, pipeline_name, 0.9, route.reason(job, pipeline_name)),
            decisions.option("auto", "Unresolved auto", 0.3, "SaaS has no human to interview; route.resolve locked a YAML"),
        ],
        selected=pipeline_name,
        reason=route.reason(job, pipeline_name) + f" (requested={pipeline_requested})",
    )
    _append_log(
        work_dir,
        decision_id="d-preflight",
        stage="proposal" if "proposal" in pipeline.stages(manifest) else pipeline.stages(manifest)[0],
        category="capability_extension",
        subject="Preflight capability menu",
        options=[
            decisions.option("remotion", "Remotion", 1.0 if engines.get("remotion") else 0.1, "Available" if engines.get("remotion") else "Missing"),
            decisions.option("hyperframes", "HyperFrames", 1.0 if engines.get("hyperframes") else 0.1, "Available" if engines.get("hyperframes") else "Missing"),
        ],
        selected="remotion" if engines.get("remotion") else "hyperframes" if engines.get("hyperframes") else "remotion",
        reason="Capability envelope presented in MACHINE_FACTS; auto-decide does not skip the menu",
    )
    if duration != requested_duration:
        _append_log(
            work_dir,
            decision_id="d-duration",
            stage="proposal" if "proposal" in pipeline.stages(manifest) else pipeline.stages(manifest)[0],
            category="fallback_decision",
            subject="Job duration",
            options=[
                decisions.option(str(requested_duration), f"{requested_duration}s requested", 0.4, "Above wall clock"),
                decisions.option(str(duration), f"{duration}s clamped", 0.8, f"chapters.MAX_DURATION={chapters.MAX_DURATION}"),
            ],
            selected=str(duration),
            reason=f"Clamped duration {requested_duration}s → {duration}s",
        )

    long_form = duration >= 480 or bool(prefs.get("long_video"))
    write_artifact(work_dir, "chapter_plan", {"version": "1.0", "chapters": chapters.plan(duration, prefs, pipeline_name)})
    retries = pipeline.max_revisions(manifest)
    send_limit = ep_loop.max_send_backs(manifest)
    playbook = prefs.get("style_playbook")

    from runner.loop import _machine_facts

    facts = _machine_facts(
        pipeline_name,
        {"render_runtime": None, "renderer_family": None},
        docs,
        {
            "capability_envelope": {
                "composition_runtimes": (cap or {}).get("composition_runtimes"),
                "runtime_warnings": (cap or {}).get("runtime_warnings") or [],
            },
            "auto_decide": True,
            "composition_mode_default": "undecided",
        },
    )

    ref_notes = reference.analyze(job, work_dir)
    if ref_notes and not mock:
        try:
            skill = skills.load_meta("meta/video-reference-analyst")
            user = json.dumps({"reference": ref_notes, "workspace": str(proj)}, default=str)[:20000]
            brief, _extras = _run_llm(
                clients["planner"],
                stage="reference_analyst",
                skill_text=skill,
                user="Stage=reference_analyst. Return video_analysis_brief JSON. " + user,
                tool_names=REFERENCE_TOOLS,
                retries=1,
                artifact="video_analysis_brief",
                produces=["video_analysis_brief"],
                work_dir=work_dir,
                mock=mock,
            )
            write_artifact(work_dir, "video_analysis_brief", brief)
        except Exception as exc:
            progress.note(f"reference_analyst LLM skipped: {exc}", kind="job")

    footage: list[Path] = []
    if ingest.has_media(job) or pipeline_name in FOOTAGE_REQUIRED:
        footage = ingest.ingest(job, work_dir)
        if pipeline_name in FOOTAGE_REQUIRED and not footage:
            return _fail(
                "delivery_promise" if pipeline_name in VIDEO_KEY_PIPELINES else "footage",
                f"{pipeline_name} needs asset_keys or asset_urls",
            )
        if footage:
            try:
                review = review_source_media(
                    footage,
                    {"pipeline_type": pipeline_name, "project_dir": str(proj)},
                    tool_registry=tools_exec.registry(),
                )
            except Exception as exc:
                if not mock:
                    return _fail("source_media_review", str(exc))
                review = _source_media_payload(footage)
                progress.note(f"source_media_review tool failed, logged treatment: {exc}", kind="job")
            try:
                write_artifact(work_dir, "source_media_review", review)
            except (ValidationError, TypeError):
                review = _source_media_payload(footage)
                write_artifact(work_dir, "source_media_review", review)
            _append_log(
                work_dir,
                decision_id="d-source",
                stage=pipeline.stages(manifest)[0],
                category="fallback_decision",
                subject="Source media treatment",
                options=[
                    decisions.option("use_as_source", "Use as source footage", 0.85, "User supplied media"),
                    decisions.option("generate_around", "Generate around it", 0.4, "Only if unusable"),
                ],
                selected="use_as_source",
                reason=str((review or {}).get("summary") or "Source media reviewed before planning"),
            )

    stages = pipeline.stages(manifest)
    completed = set(om_checkpoint.get_completed_stages(work_dir, proj.name, pipeline_name))
    if continue_after and "compose" in stages and _find_final(work_dir, read_artifact(work_dir, "render_report")) is None:
        completed.discard("compose")
    pending = [s for s in stages if s not in completed]
    if job.get("regenerate_scene_id") and "compose" in stages:
        pending = [s for s in pending if s == "compose" or s not in completed]
        if "compose" not in pending:
            pending.append("compose")

    send_backs = 0
    i = 0
    feedback_by_stage: dict[str, str] = {}
    while i < len(pending):
        stage = pending[i]
        name = artifact_name(stage)
        produces = stage_produces(manifest, stage)
        tools = stage_tools(manifest, stage)
        skill = ep_loop.skill_text(
            manifest,
            stage,
            playbook=playbook,
            pipeline_name=pipeline_name,
            duration=duration,
            tool_names=tools,
            long_form=long_form,
        )
        feedback = feedback_by_stage.pop(stage, "")
        progress.note(f"OM {stage} ({i + 1}/{len(pending)}) tools={len(tools)} role={model_role(stage)}", kind="stage")
        checkpoint.write_stage(work_dir, pipeline_name, stage, "in_progress", artifacts={}, human_approved=False)
        extra: dict[str, Any] = {
            "stage": stage,
            "topic": topic,
            "duration_seconds": duration,
            "MACHINE_FACTS": facts,
            "ep_feedback": feedback,
            "workspace": str(proj),
            "auto_decide": True,
            "produces": produces,
        }
        if ref_notes:
            extra["reference_notes"] = {k: v for k, v in ref_notes.items() if k != "skill_excerpt"}
        if footage:
            extra["footage_paths"] = [str(p) for p in footage]
        user = f"Stage={stage}. Return {name} JSON (plus sibling YAML produces {produces}). " + _user_payload(
            work_dir=work_dir, prefs=prefs, pipeline_name=pipeline_name, extra=extra
        )
        artifact: dict[str, Any] | None = None
        extras: dict[str, dict[str, Any]] = {}
        last_err = ""
        jumped = False
        gate = {"verdict": "revise", "feedback": "not reviewed", "target": stage}
        client = _client_for(clients, stage)
        reviewer = _reviewer_client(clients, stage)
        stills: list[Path] = []
        for _attempt in range(max(1, retries)):
            tools_exec.begin_trace()
            try:
                artifact, extras = _run_llm(
                    client,
                    stage=stage,
                    skill_text=skill,
                    user=user,
                    tool_names=tools,
                    retries=2,
                    artifact=name,
                    produces=produces,
                    prefs=prefs,
                    work_dir=work_dir,
                    mock=mock,
                )
                _note_model_fallback(work_dir, stage, client)
                if stage == "research":
                    from runner.loop import _gate_research_urls

                    _gate_research_urls(artifact, skip=mock)
                if stage in SHORTLIST_STAGES:
                    log = materialize_shortlist(
                        work_dir,
                        stage=stage,
                        artifact=artifact,
                        engines=engines,
                        prefs=prefs,
                        pipeline_name=pipeline_name,
                    )
                    extras["decision_log"] = log
                    if not _both_runtimes_logged(artifact, engines, log) and not mock:
                        raise LLMError("proposal must log both Remotion and HyperFrames when both are available")
                    missing_short = shortlist_missing(stage, log, engines)
                    if missing_short and not mock:
                        raise LLMError("decision_log missing shortlist: " + ", ".join(missing_short))
                missing_files = [] if mock else claimed_files_missing(stage, artifact, work_dir)
                if missing_files:
                    raise LLMError(
                        "claimed files missing from disk — call YAML tools, do not invent paths: "
                        + "; ".join(missing_files)
                    )
            except Exception as exc:
                last_err = str(exc)
                user = f"{user}\n\nPrevious attempt failed: {last_err}"
                continue
            stills = _collect_stills(work_dir, artifact if stage == "compose" else None)
            siblings = dict(extras)
            gate = _reviewer_pass(
                reviewer,
                stage=stage,
                artifact=artifact,
                mock=mock,
                stills=stills,
                siblings=siblings,
                engines=engines,
            )
            progress.note(f"OM {stage} verdict={gate['verdict']}", kind="stage")
            if gate["verdict"] == "pass":
                break
            if gate["verdict"] == "send_back" and not mock and send_backs < send_limit:
                send_backs += 1
                target = str(gate.get("target") or stage)
                feedback_by_stage[target] = str(gate.get("feedback") or "")
                if target in pending:
                    idx = pending.index(target)
                    for later in pending[idx:]:
                        completed.discard(later)
                    i = idx
                    jumped = True
                    break
            user = f"{user}\n\nReviewer: {gate.get('feedback') or last_err}"
        if jumped:
            continue
        if artifact is None:
            checkpoint.write_stage(work_dir, pipeline_name, stage, "failed", artifacts={}, human_approved=False, error=last_err)
            return _fail("stage", last_err or f"stage {stage} failed", stage=stage)
        missing_files = [] if mock else claimed_files_missing(stage, artifact, work_dir)
        if missing_files:
            detail = "; ".join(missing_files)
            checkpoint.write_stage(
                work_dir,
                pipeline_name,
                stage,
                "failed",
                artifacts={},
                human_approved=False,
                error=detail,
            )
            return _fail("schema", detail, stage=stage)
        artifact = stamp_human_gate(artifact, artifact_name=name, prefs=prefs)
        gate = _exhausted_review_gate(gate, mock=mock, stage=stage)
        if gate["verdict"] != "pass":
            checkpoint.write_stage(
                work_dir,
                pipeline_name,
                stage,
                "failed",
                artifacts={},
                human_approved=False,
                error=str(gate.get("feedback") or last_err or "reviewer exhausted"),
                review=gate,
            )
            return _fail("review", str(gate.get("feedback") or "reviewer exhausted without pass"), stage=stage)
        try:
            written, missing_prod = persist_produces(
                work_dir,
                produces=produces,
                primary=name,
                artifact=artifact,
                extras=extras,
                mock=mock,
                stills=stills,
            )
        except ValidationError as exc:
            return _fail("schema", str(exc), stage=stage)
        if missing_prod and not mock:
            return _fail("schema", f"stage {stage} missing YAML produces: {', '.join(missing_prod)}", stage=stage)
        if stage in {"proposal", "idea"}:
            playbook = (artifact.get("production_plan") or {}).get("playbook") or artifact.get("style") or playbook
            written["decision_log"] = materialize_shortlist(
                work_dir, stage=stage, artifact=artifact, engines=engines, prefs=prefs, pipeline_name=pipeline_name
            )
            if shortlist_missing(stage, written.get("decision_log"), engines) and not mock:
                return _fail("decision_log", "gated stage missing OM shortlist", stage=stage)
        gated = bool(get_stage_human_approval_default(manifest, stage))
        if gated and stage in SHORTLIST_STAGES and shortlist_missing(stage, read_artifact(work_dir, "decision_log"), engines):
            return _fail("decision_log", "cannot auto-approve without decision_log shortlist", stage=stage)
        checkpoint.write_stage(
            work_dir,
            pipeline_name,
            stage,
            "completed",
            artifacts=written,
            human_approved=True if gated else False,
            review={
                "verdict": "pass",
                "auto_decided": True,
                "pass_with_warnings": bool(gate.get("pass_with_warnings")),
                "review_source": gate.get("source"),
                "vision_stills": [str(p) for p in stills],
            },
            metadata={"partial_progress": {"stage": stage, "done": True}},
        )
        if not mock:
            _run_sample_substage(
                clients,
                work_dir=work_dir,
                manifest=manifest,
                stage=stage,
                pipeline_name=pipeline_name,
                playbook=playbook,
                duration=duration,
                long_form=long_form,
                prefs=prefs,
                extra_base=extra,
            )
        if stage == "compose" and review_mode and not continue_after:
            final = _find_final(work_dir, artifact)
            return _finish(
                {
                    "status": "review",
                    "work_dir": str(work_dir),
                    "final_mp4": str(final) if final else "",
                    "stage": stage,
                }
            )
        i += 1

    report = read_artifact(work_dir, "render_report")
    final = _find_final(work_dir, report)
    if not mock and "compose" in stages and final is None:
        return _fail("compose", "compose-director produced no render output")
    if final is not None:
        dest = proj / "renders" / "final.mp4"
        if final.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if not dest.is_file():
                    dest.write_bytes(final.read_bytes())
                    final = dest
            except Exception:
                pass
    r2_prefix = (job.get("r2") or {}).get("put_base")
    if r2_prefix:
        _upload_checkpoints(work_dir, r2_prefix)
        if final is not None:
            try:
                from runner import r2

                r2.put_file(str(r2_prefix).rstrip("/") + "/final.mp4", final, "video/mp4")
            except Exception:
                pass
    return _finish(
        {
            "status": "done",
            "work_dir": str(work_dir),
            "final_mp4": str(final) if final else "",
            "pipeline": pipeline_name,
            "doctors": docs,
        }
    )


def _run_sample_substage(
    clients: dict[str, StageModel],
    *,
    work_dir: Path,
    manifest: dict[str, Any],
    stage: str,
    pipeline_name: str,
    playbook: str | None,
    duration: int,
    long_form: bool,
    prefs: dict[str, Any],
    extra_base: dict[str, Any],
) -> None:
    context = {
        "video_analysis_brief_exists": bool(read_artifact(work_dir, "video_analysis_brief")),
        "approved_concept_exists": bool((read_artifact(work_dir, "proposal_packet") or {}).get("selected_concept")),
    }
    for sub in get_stage_sub_stages(manifest, stage, context=context, include_inactive=False):
        sub_name = str(sub.get("name") or "sample")
        tools = [str(t) for t in (sub.get("tools_available") or []) if t]
        for name in FILE_TOOLS:
            if name not in tools:
                tools.append(name)
        skill = ep_loop.skill_text(
            manifest,
            stage,
            playbook=playbook,
            pipeline_name=pipeline_name,
            duration=min(15, duration),
            tool_names=tools,
            long_form=long_form,
        )
        extra = dict(extra_base)
        extra["sub_stage"] = sub_name
        extra["sample"] = True
        user = (
            f"Stage={stage}.{sub_name}. YAML sample sub-stage: produce a 10-15s preview. "
            + _user_payload(work_dir=work_dir, prefs=prefs, pipeline_name=pipeline_name, extra=extra)
        )
        try:
            artifact, extras = _run_llm(
                _client_for(clients, "compose" if "video_compose" in tools else stage),
                stage=f"{stage}.{sub_name}",
                skill_text=skill + "\n\nThis is a YAML sample/preview sub-stage, not a substitute for full production.",
                user=user,
                tool_names=tools,
                retries=1,
                artifact="render_report",
                produces=["render_report"],
                work_dir=work_dir,
                mock=mock,
            )
            write_artifact(work_dir, f"{stage}_{sub_name}_render", artifact)
            for key, value in extras.items():
                write_artifact(work_dir, f"{stage}_{sub_name}_{key}", value)
        except Exception as exc:
            progress.note(f"sample sub-stage {stage}.{sub_name} skipped: {exc}", kind="stage")
