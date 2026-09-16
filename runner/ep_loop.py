"""Executive-producer stage loop: director skill → tools → schema → revise / send-back.

Python validates and persists. The LLM (or mock) produces the stage artifact.
"""

from __future__ import annotations

from typing import Any, Callable

from runner import chapters, pipeline, skills, tools_exec

# Every YAML stage is an LLM stage. Python does not own assets/edit/compose.
LLM_STAGES = frozenset(
    {
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
)
SKIP_LLM = frozenset()

RunStage = Callable[..., dict[str, Any]]


def max_send_backs(manifest: dict[str, Any]) -> int:
    orch = manifest.get("orchestration") or {}
    try:
        return max(1, int(orch.get("max_send_backs") or 3))
    except (TypeError, ValueError):
        return 3


def extra_skills(stage: str, pipeline_name: str, *, duration: int = 45, long_form: bool = False) -> list[str]:
    extra = ["meta/api-llm-runner", "meta/reviewer", "meta/checkpoint-protocol"]
    if stage == "research":
        extra.append("meta/api-llm-research")
    if stage in {"proposal", "idea"}:
        extra.extend(["meta/api-llm-proposal", "meta/taste-direction", "meta/animation-runtime-selector"])
    if stage in {"script", "scene_plan"}:
        extra.append("meta/api-llm-script")
    if stage == "proposal":
        extra.append("meta/bespoke-composition")
    if stage in {"proposal", "script"} and pipeline_name in {"animated-explainer", "animation"}:
        extra.append("meta/reviewer")
    if stage in {"assets", "script", "character_design"}:
        extra.append("meta/voice-performance-director")
    if stage in {"character_design", "rig_plan"}:
        extra.append("meta/reviewer")
    if stage in {"assets", "edit", "compose", "publish"}:
        extra.append("meta/reviewer")
    if stage == "compose":
        extra.extend(["meta/api-llm-atelier", "meta/bespoke-composition"])
    if long_form or duration >= 480:
        extra.append("creative/long-form")
    return extra


def skill_text(
    manifest: dict[str, Any],
    stage: str,
    *,
    playbook: str | None,
    pipeline_name: str,
    duration: int,
    tool_names: list[str],
    long_form: bool = False,
) -> str:
    extra = extra_skills(stage, pipeline_name, duration=duration, long_form=long_form)
    layer3 = tools_exec.layer3_skill_names(tool_names)
    return skills.load_stage_skill(
        manifest,
        stage,
        playbook=playbook,
        extra_skills=extra,
        layer3=layer3,
    )


def review_stage(stage: str, artifact: dict[str, Any] | None) -> dict[str, Any]:
    """Structural EP gate. LLM self-review is in the skill; this catches send-backs."""
    if not isinstance(artifact, dict):
        return {"verdict": "revise", "feedback": f"{stage} produced no JSON object", "target": stage}
    if stage == "scene_plan":
        gaps = chapters.picture_gaps(artifact)
        if gaps:
            return {
                "verdict": "send_back",
                "target": "scene_plan",
                "feedback": "Picture scenes need structure, not title cards: " + "; ".join(gaps[:8]),
            }
        scenes = artifact.get("scenes") or []
        if not scenes:
            return {"verdict": "revise", "feedback": "scene_plan.scenes is empty", "target": stage}
    if stage == "script" and not (artifact.get("sections") or artifact.get("narration") or artifact.get("title")):
        return {"verdict": "revise", "feedback": "script missing narration sections", "target": stage}
    if stage == "proposal":
        concepts = artifact.get("concept_options") or []
        if len(concepts) < 3:
            return {
                "verdict": "revise",
                "feedback": "proposal needs at least 3 distinct concept_options (taste/distinctness)",
                "target": stage,
            }
        titles = [str(c.get("title") or c.get("name") or c.get("concept_id") or "").strip().lower() for c in concepts if isinstance(c, dict)]
        if len({t for t in titles if t}) < 3:
            return {
                "verdict": "revise",
                "feedback": "concept_options are not distinct — could this be any other product's video?",
                "target": stage,
            }
    if stage == "character_design" and not (artifact.get("characters") or []):
        return {"verdict": "revise", "feedback": "character_design.characters is empty", "target": stage}
    if stage == "rig_plan" and not (artifact.get("characters") or []):
        return {"verdict": "revise", "feedback": "rig_plan.characters is empty", "target": stage}
    return {"verdict": "pass", "feedback": "", "target": stage}


def invalidate_after(stages_done: list[str], target: str) -> list[str]:
    if target not in stages_done:
        return stages_done
    idx = stages_done.index(target)
    return stages_done[:idx]


def run_llm_stages(
    *,
    stages: list[str],
    manifest: dict[str, Any],
    pipeline_name: str,
    playbook: str | None,
    duration: int,
    long_form: bool,
    retries: int,
    mock: bool,
    run_one: Callable[[str, list[str], str, str], dict[str, Any]],
    write: Callable[[str, dict[str, Any]], None],
    read: Callable[[str], dict[str, Any] | None],
    artifact_name: Callable[[str], str],
    checkpoint: Callable[[str], None],
) -> dict[str, Any]:
    """Run YAML LLM stages with revise + send-back. `run_one(stage, tools, skill, feedback)`."""
    send_backs = 0
    send_limit = max_send_backs(manifest)
    pending = [s for s in stages if s not in SKIP_LLM]
    done: list[str] = []
    i = 0
    feedback_by_stage: dict[str, str] = {}
    while i < len(pending):
        stage = pending[i]
        name = artifact_name(stage)
        tools = list(pipeline.tools_available(manifest, stage))
        skill = skill_text(
            manifest,
            stage,
            playbook=playbook,
            pipeline_name=pipeline_name,
            duration=duration,
            tool_names=tools,
            long_form=long_form,
        )
        feedback = feedback_by_stage.pop(stage, "")
        from runner import progress

        progress.note(
            f"EP {stage} ({i + 1}/{len(pending)}) tools={len(tools)}"
            + (f" retry_feedback={feedback[:80]}" if feedback else ""),
            kind="stage",
        )
        artifact: dict[str, Any] | None = None
        last_err = ""
        jumped = False
        for _attempt in range(max(1, retries)):
            try:
                artifact = run_one(stage, tools, skill, feedback)
            except Exception as exc:
                last_err = str(exc)
                feedback = last_err
                continue
            gate = review_stage(stage, artifact)
            from runner import progress

            progress.note(f"EP {stage} verdict={gate['verdict']}" + (f" {gate.get('feedback')}" if gate.get("feedback") else ""), kind="stage")
            if gate["verdict"] == "pass":
                break
            if gate["verdict"] == "send_back" and not mock and send_backs < send_limit:
                send_backs += 1
                target = str(gate.get("target") or stage)
                note = str(gate.get("feedback") or "")
                feedback_by_stage[target] = note
                done = invalidate_after(done, target)
                if target in pending:
                    i = pending.index(target)
                    jumped = True
                    break
            feedback = str(gate.get("feedback") or last_err)
        if jumped:
            continue
        if artifact is None:
            return {"error": "stage", "reason": last_err or f"stage {stage} failed", "stage": stage}
        write(name, artifact)
        checkpoint(stage)
        if stage not in done:
            done.append(stage)
        i += 1
    if not mock:
        plan = read("scene_plan")
        gaps = chapters.picture_gaps(plan)
        if gaps:
            return {
                "error": "scene_plan",
                "reason": "Motion Canvas scenes missing structure: " + "; ".join(gaps[:8]),
                "stage": "scene_plan",
            }
    return {"ok": True, "send_backs": send_backs, "stages": done}
