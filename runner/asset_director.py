"""Assets stage as an LLM director that may call selectors; Python still executes gather()."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runner import ep_loop, pipeline, tools_exec
from runner.llm_gemini import LLMError, StageModel


def run(
    model: StageModel,
    *,
    work_dir: Path,
    manifest: dict[str, Any],
    pipeline_name: str,
    playbook: str | None,
    duration: int,
    long_form: bool,
    topic: str,
    prefs: dict[str, Any],
    retries: int,
) -> dict[str, Any] | None:
    """Director may call YAML asset tools. Returns a partial asset_manifest or None."""
    tools = list(pipeline.tools_available(manifest, "assets"))
    if not tools:
        return None
    skill = ep_loop.skill_text(
        manifest,
        "assets",
        playbook=playbook,
        pipeline_name=pipeline_name,
        duration=duration,
        tool_names=tools,
        long_form=long_form,
    )
    user = (
        f"Stage=assets. Call declared selectors (tts_selector, image_selector, video_selector, "
        f"music_gen, diagram_gen) as needed for topic={topic!r}. Return asset_manifest JSON "
        f"with version 1.0 and assets[]. Do not call shell."
    )
    schemas = tools_exec.tool_schemas(tools)
    last = "assets director produced no JSON"
    for attempt in range(max(1, retries)):
        try:
            turn = model.run_stage(
                system="You are the asset director. Call declared tools. Return ONE asset_manifest JSON."
                + "\n\n"
                + skill,
                user=user if attempt == 0 else f"{user}\nPrevious attempt: {last}",
                tools=schemas,
                retry=attempt,
                on_tool=lambda n, a: tools_exec.execute(n, a or {}),
                max_rounds=8,
            )
        except LLMError as exc:
            last = str(exc)
            continue
        art = turn.artifact if isinstance(turn.artifact, dict) else None
        if art and isinstance(art.get("assets"), list):
            art.setdefault("version", "1.0")
            return art
        last = turn.text or last
    return None


def merge(base: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    if not extra:
        return base
    assets = list(base.get("assets") or [])
    seen = {str(a.get("id")) for a in assets if isinstance(a, dict)}
    for row in extra.get("assets") or []:
        if not isinstance(row, dict):
            continue
        aid = str(row.get("id") or "")
        path = Path(str(row.get("path") or ""))
        if not path.is_file():
            continue
        if aid and aid not in seen:
            assets.append(row)
            seen.add(aid)
    out = dict(base)
    out["assets"] = assets
    return out


def skip_types(extra: dict[str, Any] | None) -> set[str]:
    types: set[str] = set()
    for row in (extra or {}).get("assets") or []:
        if isinstance(row, dict) and Path(str(row.get("path") or "")).is_file():
            types.add(str(row.get("type") or ""))
    return types
