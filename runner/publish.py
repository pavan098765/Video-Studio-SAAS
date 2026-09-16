"""Publish stage: export_bundle + chapters.txt. Skip documentary (no publish in YAML)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner import tools_exec
from runner.artifacts import project_dir, write_artifact


def run(
    work_dir: Path,
    *,
    pipeline: str,
    final_mp4: Path,
    title: str,
    chapters: list[dict[str, Any]] | None = None,
    duration: int = 0,
) -> dict[str, Any] | None:
    if pipeline == "documentary-montage":
        return None
    dest = project_dir(work_dir) / "exports"
    dest.mkdir(parents=True, exist_ok=True)
    chapter_rows = []
    for ch in chapters or []:
        chapter_rows.append(
            {
                "start_seconds": int(ch.get("start_seconds") or 0),
                "title": str(ch.get("id") or ch.get("title") or "Chapter"),
            }
        )
    if not chapter_rows and duration:
        chapter_rows = [{"start_seconds": 0, "title": title[:80] or "Video"}]
    result = tools_exec.execute(
        "export_bundle",
        {
            "video_path": str(final_mp4),
            "title": title[:200] or pipeline,
            "project_name": project_dir(work_dir).name,
            "export_dir": str(dest),
            "chapters": chapter_rows,
            "platform": "local",
            "visibility": "unlisted",
        },
    )
    log = (result.data or {}).get("publish_log") if result.success else None
    if not isinstance(log, dict):
        log = {
            "version": "1.0",
            "entries": [
                {
                    "platform": "local",
                    "status": "exported" if result.success else "failed",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "export_path": str(dest),
                    "error": None if result.success else (result.error or "export_bundle failed"),
                    "metadata_used": {"title": title[:200], "chapters": chapter_rows},
                }
            ],
        }
        if not result.success:
            log["entries"][0].pop("error", None)
            log["entries"][0]["error"] = result.error or "export_bundle failed"
            log["entries"][0]["status"] = "failed"
    write_artifact(work_dir, "publish_log", log)
    chapters_txt = dest / "chapters.txt"
    lines = []
    for row in chapter_rows:
        sec = int(row.get("start_seconds") or 0)
        mm, ss = divmod(sec, 60)
        lines.append(f"{mm:02d}:{ss:02d} {row.get('title')}")
    chapters_txt.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return log
