"""Fetch a URL and extract title + text for research citations."""

from __future__ import annotations

import html as html_lib
import re
import time
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

MAX_BYTES = 400_000
TIMEOUT = 20
TAG_RE = re.compile(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", re.I)
STRIP_RE = re.compile(r"<[^>]+>")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
WS_RE = re.compile(r"\s+")


class WebFetch(BaseTool):
    name = "web_fetch"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "research"
    provider = "openmontage"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    agent_skills = []
    capabilities = ["web_fetch"]
    best_for = ["reading source_url pages cited in research_brief"]
    not_good_for = ["binary downloads", "authenticated dashboards"]
    input_schema = {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=1, network_required=True)

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        url = str(inputs.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(success=False, error="url must be http(s)", duration_seconds=round(time.time() - started, 2))
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "OpenMontageStudio/1.0 (research fetch)", "Accept": "text/html,application/xhtml+xml"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read(MAX_BYTES + 1)
                final_url = str(resp.geturl() or url)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"fetch failed: {exc}",
                duration_seconds=round(time.time() - started, 2),
            )
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
        blob = raw.decode("utf-8", errors="replace")
        title_m = TITLE_RE.search(blob)
        title = html_lib.unescape(title_m.group(1).strip()) if title_m else final_url
        title = WS_RE.sub(" ", STRIP_RE.sub("", title)).strip()[:300]
        text = TAG_RE.sub(" ", blob)
        text = html_lib.unescape(STRIP_RE.sub(" ", text))
        text = WS_RE.sub(" ", text).strip()[:8000]
        return ToolResult(
            success=True,
            data={"url": final_url, "title": title, "text": text},
            duration_seconds=round(time.time() - started, 2),
        )
