"""Load kala-studio-engine SaaS config and environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SAAS_PATH = REPO_ROOT / "config" / "saas.yaml"

_ROLE_YAML = {
    "default": "planner",
    "planner": "planner",
    "visual_qa": "visual_qa",
    "atelier": "atelier",
}
_ROLE_ENV = {
    "planner": "STUDIO_LLM_PLANNER",
    "visual_qa": "STUDIO_LLM_VISUAL_QA",
    "atelier": "STUDIO_LLM_ATELIER",
}


def load_saas() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if SAAS_PATH.is_file():
        data = yaml.safe_load(SAAS_PATH.read_text(encoding="utf-8")) or {}
    os.environ.setdefault("VIDEO_GEN_LOCAL_ENABLED", "false")
    os.environ.setdefault("SAAS_DISABLE_BLENDER", "1")
    if "TTS_PROVIDER" not in os.environ:
        if os.environ.get("ELEVENLABS_API_KEY"):
            os.environ["TTS_PROVIDER"] = "elevenlabs"
        else:
            os.environ["TTS_PROVIDER"] = str(data.get("tts", {}).get("provider") or "piper")
    if not data.get("blender", {}).get("enabled", False):
        os.environ["SAAS_DISABLE_BLENDER"] = "1"
    if data.get("VIDEO_GEN_LOCAL_ENABLED") is False:
        os.environ["VIDEO_GEN_LOCAL_ENABLED"] = "false"
    return data


def repo_root() -> Path:
    return REPO_ROOT


def llm_role(kind: str = "default") -> dict[str, str]:
    """Resolve provider/model/base_url for one role. Never rewrite model IDs."""
    role = _ROLE_YAML.get(kind, "planner")
    llm = load_saas().get("llm") or {}
    block = llm.get(role) if isinstance(llm.get(role), dict) else {}
    prefix = _ROLE_ENV[role]
    legacy_provider = (os.environ.get("STUDIO_LLM_PROVIDER") or "").strip()
    if legacy_provider.lower() in {"mock", "injected"}:
        legacy_provider = ""
    provider = (
        (os.environ.get(f"{prefix}_PROVIDER") or "").strip()
        or str(block.get("provider") or "").strip()
        or legacy_provider
        or str(llm.get("provider") or "").strip()
        or "gemini"
    )
    legacy_model = ""
    if role == "atelier":
        legacy_model = str(llm.get("atelier_model") or "")
    elif role == "visual_qa":
        legacy_model = str(llm.get("visual_qa_model") or "")
    else:
        legacy_model = str(llm.get("model") or "")
    model = (
        (os.environ.get(f"{prefix}_MODEL") or "").strip()
        or str(block.get("model") or "").strip()
        or legacy_model.strip()
        or str(llm.get("model") or "").strip()
        or "gemini-2.0-flash"
    )
    base_url = (
        (os.environ.get(f"{prefix}_BASE_URL") or "").strip()
        or str(block.get("base_url") or "").strip()
    )
    api_key_env = str(block.get("api_key_env") or "").strip()
    return {
        "kind": role,
        "provider": provider.lower(),
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env,
    }


def llm_provider(kind: str = "default") -> str:
    return llm_role(kind)["provider"]


def llm_model_name(kind: str = "default", provider: str | None = None) -> str:
    del provider  # model IDs are never rewritten from the provider name
    return llm_role(kind)["model"]


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)
