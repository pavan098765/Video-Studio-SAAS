"""Motion Canvas compose tool — generate a project from scene_plan and render."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
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

ADAPTER = Path(__file__).resolve().parent / "mc_adapter"


def _chrome_path() -> str | None:
    env = os.environ.get("CHROME_PATH") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if env and Path(env).is_file():
        return env
    root = Path(__file__).resolve().parents[2]
    hits = list((root / "remotion-composer" / "node_modules" / ".remotion").rglob("chrome-headless-shell*"))
    for hit in hits:
        if hit.is_file() and hit.suffix.lower() in {".exe", ""}:
            return str(hit)
        if hit.is_file() and hit.name.startswith("chrome-headless-shell"):
            return str(hit)
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _escape_ts(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${").replace('"', '\\"')


def _mermaid_nodes(definition: str) -> list[str]:
    """Pull node labels from mermaid: A[Foo], A["Foo"], A(Foo), A{Foo}."""
    labels: list[str] = []
    for match in re.finditer(
        r"(?:\[|\(|\{)\s*(?:[\"']([^\"']+)[\"']|([^\]\)}]+))\s*(?:\]|\)|\})",
        definition or "",
    ):
        label = (match.group(1) or match.group(2) or "").strip()
        if not label or label.lower() in {"graph", "flowchart"}:
            continue
        if label not in labels:
            labels.append(label[:48])
        if len(labels) >= 6:
            break
    if labels:
        return labels
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]{0,24})\b", definition or ""):
        word = match.group(1)
        if word.lower() in {"flowchart", "graph", "lr", "td", "tb", "rl", "bt"}:
            continue
        if word not in labels:
            labels.append(word)
        if len(labels) >= 6:
            break
    return labels or ["node"]


def _mp4_is_blank(path: Path) -> bool:
    """True when every sampled frame is a flat field (failed MC canvas capture)."""
    if not path.is_file():
        return True
    try:
        from PIL import Image
    except ImportError:
        return False
    from runner.ffmpeg_bin import find_ffmpeg, prepend_to_path

    prepend_to_path()
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    tmp = path.with_name(path.stem + ".__blank.png")
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0.4",
                "-i",
                str(path),
                "-frames:v",
                "1",
                str(tmp),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not tmp.is_file():
            return True
        image = Image.open(tmp).convert("L").resize((48, 27))
        pixels = list(image.getdata())
        mean = sum(pixels) / max(1, len(pixels))
        var = sum((pixel - mean) ** 2 for pixel in pixels) / max(1, len(pixels))
        return var < 8
    finally:
        tmp.unlink(missing_ok=True)


def generate_scene_tsx(
    *,
    scene_type: str,
    title: str,
    duration: float,
    timestamps: list[dict[str, Any]] | None = None,
    mermaid: str = "",
    code_text: str = "",
    formula: str = "",
    allow_mc_fixture: bool = False,
) -> str | None:
    label = _escape_ts(title or scene_type)
    wait_hits = ""
    if timestamps:
        lines: list[str] = []
        prev = 0.0
        for i, row in enumerate(timestamps[:24]):
            if not isinstance(row, dict):
                continue
            end = row.get("end") or row.get("end_seconds") or row.get("t") or ((i + 1) * 0.25)
            try:
                at = float(end)
            except (TypeError, ValueError):
                continue
            delta = max(0.0, at - prev)
            if at > float(duration) + 0.08:
                break
            lines.append(f"  // waitUntil('w{i}') at {at:.3f}s")
            lines.append(f"  yield* waitFor({delta:.3f});")
            prev = at
        wait_hits = "\n".join(lines) + "\n"
    kind = (scene_type or "diagram").lower()
    if kind == "code":
        snippet = (code_text or "").strip() or f"def explain(topic: str) -> str:\n    return {title!r}\n"
        escaped = _escape_ts(snippet[:600])
        body = f'''
  const code = createRef<Txt>();
  view.add(<Txt ref={{code}} fill="#e2e8f0" fontFamily="Arial" fontSize={{28}} text="" />);
  yield* code().text("{escaped}", 1.4);
'''
    elif kind == "math":
        tex = (formula or title or "E = mc^2").strip()
        escaped = _escape_ts(tex[:120])
        body = f'''
  const tex = createRef<Txt>();
  view.add(<Txt ref={{tex}} fill="#f8fafc" fontFamily="Arial" fontSize={{56}} text="" />);
  // TeX / MathTex: {escaped}
  yield* tex().text("{escaped}", 1.5);
'''
    elif mermaid and kind in {"diagram", "map", "etymology", "timeline"}:
        nodes = _mermaid_nodes(mermaid)
        cards: list[str] = []
        for node in nodes:
            escaped = _escape_ts(node)
            cards.append(
                "      <Rect layout width={280} height={72} fill=\"#38bdf8\" radius={12} justifyContent=\"center\" alignItems=\"center\">\n"
                f'        <Txt fill="#0f172a" fontFamily="Arial" fontSize={{22}} text="{escaped}" />\n'
                "      </Rect>"
            )
        joined = "\n".join(cards)
        body = f'''
  const row = createRef<Layout>();
  const caption = createRef<Txt>();
  view.add(
    <Layout layout width={{1920}} height={{1080}} direction="column" gap={{40}} alignItems="center" justifyContent="center">
      <Layout ref={{row}} layout direction="row" gap={{18}} alignItems="center">
{joined}
      </Layout>
      <Txt ref={{caption}} fill="#f8fafc" fontFamily="Arial" fontSize={{28}} text="{label}" />
    </Layout>,
  );
  yield* row().scale(0.96, 0).to(1, 0.4);
'''
    elif kind in {"map", "etymology", "timeline", "diagram"}:
        if not allow_mc_fixture:
            return None
        body = f'''
  const a = createRef<Circle>();
  const b = createRef<Circle>();
  const line = createRef<Line>();
  const caption = createRef<Txt>();
  view.add(
    <>
      <Circle ref={{a}} size={{48}} fill="#38bdf8" x={{-320}} />
      <Circle ref={{b}} size={{48}} fill="#f472b6" x={{320}} />
      <Line ref={{line}} points={{[[-320,0],[320,0]]}} stroke="#64748b" lineWidth={{6}} end={{0}} />
      <Txt ref={{caption}} y={{160}} fill="#f8fafc" fontSize={{40}} text="{label}" />
    </>,
  );
  yield* line().end(1, 1.4);
  yield* all(a().scale(1.2, 0.4), b().scale(1.2, 0.4));
'''
    else:
        body = f'''
  const box = createRef<Rect>();
  const caption = createRef<Txt>();
  const dot = createRef<Circle>();
  view.add(
    <Layout layout direction="column" gap={{32}} alignItems="center">
      <Rect ref={{box}} width={{560}} height={{180}} fill="#1e293b" radius={{18}} />
      <Txt ref={{caption}} fill="#f8fafc" fontSize={{42}} text="{label}" />
      <Circle ref={{dot}} size={{36}} fill="#22d3ee" />
    </Layout>,
  );
  yield* all(box().opacity(0).opacity(1, 0.5), caption().opacity(0).opacity(1, 0.5));
  yield* dot().position.x(200, 1).to(-200, 1).to(0, 1);
'''
    hold = max(0.3, min(4.0, float(duration) * 0.25))
    return f'''import {{makeScene2D, Circle, Layout, Line, Rect, Txt}} from '@motion-canvas/2d';
import {{createRef}} from '@motion-canvas/core';
import {{all, waitFor, waitUntil}} from '@motion-canvas/core/lib/flow';

export default makeScene2D(function* (view) {{
  view.fill('#0f172a');
{body}
{wait_hits}  yield* waitFor({hold:.2f});
}});
'''


class MotionCanvasCompose(BaseTool):
    name = "motion_canvas_compose"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "compose"
    provider = "motion_canvas"
    stability = ToolStability.BETA
    runtime = ToolRuntime.LOCAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=2048, vram_mb=0, disk_mb=500)
    agent_skills = ["motion-canvas"]
    capabilities = ["render", "doctor", "generate"]
    best_for = ["diagrams", "code", "math", "maps", "timelines", "narration-synced motion"]
    not_good_for = ["TalkingHead", "character rigs", "karaoke-only captions"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": ["doctor", "generate", "render"]},
            "workspace_path": {"type": "string"},
            "output_path": {"type": "string"},
            "scene_type": {"type": "string"},
            "title": {"type": "string"},
            "duration_seconds": {"type": "number"},
            "timestamps": {"type": "array"},
            "mermaid": {"type": "string"},
            "code_text": {"type": "string"},
            "formula": {"type": "string"},
        },
    }

    def get_status(self) -> ToolStatus:
        if (
            (ADAPTER / "package.json").is_file()
            and (ADAPTER / "node_modules").is_dir()
            and shutil.which("npx")
        ):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        info["render_runtime"] = "motion_canvas"
        info["adapter"] = str(ADAPTER)
        info["node_modules"] = (ADAPTER / "node_modules").is_dir()
        return info

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        op = inputs["operation"]
        if op == "doctor":
            ok = self.get_status() == ToolStatus.AVAILABLE
            return ToolResult(
                success=ok,
                data={"available": ok, "adapter": str(ADAPTER)},
                duration_seconds=round(time.time() - start, 2),
            )
        workspace = Path(inputs.get("workspace_path") or (Path.cwd() / "motion_canvas"))
        workspace.mkdir(parents=True, exist_ok=True)
        self._materialize_workspace(workspace)
        scene_tsx = generate_scene_tsx(
            scene_type=str(inputs.get("scene_type") or "diagram"),
            title=str(inputs.get("title") or "Scene"),
            duration=float(inputs.get("duration_seconds") or 5),
            timestamps=inputs.get("timestamps") if isinstance(inputs.get("timestamps"), list) else None,
            mermaid=str(inputs.get("mermaid") or ""),
            code_text=str(inputs.get("code_text") or ""),
            formula=str(inputs.get("formula") or ""),
            allow_mc_fixture=bool(inputs.get("allow_mc_fixture", False)),
        )
        if not scene_tsx:
            return ToolResult(
                success=False,
                error="mermaid required for diagram/map/timeline/etymology (two-circle fixture disabled)",
                duration_seconds=round(time.time() - start, 2),
            )
        scene_path = workspace / "src" / "scenes" / "generated.tsx"
        scene_path.parent.mkdir(parents=True, exist_ok=True)
        scene_path.write_text(scene_tsx, encoding="utf-8")
        events = workspace / "src" / "waituntil.json"
        events.write_text(
            json.dumps({"waitUntil": True, "timestamps": inputs.get("timestamps") or []}),
            encoding="utf-8",
        )
        if op == "generate":
            return ToolResult(
                success=True,
                data={"workspace": str(workspace), "scene": str(scene_path), "waitUntil": True},
                artifacts=[str(scene_path)],
                duration_seconds=round(time.time() - start, 2),
            )
        output = Path(inputs.get("output_path") or (workspace / "output.mp4"))
        output.parent.mkdir(parents=True, exist_ok=True)
        if not (workspace / "node_modules").is_dir():
            from runner import progress

            progress.note(f"npm install motion-canvas workspace {workspace.name}", kind="render")
            npm = subprocess.run(
                ["npm", "ci"],
                cwd=workspace,
                capture_output=True,
                text=True,
                shell=(__import__("os").name == "nt"),
            )
            if npm.returncode != 0:
                subprocess.run(
                    ["npm", "install"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    shell=(__import__("os").name == "nt"),
                )
        env = os.environ.copy()
        chrome = _chrome_path()
        if chrome:
            env["CHROME_PATH"] = chrome
        from runner.ffmpeg_bin import find_ffmpeg, prepend_to_path

        prepend_to_path()
        ff = find_ffmpeg()
        if ff:
            env["FFMPEG"] = ff
            env["PATH"] = str(Path(ff).parent) + os.pathsep + env.get("PATH", "")
        from runner import progress

        progress.note(f"motion-canvas render {output.name} {inputs.get('duration_seconds') or 5}s", kind="render")
        proc = subprocess.run(
            ["node", str(workspace / "render.mjs"), "src/project.ts", str(output), str(int(float(inputs.get("duration_seconds") or 5)))],
            cwd=workspace,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            shell=(__import__("os").name == "nt"),
        )
        if proc.returncode != 0 or not output.is_file():
            return ToolResult(
                success=False,
                error=(proc.stderr or proc.stdout or "motion canvas render failed")[-2000:],
                data={"workspace": str(workspace)},
                duration_seconds=round(time.time() - start, 2),
            )
        if _mp4_is_blank(output):
            return ToolResult(
                success=False,
                error="motion canvas render produced a blank clip (capture never drew the scene)",
                data={"workspace": str(workspace), "output": str(output)},
                duration_seconds=round(time.time() - start, 2),
            )
        return ToolResult(
            success=True,
            data={"output": str(output), "workspace": str(workspace), "waitUntil": True},
            artifacts=[str(output)],
            duration_seconds=round(time.time() - start, 2),
        )

    def _materialize_workspace(self, workspace: Path) -> None:
        for rel in (
            "package.json",
            "vite.config.ts",
            "render.mjs",
            "preview.html",
            "preview.ts",
            "src/project.ts",
        ):
            src = ADAPTER / rel
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy(src, dest)
        nm = ADAPTER / "node_modules"
        if nm.is_dir() and not (workspace / "node_modules").exists():
            try:
                (workspace / "node_modules").symlink_to(nm, target_is_directory=True)
            except OSError:
                pass
