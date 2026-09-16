"""Gemini free-tier RPM / TPM / RPD pacing for the job runner and demo_studio.

Limits are per model (Google counts each model separately). State is persisted so
sequential jobs in the same Pacific day share the daily request budget.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from runner.config import load_saas, repo_root

PT = ZoneInfo("America/Los_Angeles")
WINDOW_SEC = 60.0
IMAGE_TOKENS = 1000
MIN_ESTIMATE = 256

# Free-tier text-out models (user-provided table).
_LITE = {"rpm": 15, "tpm": 250_000, "rpd": 500}
_FLASH = {"rpm": 5, "tpm": 250_000, "rpd": 20}
_DEFAULT = dict(_FLASH)

# 3.8 Flash down through 3-flash-preview share the Flash free-tier cap.
# gemini-3-flash is not a real ID (use gemini-3-flash-preview).
# gemini-2.5-flash is closed to new keys.
_FLASH_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3-flash",
    "gemini-3.0-flash",
    "gemini-2.5-flash",
)

# Atelier only: each Flash ID has its own 20 RPD. Do not fall back to Lite —
# Lite skips tools and invents JSON. Quota exhaustion fails closed.
ATELIER_FALLBACK = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
]

# Planner / visual_qa: other pools after persistent 502/503/504. Not used for RPD.
PLANNER_FALLBACK = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-3.5-flash",
]

_BUILTIN: dict[str, dict[str, int]] = {
    "gemini-3.5-flash-lite": dict(_LITE),
    **{name: dict(_FLASH) for name in _FLASH_MODELS},
}

_lock = threading.Lock()
_sleep: Callable[[float], None] = time.sleep
_now: Callable[[], float] = time.time


class GeminiRateLimitError(RuntimeError):
    """Daily (RPD) free-tier cap reached; waiting until midnight PT is not useful mid-job."""


def set_clock(*, now: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> None:
    """Tests inject a fake clock so waits do not block."""
    global _now, _sleep
    if now is not None:
        _now = now
    if sleep is not None:
        _sleep = sleep


def reset_clock() -> None:
    set_clock(now=time.time, sleep=time.sleep)


def normalize_model(model: str) -> str:
    name = str(model or "").strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.lower()


def pacific_day(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts if ts is not None else _now(), PT)
    return when.date().isoformat()


def next_pacific_midnight(ts: float | None = None) -> datetime:
    when = datetime.fromtimestamp(ts if ts is not None else _now(), PT)
    nxt = (when + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt


def enabled() -> bool:
    raw = (os.environ.get("STUDIO_GEMINI_RATE_LIMIT") or "").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    if raw in {"1", "true", "on", "yes"}:
        return True
    block = _yaml_block()
    if "enabled" in block:
        return bool(block.get("enabled"))
    return True


def limits_for(model: str) -> dict[str, int]:
    name = normalize_model(model)
    yaml_models = _yaml_block().get("models")
    if isinstance(yaml_models, dict):
        for key, row in yaml_models.items():
            if normalize_model(str(key)) == name and isinstance(row, dict):
                return _coerce_limits(row)
    if "flash-lite" in name or name.endswith("-lite"):
        return dict(_LITE)
    if name in _BUILTIN:
        return dict(_BUILTIN[name])
    if name in _FLASH_MODELS or _is_flash_family(name):
        return dict(_FLASH)
    default = _yaml_block().get("default")
    if isinstance(default, dict):
        return _coerce_limits(default)
    return dict(_DEFAULT)


def atelier_fallback_chain(primary: str) -> list[str]:
    """Atelier Gemini walk order, starting at the configured primary."""
    configured = _yaml_fallback_models()
    chain = [normalize_model(item) for item in (configured or ATELIER_FALLBACK) if str(item).strip()]
    if not chain:
        chain = list(ATELIER_FALLBACK)
    start = normalize_model(primary)
    out: list[str] = []
    if start and start not in chain:
        out.append(start)
        rest = chain
    elif start in chain:
        rest = chain[chain.index(start) :]
    else:
        rest = chain
    for name in rest:
        if name and name not in out:
            out.append(name)
    return out


def atelier_models_to_try(primary: str) -> list[str]:
    """Skip models already RPD-exhausted or 404 for this Pacific day; honor sticky fallback."""
    chain = atelier_fallback_chain(primary)
    sticky = _atelier_sticky()
    if sticky in chain:
        chain = chain[chain.index(sticky) :]
    return [name for name in chain if not is_unavailable(name) and not rpd_exhausted(name)]


def planner_fallback_chain(primary: str) -> list[str]:
    """Planner/visual_qa walk order after a persistent capacity error on the primary."""
    configured = _yaml_role_fallback_models("planner") or _yaml_role_fallback_models("visual_qa")
    chain = [normalize_model(item) for item in (configured or PLANNER_FALLBACK) if str(item).strip()]
    if not chain:
        chain = list(PLANNER_FALLBACK)
    start = normalize_model(primary)
    out: list[str] = []
    if start and start not in chain:
        out.append(start)
        rest = chain
    elif start in chain:
        rest = chain[chain.index(start) :]
    else:
        rest = chain
    for name in rest:
        if name and name not in out:
            out.append(name)
    return out


def planner_models_to_try(primary: str) -> list[str]:
    """Skip 404-marked IDs. Keep RPD-exhausted primary first so acquire() fails closed."""
    chain = planner_fallback_chain(primary)
    live = [name for name in chain if not is_unavailable(name)]
    return live or chain


def rpd_exhausted(model: str) -> bool:
    if not enabled():
        return False
    name = normalize_model(model)
    state = _load()
    bucket = _bucket(state, name)
    _prune(bucket, _now())
    return int(bucket.get("rpd") or 0) >= int(limits_for(name)["rpd"])


def is_unavailable(model: str) -> bool:
    name = normalize_model(model)
    row = (_load().get("unavailable") or {}).get(name)
    if not isinstance(row, dict):
        return False
    return str(row.get("day") or "") == pacific_day()


def mark_unavailable(model: str, reason: str = "not_found") -> None:
    name = normalize_model(model)
    with _lock:
        state = _load()
        unavailable = state.setdefault("unavailable", {})
        unavailable[name] = {"day": pacific_day(), "reason": str(reason)[:120]}
        _save(state)


def mark_rpd_exhausted(model: str) -> None:
    """Treat a daily 429 as RPD full so the next atelier call skips this model."""
    name = normalize_model(model)
    with _lock:
        state = _load()
        bucket = _bucket(state, name)
        bucket["rpd"] = max(int(bucket.get("rpd") or 0), int(limits_for(name)["rpd"]))
        _save(state)


def set_atelier_sticky(model: str) -> None:
    name = normalize_model(model)
    with _lock:
        state = _load()
        state["atelier_sticky"] = {"day": pacific_day(), "model": name}
        _save(state)


def clear_atelier_sticky() -> None:
    with _lock:
        state = _load()
        state.pop("atelier_sticky", None)
        _save(state)


def _atelier_sticky() -> str:
    row = _load().get("atelier_sticky")
    if not isinstance(row, dict):
        return ""
    if str(row.get("day") or "") != pacific_day():
        return ""
    return normalize_model(str(row.get("model") or ""))


def _is_flash_family(name: str) -> bool:
    if "flash-lite" in name or name.endswith("-lite") or "image" in name:
        return False
    return name.startswith("gemini-2.5-flash") or (name.startswith("gemini-3") and "flash" in name)


def _yaml_fallback_models() -> list[str]:
    return _yaml_role_fallback_models("atelier") or _yaml_list(_yaml_block().get("atelier_fallback"))


def _yaml_role_fallback_models(role: str) -> list[str]:
    llm = load_saas().get("llm") or {}
    block = llm.get(role) if isinstance(llm.get(role), dict) else {}
    return _yaml_list(block.get("fallback_models") if isinstance(block, dict) else None)


def _yaml_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def estimate_request_tokens(body: dict[str, Any] | None) -> int:
    """Count prompt text only. Ignore base64 so vision TPM waits stay sane."""
    if not isinstance(body, dict):
        return MIN_ESTIMATE
    chars = 0
    images = 0

    def walk(node: Any) -> None:
        nonlocal chars, images
        if isinstance(node, str):
            chars += len(node)
            return
        if isinstance(node, dict):
            if node.get("inlineData") or node.get("inline_data"):
                images += 1
                return
            if "text" in node and isinstance(node.get("text"), str):
                chars += len(node["text"])
                for key, value in node.items():
                    if key != "text":
                        walk(value)
                return
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(body.get("system_instruction") or body.get("systemInstruction"))
    walk(body.get("contents"))
    return max(MIN_ESTIMATE, chars // 4 + images * IMAGE_TOKENS)


def acquire(model: str, estimated_tokens: int = MIN_ESTIMATE) -> float:
    """Block until this Gemini call is within RPM/TPM. Raises on RPD exhaustion.

    Returns seconds waited.
    """
    if not enabled():
        return 0.0
    name = normalize_model(model)
    estimated = max(MIN_ESTIMATE, int(estimated_tokens or MIN_ESTIMATE))
    waited = 0.0
    announced = False
    while True:
        with _lock:
            state = _load()
            bucket = _bucket(state, name)
            limits = limits_for(name)
            now = _now()
            _prune(bucket, now)
            rpd_limit = int(limits["rpd"])
            if int(bucket.get("rpd") or 0) >= rpd_limit:
                reset_at = next_pacific_midnight(now)
                raise GeminiRateLimitError(
                    f"{name} free-tier RPD exhausted ({bucket.get('rpd')}/{rpd_limit} requests). "
                    f"Resets {reset_at.strftime('%Y-%m-%d %H:%M')} Pacific."
                )
            wait = _wait_seconds(bucket, limits, estimated, now)
            if wait <= 0:
                _commit(bucket, estimated, now)
                _save(state)
                if waited >= 0.5:
                    _note(name, bucket, limits, waited, "proceeding")
                    _flag(name, waited, limits, bucket)
                return waited
            note_bucket = {
                "window": list(bucket.get("window") or []),
                "rpd": bucket.get("rpd"),
            }
        if not announced:
            _note(name, note_bucket, limits, wait, "waiting")
            announced = True
        slice_s = min(max(wait, 0.05), 5.0)
        _sleep(slice_s)
        waited += slice_s


def record(model: str, actual_tokens: int) -> None:
    """Replace the last in-window estimate with provider-reported usage."""
    if not enabled():
        return
    tokens = int(actual_tokens or 0)
    if tokens <= 0:
        return
    name = normalize_model(model)
    with _lock:
        state = _load()
        bucket = _bucket(state, name)
        window = bucket.get("window") or []
        if window:
            window[-1][1] = tokens
            bucket["window"] = window
            _save(state)


def snapshot(model: str | None = None) -> dict[str, Any]:
    state = _load()
    if model:
        name = normalize_model(model)
        bucket = _bucket(state, name)
        now = _now()
        _prune(bucket, now)
        limits = limits_for(name)
        return {
            "model": name,
            "limits": limits,
            "rpm_used": len(bucket.get("window") or []),
            "tpm_used": sum(int(row[1]) for row in (bucket.get("window") or [])),
            "rpd_used": int(bucket.get("rpd") or 0),
            "day": bucket.get("day"),
        }
    return state


def quota_path() -> Path:
    override = (os.environ.get("STUDIO_GEMINI_QUOTA_PATH") or "").strip()
    if override:
        return Path(override)
    return repo_root() / "work" / ".gemini_quota.json"


def _yaml_block() -> dict[str, Any]:
    llm = load_saas().get("llm") or {}
    block = llm.get("gemini_rate_limit")
    return block if isinstance(block, dict) else {}


def _coerce_limits(row: dict[str, Any]) -> dict[str, int]:
    base = dict(_DEFAULT)
    for key in ("rpm", "tpm", "rpd"):
        if row.get(key) is not None:
            try:
                base[key] = int(row[key])
            except (TypeError, ValueError):
                pass
    return base


def _load() -> dict[str, Any]:
    path = quota_path()
    if not path.is_file():
        return {"models": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"models": {}}
    if not isinstance(data, dict):
        return {"models": {}}
    models = data.get("models")
    if not isinstance(models, dict):
        data["models"] = {}
    return data


def _save(state: dict[str, Any]) -> None:
    path = quota_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _bucket(state: dict[str, Any], name: str) -> dict[str, Any]:
    models = state.setdefault("models", {})
    bucket = models.get(name)
    if not isinstance(bucket, dict):
        bucket = {"day": pacific_day(), "rpd": 0, "window": []}
        models[name] = bucket
    day = pacific_day()
    if bucket.get("day") != day:
        bucket["day"] = day
        bucket["rpd"] = 0
        bucket["window"] = []
    if not isinstance(bucket.get("window"), list):
        bucket["window"] = []
    return bucket


def _prune(bucket: dict[str, Any], now: float) -> None:
    cutoff = now - WINDOW_SEC
    window = [row for row in (bucket.get("window") or []) if isinstance(row, list) and len(row) >= 2 and float(row[0]) > cutoff]
    bucket["window"] = window


def _wait_seconds(bucket: dict[str, Any], limits: dict[str, int], estimated: int, now: float) -> float:
    window = bucket.get("window") or []
    rpm = max(1, int(limits["rpm"]))
    tpm = max(1, int(limits["tpm"]))
    rpm_wait = 0.0
    if len(window) >= rpm:
        oldest = float(window[0][0])
        rpm_wait = max(0.0, oldest + WINDOW_SEC - now)
    used = sum(int(row[1]) for row in window)
    tpm_wait = 0.0
    if used + estimated > tpm:
        if estimated >= tpm:
            if window:
                tpm_wait = max(0.0, float(window[-1][0]) + WINDOW_SEC - now)
        else:
            dropped = 0
            for row in window:
                dropped += int(row[1])
                if used - dropped + estimated <= tpm:
                    tpm_wait = max(0.0, float(row[0]) + WINDOW_SEC - now)
                    break
            else:
                tpm_wait = max(0.0, float(window[-1][0]) + WINDOW_SEC - now) if window else 0.0
    return max(rpm_wait, tpm_wait)


def _commit(bucket: dict[str, Any], estimated: int, now: float) -> None:
    window = list(bucket.get("window") or [])
    window.append([now, estimated])
    bucket["window"] = window
    bucket["rpd"] = int(bucket.get("rpd") or 0) + 1


def _note(name: str, bucket: dict[str, Any], limits: dict[str, int], wait: float, action: str) -> None:
    rpm_used = len(bucket.get("window") or [])
    tpm_used = sum(int(row[1]) for row in (bucket.get("window") or []))
    rpd_used = int(bucket.get("rpd") or 0)
    print(
        f"[gemini-rate-limit] {name} {action} {wait:.1f}s "
        f"(rpm {rpm_used}/{limits['rpm']}, tpm {tpm_used}/{limits['tpm']}, "
        f"rpd {rpd_used}/{limits['rpd']})",
        file=sys.stderr,
        flush=True,
    )


def _flag(name: str, waited: float, limits: dict[str, int], bucket: dict[str, Any]) -> None:
    try:
        from runner import telemetry

        telemetry.record_flag(
            "gemini_rate_limit",
            f"{name} waited {waited:.1f}s",
            model=name,
            wait_seconds=round(waited, 3),
            rpd=int(bucket.get("rpd") or 0),
            rpd_limit=int(limits["rpd"]),
        )
    except Exception:
        pass
