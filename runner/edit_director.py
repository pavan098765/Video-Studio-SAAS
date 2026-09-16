"""Edit stage as an LLM director. EDL compiler remains the mechanical fallback."""

from __future__ import annotations

from typing import Any

from jsonschema import ValidationError

from runner import ep_loop, pipeline, tools_exec
from runner.llm_gemini import LLMError, StageModel
from schemas.artifacts import validate_artifact


def run(
    model: StageModel,
    *,
    manifest: dict[str, Any],
    pipeline_name: str,
    playbook: str | None,
    duration: int,
    long_form: bool,
    retries: int,
    compiled: dict[str, Any],
) -> dict[str, Any]:
    tools = list(pipeline.tools_available(manifest, "edit"))
    skill = ep_loop.skill_text(
        manifest,
        "edit",
        playbook=playbook,
        pipeline_name=pipeline_name,
        duration=duration,
        tool_names=tools,
        long_form=long_form,
    )
    user = (
        "Stage=edit. Return schema-valid edit_decisions JSON. "
        "You may adjust cuts, but keep render_runtime and renderer_family from the compiled EDL. "
        f"Compiled EDL (mechanical): {str(compiled)[:4000]}"
    )
    schemas = tools_exec.tool_schemas(tools) if tools else []
    last = "edit director produced no JSON"
    for attempt in range(max(1, retries)):
        try:
            turn = model.run_stage(
                system="You are the edit director. Return ONE edit_decisions JSON. Never call shell."
                + "\n\n"
                + skill,
                user=user if attempt == 0 else f"{user}\nPrevious: {last}",
                tools=schemas,
                retry=attempt,
                on_tool=lambda n, a: tools_exec.execute(n, a or {}),
                max_rounds=4,
            )
        except LLMError as exc:
            last = str(exc)
            continue
        art = turn.artifact if isinstance(turn.artifact, dict) else None
        if not art:
            last = turn.text or last
            continue
        art.setdefault("version", "1.0")
        art.setdefault("render_runtime", compiled.get("render_runtime"))
        art.setdefault("renderer_family", compiled.get("renderer_family"))
        art.setdefault("composition_mode", compiled.get("composition_mode"))
        if not art.get("cuts"):
            art["cuts"] = compiled.get("cuts") or []
        try:
            validate_artifact("edit_decisions", art)
            return art
        except ValidationError as exc:
            last = str(exc)
            continue
    return compiled
