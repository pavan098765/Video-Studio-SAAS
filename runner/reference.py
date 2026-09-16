"""Reference-video analysis without local Whisper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runner import tools_exec
from runner.artifacts import project_dir
from runner.skills import load_meta


def analyze(job: dict[str, Any], work_dir: Path) -> dict[str, Any] | None:
    url = job.get("reference_url") or (job.get("prefs") or {}).get("reference_url")
    if not url:
        return None
    dest = project_dir(work_dir) / "reference"
    dest.mkdir(parents=True, exist_ok=True)
    brief: dict[str, Any] = {
        "source": str(url),
        "analyst_skill": "meta/video-reference-analyst",
        "steps": [],
        "skill_excerpt": load_meta("meta/video-reference-analyst", limit=2500),
    }
    analyzed = tools_exec.execute(
        "video_analyzer",
        {"source": str(url), "output_dir": str(dest), "analysis_depth": "standard"},
    )
    if analyzed.success:
        brief["video_analyzer"] = analyzed.data
        brief["steps"].append("video_analyzer")
        if isinstance(analyzed.data, dict):
            from runner.artifacts import write_artifact

            payload = analyzed.data.get("video_analysis_brief") or analyzed.data
            if isinstance(payload, dict) and payload.get("version") == "1.0":
                try:
                    write_artifact(work_dir, "video_analysis_brief", payload)
                except Exception:
                    pass
    tf = tools_exec.execute("transcript_fetcher", {"source": str(url)})
    if tf.success:
        brief["transcript"] = tf.data
        brief["steps"].append("transcript_fetcher")
    downloaded = Path(str(url)) if Path(str(url)).is_file() else None
    if downloaded is None:
        dl = tools_exec.execute("video_downloader", {"url": str(url), "output_dir": str(dest), "format": "video"})
        if dl.success:
            if dl.artifacts:
                cand = Path(str(dl.artifacts[0]))
                if cand.is_file():
                    downloaded = cand
            if downloaded is None:
                hits = list(dest.glob("*.mp4")) + list(dest.glob("*.webm")) + list(dest.glob("*.mkv"))
                if hits:
                    downloaded = hits[0]
            brief["steps"].append("video_downloader")
    if downloaded and downloaded.is_file():
        brief["local_path"] = str(downloaded)
        scenes = dest / "scenes.json"
        sd = tools_exec.execute(
            "scene_detect",
            {"input_path": str(downloaded), "output_path": str(scenes), "method": "content"},
        )
        if sd.success:
            brief["scene_detect"] = sd.data
            brief["steps"].append("scene_detect")
        fs = tools_exec.execute(
            "frame_sampler",
            {
                "input_path": str(downloaded),
                "strategy": "count",
                "count": 8,
                "output_dir": str(dest / "frames"),
            },
        )
        if fs.success:
            brief["frame_sampler"] = fs.data
            brief["steps"].append("frame_sampler")
    (dest / "reference_notes.json").write_text(json.dumps(brief, indent=2, default=str), encoding="utf-8")
    return brief
