"""Brave Search API — OpenMontage `web_search` tool for research stages."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS = 8
# Brave's rate-limit docs use a 1s sliding window (legacy/free headers: 1 rps).
# Paid Search is advertised at 50 rps; 1.05s is safe for $5-credit / free-tier keys.
_MIN_INTERVAL_S = 1.05
_MAX_RETRIES = 3
_lock = threading.Lock()
_last_request_at = 0.0


def _pace() -> None:
    global _last_request_at
    with _lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_wait(exc: urllib.error.HTTPError) -> float:
    headers = getattr(exc, "headers", None) or {}
    raw = ""
    try:
        raw = headers.get("X-RateLimit-Reset") or headers.get("Retry-After") or ""
    except Exception:
        raw = ""
    first = str(raw).split(",")[0].strip()
    try:
        return max(_MIN_INTERVAL_S, float(first))
    except (TypeError, ValueError):
        return _MIN_INTERVAL_S * 2


class WebSearch(BaseTool):
    name = "web_search"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "research"
    provider = "brave"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    dependencies = ["env:BRAVE_API_KEY"]
    install_instructions = "Set BRAVE_API_KEY from https://brave.com/search/api/"
    agent_skills = []
    capabilities = ["web_search"]
    best_for = ["grounding research_brief data_points and bibliography URLs"]
    not_good_for = ["image generation", "video download"]
    input_schema = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "count": {"type": "integer", "minimum": 1, "maximum": 8, "default": 8},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=0, network_required=True)

    def get_status(self) -> ToolStatus:
        if os.environ.get("BRAVE_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        key = os.environ.get("BRAVE_API_KEY") or ""
        if not key:
            return ToolResult(
                success=False,
                error="BRAVE_API_KEY missing — research cannot run without Brave Search",
                duration_seconds=round(time.time() - started, 2),
            )
        query = str(inputs.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, error="query required", duration_seconds=round(time.time() - started, 2))
        count = min(MAX_RESULTS, max(1, int(inputs.get("count") or MAX_RESULTS)))
        params = urllib.parse.urlencode({"q": query, "count": count})
        req = urllib.request.Request(
            f"{BRAVE_URL}?{params}",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "X-Subscription-Token": key,
            },
            method="GET",
        )
        payload: dict[str, Any] | None = None
        last_err = ""
        for attempt in range(_MAX_RETRIES):
            _pace()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                break
            except urllib.error.HTTPError as exc:
                last_err = str(exc)
                if exc.code != 429 or attempt >= _MAX_RETRIES - 1:
                    return ToolResult(
                        success=False,
                        error=f"Brave search failed: {exc}",
                        duration_seconds=round(time.time() - started, 2),
                    )
                time.sleep(_retry_wait(exc))
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=f"Brave search failed: {exc}",
                    duration_seconds=round(time.time() - started, 2),
                )
        if not isinstance(payload, dict):
            return ToolResult(
                success=False,
                error=f"Brave search failed: {last_err or 'empty response'}",
                duration_seconds=round(time.time() - started, 2),
            )
        web = (payload.get("web") or {}).get("results") or []
        results = []
        for row in web[:count]:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            results.append(
                {
                    "title": str(row.get("title") or url),
                    "url": url,
                    "snippet": str(row.get("description") or row.get("extra_snippets") or ""),
                }
            )
        return ToolResult(
            success=True,
            data={"query": query, "results": results},
            duration_seconds=round(time.time() - started, 2),
        )
