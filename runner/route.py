"""Route pipeline=auto from inputs the way an OpenMontage agent would pick YAML."""

from __future__ import annotations

from typing import Any

from runner import ingest

FOOTAGE_PIPELINES = {
    "talking-head",
    "hybrid",
    "clip-factory",
    "podcast-repurpose",
    "localization-dub",
    "documentary-montage",
}


def _blob(job: dict[str, Any]) -> str:
    prefs = job.get("prefs") or {}
    parts = [
        str(prefs.get("topic") or ""),
        str(prefs.get("audience") or ""),
        str(prefs.get("source_kind") or ""),
        str(prefs.get("talking") or ""),
        str(job.get("intent") or ""),
    ]
    return " ".join(parts).lower()


def resolve(job: dict[str, Any]) -> str:
    """Return a concrete pipeline name. `auto` is resolved; others pass through."""
    name = str(job.get("pipeline") or "auto").strip() or "auto"
    if name != "auto":
        return name
    prefs = job.get("prefs") or {}
    duration = int(prefs.get("duration_seconds") or 45)
    long_video = bool(prefs.get("long_video")) or duration >= 480
    footage = ingest.has_media(job)
    text = _blob(job)
    locale = str(prefs.get("target_locale") or prefs.get("locale") or "")

    if footage and (locale or "dub" in text or "translat" in text or "localiz" in text):
        return "localization-dub"
    if footage and ("podcast" in text or prefs.get("source_kind") == "podcast"):
        return "podcast-repurpose"
    if footage and (long_video or "clip factory" in text or "highlight" in text or "clips" in text):
        return "clip-factory"
    if footage and ("talking head" in text or "talking-head" in text or "spokesperson footage" in text):
        return "talking-head"
    if footage and ("hybrid" in text or "overlay" in text or "b-roll" in text or "broll" in text):
        return "hybrid"
    if footage and long_video:
        return "documentary-montage"
    if footage:
        return "hybrid"
    if "avatar" in text or "spokesperson" in text or prefs.get("talking") == "avatar":
        return "avatar-spokesperson"
    if "cinematic" in text or "trailer" in text or "teaser" in text:
        return "cinematic"
    if "character" in text or "cartoon" in text or "rigged" in text:
        return "character-animation"
    if "screen demo" in text or "walkthrough" in text or "terminal" in text or "cli " in text:
        return "screen-demo"
    if "motion graphic" in text or "kinetic" in text or text.strip() == "animation":
        return "animation"
    if "animation" in text and "explainer" not in text:
        return "animation"
    return "animated-explainer"


def reason(job: dict[str, Any], chosen: str) -> str:
    prefs = job.get("prefs") or {}
    if str(job.get("pipeline") or "") not in {"", "auto"}:
        return f"pipeline locked by job as {chosen}"
    if ingest.has_media(job):
        return f"auto routed to {chosen} from footage + topic={prefs.get('topic')!r}"
    return f"auto routed to {chosen} from topic={prefs.get('topic')!r}"
