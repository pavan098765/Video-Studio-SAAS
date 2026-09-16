"""Append-only decision_log. Latest (category, subject) pair is current."""

from __future__ import annotations

from typing import Any


def empty(project_id: str) -> dict[str, Any]:
    return {"version": "1.0", "project_id": str(project_id or "studio"), "decisions": []}


def option(option_id: str, label: str, score: float, reason: str, rejected_because: str | None = None) -> dict[str, Any]:
    row = {
        "option_id": option_id,
        "label": label,
        "score": max(0.0, min(1.0, float(score))),
        "reason": reason,
    }
    if rejected_because:
        row["rejected_because"] = rejected_because
    return row


def append(
    log: dict[str, Any] | None,
    *,
    decision_id: str,
    stage: str,
    category: str,
    subject: str,
    options: list[dict[str, Any]],
    selected: str,
    reason: str,
    project_id: str = "studio",
) -> dict[str, Any]:
    blob = dict(log or empty(project_id))
    blob["version"] = "1.0"
    blob["project_id"] = str(blob.get("project_id") or project_id)
    decisions = list(blob.get("decisions") or [])
    decisions.append(
        {
            "decision_id": decision_id,
            "stage": stage,
            "category": category,
            "subject": subject,
            "options_considered": options,
            "selected": selected,
            "reason": reason,
        }
    )
    blob["decisions"] = decisions
    return blob


def current(log: dict[str, Any] | None, category: str, subject: str) -> dict[str, Any] | None:
    last = None
    for row in (log or {}).get("decisions") or []:
        if row.get("category") == category and row.get("subject") == subject:
            last = row
    return last


def from_assets(
    log: dict[str, Any] | None,
    *,
    asset_manifest: dict[str, Any] | None,
    playbook: str | None,
    project_id: str,
) -> dict[str, Any]:
    """Append voice / music / playbook / image provider rows after assets exist."""
    blob = dict(log or empty(project_id))
    tools = {str(a.get("source_tool") or "") for a in (asset_manifest or {}).get("assets") or [] if isinstance(a, dict)}
    types = {str(a.get("type") or "") for a in (asset_manifest or {}).get("assets") or [] if isinstance(a, dict)}
    if playbook:
        blob = append(
            blob,
            decision_id="d-playbook",
            stage="proposal",
            category="playbook_selection",
            subject="Style playbook",
            options=[option(playbook, playbook, 0.8, "Job prefs or proposal production_plan")],
            selected=playbook,
            reason=f"Locked playbook={playbook}",
            project_id=project_id,
        )
    if "narration" in types:
        tts = "tts_selector" if "tts_selector" in tools else "tts"
        blob = append(
            blob,
            decision_id="d-voice",
            stage="assets",
            category="voice_selection",
            subject="TTS provider for narration",
            options=[
                option("tts_selector", "tts_selector (ElevenLabs when keyed)", 0.9, "YAML asset director + selector"),
                option("kokoro", "Kokoro-82M local", 0.55, "Free local TTS with English word timestamps"),
                option("piper", "Piper local", 0.4, "Fallback when no ElevenLabs key"),
            ],
            selected=tts,
            reason="Narration routed through tts_selector",
            project_id=project_id,
        )
    if "music" in types:
        src = "music_library" if "music_library" in tools else next((t for t in tools if "music" in t), "music_library")
        blob = append(
            blob,
            decision_id="d-music",
            stage="assets",
            category="music_source",
            subject="Underscore bed",
            options=[
                option("music_library", "Library first", 0.7, "Long-form and explainer prefer a bed under VO"),
                option("google_music", "Lyria generated bed", 0.55, "When stock miss and Google is keyed"),
                option("music_gen", "ElevenLabs Music", 0.5, "Needs Music SKU, not TTS-only"),
                option("pixabay_music", "Pixabay", 0.4, "Stock scrape"),
                option("freesound_music", "Freesound", 0.4, "CC stock when keyed"),
            ],
            selected=src
            if src
            in {"music_library", "google_music", "music_gen", "pixabay_music", "freesound_music"}
            else "music_library",
            reason=f"Music asset source_tool={src}",
            project_id=project_id,
        )
    if "image" in types or "image_selector" in tools:
        blob = append(
            blob,
            decision_id="d-image-provider",
            stage="assets",
            category="provider_selection",
            subject="Still image provider",
            options=[
                option("image_selector", "Live registry selector", 0.9, "Flux/stock ranked by keys — not a Pexels lock"),
                option("pexels", "Pexels stock", 0.3, "Only if selector ranks it"),
            ],
            selected="image_selector",
            reason="Asset director uses image_selector; no preferred_provider pexels lock",
            project_id=project_id,
        )
    return blob
