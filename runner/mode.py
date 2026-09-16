"""Lock composition_mode the way OpenMontage proposal does."""

from __future__ import annotations

from typing import Any

HERO_ATELIER = {"animated-explainer", "animation"}
HF_ATELIER = {"character-animation"}


def default_mode(pipeline: str) -> str:
    """No silent atelier/templated lock — the proposal director must log the choice."""
    del pipeline
    return "undecided"


def resolve(
    pipeline: str,
    prefs: dict[str, Any],
    proposal: dict[str, Any] | None,
    *,
    canned: bool,
) -> str:
    if canned:
        return "templated"
    override = (prefs or {}).get("composition_mode")
    if override in {"templated", "atelier"}:
        return override
    plan = (proposal or {}).get("production_plan") or {}
    locked = plan.get("composition_mode")
    if locked in {"templated", "atelier"}:
        return locked
    return default_mode(pipeline)


def platform_size(profile: str | None) -> tuple[int, int]:
    raw = (profile or "16:9").strip()
    mapping = {
        "16:9": (1920, 1080),
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "21:9": (2560, 1080),
        "youtube_landscape": (1920, 1080),
        "tiktok": (1080, 1920),
        "instagram_reels": (1080, 1920),
    }
    return mapping.get(raw, (1920, 1080))


def output_profile(profile: str | None) -> str:
    raw = (profile or "16:9").strip()
    mapping = {
        "16:9": "youtube_landscape",
        "9:16": "tiktok",
        "1:1": "instagram_feed",
        "21:9": "cinematic",
        "youtube_landscape": "youtube_landscape",
        "tiktok": "tiktok",
    }
    return mapping.get(raw, "youtube_landscape")


def wants_ink(*blobs: Any) -> bool:
    text = " ".join(str(b or "") for b in blobs).lower()
    return any(w in text for w in ("ink theater", "ink-theater", "doodle", "whiteboard", "stick figure", "sketch puppet"))


def screen_is_gui(topic: str, scene: dict[str, Any] | None = None) -> bool:
    blob = f"{topic} {(scene or {}).get('description') or ''} {(scene or {}).get('type') or ''}".lower()
    if any(w in blob for w in ("terminal", "cli", "shell", "command line", "console")):
        return False
    return any(w in blob for w in ("gui", "screenshot", "app ui", "click", "button", "dashboard", "browser"))
