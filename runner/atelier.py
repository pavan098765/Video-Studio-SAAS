"""Atelier authoring: scaffold + Gemini TSX/HTML + render. Never silent Explainer."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from runner import tools_exec
from runner.artifacts import project_dir
from runner.config import repo_root
from runner.ffmpeg_bin import find_ffmpeg, find_ffprobe, prepend_to_path
from runner.llm_gemini import LLMError, StageModel
from runner.mode import platform_size


def _scaffold_mod():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "scaffold_atelier_project.py"
    spec = importlib.util.spec_from_file_location("scaffold_atelier_project", path)
    if spec is None or spec.loader is None:
        raise AtelierError("scaffold_atelier_project.py missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

STOCK_IMPORT_RE = re.compile(
    r"(?:from|import)\s+['\"](?:\.\./)+src/(?:components|Explainer|CinematicRenderer|TalkingHead)|"
    r"remotion-composer/src/(?:components|Explainer)",
    re.I,
)
SCAFFOLD_MARKERS = (
    "TODO: hand-stitch",
    'background: "#000"',
    "background: '#000'",
)
BIND_MARK = "// SaaS runner aliases — Root.tsx imports Scene, SceneProps, calculateMetadata."
_EXPORT_NAME = re.compile(r"export\s+(?:const|function|class)\s+([A-Z][A-Za-z0-9]*)\b")
_EXPORT_FC = re.compile(r"export\s+const\s+([A-Z][A-Za-z0-9]*)\s*:\s*React\.FC")
_EXPORT_PROPS = re.compile(r"export\s+(?:interface|type)\s+([A-Z][A-Za-z0-9]*Props)\b")
_EXPORT_META = re.compile(r"export\s+(?:const|function)\s+calculateMetadata\b")
_PREFERRED_COMPONENTS = ("Scene", "Composition", "Main", "App")
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}


class AtelierError(RuntimeError):
    pass


def slug_for(job_id: str, topic: str) -> str:
    """One Remotion project per job (OpenMontage `projects/<name>/`), not per topic."""
    topic_part = re.sub(r"[^a-z0-9]+", "-", (topic or "studio").lower()).strip("-")[:36]
    compact = re.sub(r"[^a-z0-9]+", "", (job_id or "job").lower()) or "job"
    return f"{topic_part}-{compact[-16:]}"


def project_path(slug: str) -> Path:
    return repo_root() / "projects" / slug


def is_scaffold(tsx: str) -> bool:
    blob = tsx or ""
    return any(m in blob for m in SCAFFOLD_MARKERS) and "useCurrentFrame" not in blob


def validate_tsx(tsx: str, scene_plan: dict[str, Any]) -> None:
    if STOCK_IMPORT_RE.search(tsx or ""):
        raise AtelierError("atelier TSX imports stock remotion-composer/src components")
    if is_scaffold(tsx) or len(tsx or "") < 900:
        raise AtelierError("atelier Composition.tsx is still the scaffold placeholder")
    if "from \"remotion\"" not in tsx and "from 'remotion'" not in tsx:
        raise AtelierError("atelier Composition.tsx must use Remotion APIs")
    scenes = scene_plan.get("scenes") or []
    if scenes and "Sequence" not in tsx and "interpolate" not in tsx and "spring(" not in tsx:
        raise AtelierError("atelier TSX has no motion (Sequence/interpolate/spring)")
    if not re.search(r"export\s+(?:const|function|class)\s+Scene\b", tsx or ""):
        raise AtelierError("atelier Composition.tsx must export Scene for Root.tsx")
    if not re.search(r"export\s+(?:const|function)\s+calculateMetadata\b", tsx or ""):
        raise AtelierError("atelier Composition.tsx must export calculateMetadata")


def duration_frames(
    *,
    scene_plan: dict[str, Any] | None = None,
    props: dict[str, Any] | None = None,
    fallback_seconds: float = 8.0,
) -> int:
    end = 0.0
    for scene in (scene_plan or {}).get("scenes") or []:
        try:
            end = max(end, float(scene.get("end_seconds") or 0))
        except (TypeError, ValueError):
            continue
    if end <= 0 and props:
        try:
            end = float(props.get("durationInSeconds") or 0)
        except (TypeError, ValueError):
            end = 0.0
    if end <= 0:
        end = fallback_seconds
    return max(30, int(round(end * 30)))


def detect_scene_export(tsx: str) -> tuple[str, str | None]:
    names = _EXPORT_NAME.findall(tsx or "")
    component = next((pref for pref in _PREFERRED_COMPONENTS if pref in names), None)
    if component is None:
        fc = _EXPORT_FC.findall(tsx or "")
        component = fc[-1] if fc else None
    if not component:
        raise AtelierError("atelier Composition.tsx has no exported scene component")
    prop_names = _EXPORT_PROPS.findall(tsx or "")
    guessed = f"{component}Props"
    if guessed in prop_names:
        return component, guessed
    for pref in ("SceneProps", "CompositionProps", "Props"):
        if pref in prop_names:
            return component, pref
    return component, (prop_names[0] if prop_names else None)


def bind_atelier_exports(tsx: str, *, duration_frames: int) -> str:
    blob = (tsx or "").replace("\r\n", "\n")
    if BIND_MARK in blob:
        blob = blob.split(BIND_MARK)[0].rstrip() + "\n"
    component, props_name = detect_scene_export(blob)
    names = set(_EXPORT_NAME.findall(blob))
    prop_names = set(_EXPORT_PROPS.findall(blob))
    extras: list[str] = []
    if "Scene" not in names:
        extras.append(f"export const Scene = {component};\n")
    if "SceneProps" not in prop_names:
        if props_name and props_name != "SceneProps":
            extras.append(f"export type SceneProps = {props_name};\n")
        else:
            extras.append("export type SceneProps = Record<string, unknown>;\n")
    if not _EXPORT_META.search(blob):
        extras.append(
            "export const calculateMetadata = async () => ({\n"
            f"  durationInFrames: {int(duration_frames)},\n"
            "  fps: 30,\n"
            "  width: 1920,\n"
            "  height: 1080,\n"
            "});\n"
        )
    if extras:
        blob = blob.rstrip() + "\n\n" + BIND_MARK + "\n" + "".join(extras)
    return harden_offthread_video(blob)


def harden_offthread_video(tsx: str) -> str:
    """Keep OffthreadVideo from crashing the Remotion compositor on short/gappy inserts."""
    blob = tsx or ""
    if "<OffthreadVideo" not in blob:
        return blob
    extras: list[str] = []
    if "acceptableTimeShiftInSeconds" not in blob:
        extras.append("acceptableTimeShiftInSeconds={1}")
    if not re.search(r"\btoneMapped\b", blob):
        extras.append("toneMapped={false}")
    if not extras:
        return blob
    return blob.replace("<OffthreadVideo", "<OffthreadVideo " + " ".join(extras))


def atelier_root_tsx(composition_id: str, duration_frames: int) -> str:
    cid = composition_id or "StudioPiece"
    frames = max(30, int(duration_frames))
    return (
        'import "../../src/index.css";\n'
        'import { Composition } from "remotion";\n'
        'import { Scene, calculateMetadata, SceneProps } from "./Composition";\n'
        "\n"
        "export const Root: React.FC = () => (\n"
        "  <Composition\n"
        f'    id="{cid}"\n'
        "    component={Scene}\n"
        f"    durationInFrames={{{frames}}}\n"
        "    fps={30}\n"
        "    width={1920}\n"
        "    height={1080}\n"
        "    defaultProps={{} as SceneProps}\n"
        "    calculateMetadata={calculateMetadata}\n"
        "  />\n"
        ");\n"
    )


def _composition_id_from_root(proj: Path, fallback: str) -> str:
    root = proj / "Root.tsx"
    if not root.is_file():
        return fallback
    match = re.search(r'id="([^"]+)"', root.read_text(encoding="utf-8"))
    return match.group(1) if match else fallback


def _commit_scene_tsx(slug: str, tsx: str) -> str:
    proj = project_path(slug)
    plan: dict[str, Any] = {}
    props: dict[str, Any] = {}
    plan_path = proj / "artifacts" / "scene_plan.json"
    props_path = proj / "artifacts" / "props.json"
    if plan_path.is_file():
        try:
            loaded = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            plan = loaded
    if props_path.is_file():
        try:
            loaded = json.loads(props_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            props = loaded
    frames = duration_frames(scene_plan=plan, props=props)
    cid = _composition_id_from_root(proj, fallback=_scaffold_mod().to_camel(slug)[:48] or "StudioPiece")
    bound = bind_atelier_exports(tsx, duration_frames=frames)
    validate_tsx(bound, plan)
    (proj / "Composition.tsx").write_text(bound, encoding="utf-8")
    (proj / "Root.tsx").write_text(atelier_root_tsx(cid, frames), encoding="utf-8")
    return bound


def _probe_duration(path: Path) -> float:
    probe = find_ffprobe()
    if not probe or not path.is_file():
        return 0.0
    proc = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float((proc.stdout or "").strip() or 0)
    except ValueError:
        return 0.0


def stage_insert_video(src: Path, dest: Path, *, min_seconds: float) -> None:
    """CFR re-encode (no B-frames) and loop-pad so OffthreadVideo can always extract a frame."""
    prepend_to_path()
    ffmpeg = find_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not ffmpeg:
        shutil.copy2(src, dest)
        return
    duration = _probe_duration(src)
    target = max(float(min_seconds or 0), duration, 1.0)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if duration > 0 and duration + 0.08 < target:
        cmd += ["-stream_loop", "-1"]
    cmd += [
        "-i",
        str(src),
        "-t",
        f"{target:.3f}",
        "-an",
        "-vf",
        "fps=30,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-bf",
        "0",
        "-g",
        "30",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 32:
        shutil.copy2(src, dest)


def _copy_public_media(proj: Path, asset_manifest: dict[str, Any], *, min_seconds: float = 8.0) -> None:
    public = proj / "public"
    public.mkdir(parents=True, exist_ok=True)
    for row in asset_manifest.get("assets") or []:
        src = Path(str(row.get("path") or ""))
        if not src.is_file():
            continue
        dest = public / src.name
        if src.suffix.lower() in _VIDEO_SUFFIXES:
            stage_insert_video(src, dest, min_seconds=min_seconds)
        else:
            shutil.copy2(src, dest)


def write_files(
    *,
    slug: str,
    composition_id: str,
    composition_tsx: str,
    art_direction_md: str,
    props: dict[str, Any],
    scene_plan: dict[str, Any],
    asset_manifest: dict[str, Any],
    root_tsx: str | None = None,
) -> Path:
    _ = root_tsx  # Root.tsx is runner-owned; LLM root_tsx is ignored.
    mod = _scaffold_mod()
    root = repo_root()
    proj = mod.scaffold(slug, composition_id, root)
    frames = duration_frames(scene_plan=scene_plan, props=props)
    bound = bind_atelier_exports(composition_tsx, duration_frames=frames)
    (proj / "Composition.tsx").write_text(bound, encoding="utf-8")
    validate_tsx(bound, scene_plan)
    (proj / "Root.tsx").write_text(atelier_root_tsx(composition_id, frames), encoding="utf-8")
    (proj / "art-direction.md").write_text(art_direction_md or "Art direction for this piece.", encoding="utf-8")
    props_path = proj / "artifacts" / "props.json"
    props_path.parent.mkdir(parents=True, exist_ok=True)
    props_path.write_text(json.dumps(props, indent=2), encoding="utf-8")
    (proj / "artifacts" / "scene_plan.json").write_text(json.dumps(scene_plan, indent=2), encoding="utf-8")
    _copy_public_media(proj, asset_manifest, min_seconds=frames / 30)
    return proj


def typecheck(proj: Path) -> None:
    from runner import progress

    progress.note(f"typecheck {proj.name}", kind="atelier")
    npx = shutil.which("npx") or "npx"
    composer = repo_root() / "remotion-composer"
    proc = subprocess.run(
        [npx, "tsc", "--noEmit", "--pretty", "false", str(proj / "Composition.tsx"), str(proj / "Root.tsx")],
        cwd=str(composer),
        capture_output=True,
        text=True,
        timeout=120,
        shell=(__import__("os").name == "nt"),
    )
    # Isolated file tsc often fails without project refs; Remotion bundle is the real gate.
    if proc.returncode != 0 and "Cannot find module 'remotion'" in (proc.stderr or proc.stdout or ""):
        return
    if proc.returncode != 0 and "error TS" in ((proc.stderr or "") + (proc.stdout or "")):
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-1500:]
        raise AtelierError(f"atelier typecheck failed: {tail}")


def snapshots_ok(slug: str, composition_id: str) -> bool:
    script = repo_root() / "scripts" / "atelier_snapshots.py"
    if not script.is_file():
        return True
    from runner import progress

    progress.note(
        f"snapshots {slug} composition={composition_id} — Remotion stills; first one includes webpack and can take several minutes",
        kind="atelier",
    )
    proc = subprocess.run(
        ["python", str(script), slug, "--composition-id", composition_id],
        cwd=str(repo_root()),
        timeout=1800,
    )
    snap_dir = repo_root() / "projects" / slug / "snapshots"
    if proc.returncode != 0:
        return False
    blacks = 0
    total = 0
    try:
        from PIL import Image
    except ImportError:
        return proc.returncode == 0
    for png in snap_dir.glob("*.png"):
        total += 1
        try:
            im = Image.open(png).convert("L").resize((32, 18))
            mean = sum(im.getdata()) / max(1, len(list(im.getdata())))
            if mean < 6:
                blacks += 1
        except Exception:
            continue
    if total and blacks == total:
        return False
    return True


def render(
    *,
    slug: str,
    composition_id: str,
    dest: Path,
    duration_seconds: int,
    width: int,
    height: int,
    playbook: str | None,
) -> Path:
    proj = project_path(slug)
    entry = proj / "index.tsx"
    props_path = proj / "artifacts" / "props.json"
    art = (proj / "art-direction.md").read_text(encoding="utf-8") if (proj / "art-direction.md").is_file() else "atelier"
    edit = {
        "version": "1.0",
        "cuts": [{"id": "root", "source": "atelier", "in_seconds": 0, "out_seconds": duration_seconds}],
        "render_runtime": "remotion",
        "renderer_family": "explainer-data",
        "composition_mode": "atelier",
        "bespoke": {
            "entry": str(entry),
            "composition_id": composition_id,
            "art_direction": art[:500],
            "props_path": str(props_path),
            "public_dir": str(proj / "public"),
        },
        "metadata": {"playbook": playbook or "clean-professional"},
    }
    from runner import progress

    progress.note(f"remotion render {slug} -> {dest.name} ({duration_seconds}s {width}x{height})", kind="atelier")
    result = tools_exec.execute(
        "video_compose",
        {
            "operation": "render",
            "edit_decisions": edit,
            "output_path": str(dest),
            "width": width,
            "height": height,
            "duration_frames": max(30, int(duration_seconds * 30)),
        },
    )
    if not result.success or not dest.is_file():
        raise AtelierError(result.error or "atelier render failed")
    checks = ((result.data or {}).get("final_review") or {}).get("checks") or {}
    atelier = checks.get("atelier") or {}
    if atelier.get("stock_reuse_detected"):
        raise AtelierError("atelier stock_reuse_detected")
    return dest


def author_with_gemini(
    model: StageModel,
    *,
    skill_text: str,
    user: str,
    retries: int,
) -> dict[str, Any]:
    """Canned/fixture packaging only. Live jobs author TSX via write_file tools."""
    last = "atelier_author produced no JSON"
    from runner import progress

    for attempt in range(max(1, retries)):
        progress.note(f"author TSX attempt {attempt + 1}/{max(1, retries)} (Gemini, can take several minutes)", kind="atelier")
        turn = model.run_stage(
            system=(
                "You author a Remotion atelier composition. Return ONE JSON object with keys "
                "composition_tsx, art_direction_md, props. composition_tsx is the full Composition.tsx. "
                "Do not import remotion-composer/src/components or Explainer. Use Remotion APIs, karaoke "
                "from props.captions, charts from props.chartData, and OffthreadVideo/staticFile for inserts. "
                "Overwrite the black scaffold. No markdown fences."
            )
            + "\n\n"
            + skill_text,
            user=user,
            tools=[],
            retry=attempt,
            max_rounds=4,
        )
        artifact = turn.artifact if turn.artifact else None
        if artifact is None and turn.text:
            blob = turn.text.strip()
            if blob.startswith("```"):
                blob = blob.strip("`")
                if blob.startswith("json"):
                    blob = blob[4:].strip()
            try:
                artifact = json.loads(blob)
            except json.JSONDecodeError:
                artifact = None
        if isinstance(artifact, dict) and artifact.get("composition_tsx"):
            return artifact
        last = turn.text or last
    raise LLMError(last)


def is_kinetic_stub(html: str) -> bool:
    blob = html or ""
    return ".kline" in blob or ("data-composition-id" not in blob)


def author_kinetic_html(
    model: StageModel,
    *,
    topic: str,
    script: dict[str, Any] | None,
    duration: float,
    retries: int,
    skill_text: str = "",
) -> str:
    user = json.dumps(
        {
            "topic": topic,
            "duration": duration,
            "sections": [(s.get("id"), (s.get("text") or "")[:200]) for s in (script or {}).get("sections") or []],
            "rules": "Return JSON {composition_html, art_direction_md}. HTML must include data-composition-id=\"root\" and GSAP. No class kline title stacks.",
        },
        default=str,
    )
    last = "kinetic html missing"
    for attempt in range(max(1, retries)):
        turn = model.run_stage(
            system=(
                "Author a HyperFrames kinetic HTML composition. Return ONE JSON object with "
                "composition_html and art_direction_md. Root div: data-composition-id=\"root\". "
                "Do not use class kline or three stacked h1 titles."
            )
            + ("\n\n" + skill_text if skill_text else ""),
            user=user,
            tools=[],
            retry=attempt,
            max_rounds=3,
        )
        artifact = turn.artifact if isinstance(turn.artifact, dict) else None
        html = str((artifact or {}).get("composition_html") or "")
        if html and not is_kinetic_stub(html) and 'data-composition-id="root"' in html:
            return html
        last = turn.text or last
    raise AtelierError(last or "kinetic HTML authoring failed")


def _parse_author_artifact(turn: Any) -> dict[str, Any] | None:
    artifact = turn.artifact if getattr(turn, "artifact", None) else None
    if artifact is None and getattr(turn, "text", None):
        blob = str(turn.text).strip()
        if blob.startswith("```"):
            blob = blob.strip("`")
            if blob.startswith("json"):
                blob = blob[4:].strip()
        try:
            artifact = json.loads(blob)
        except json.JSONDecodeError:
            artifact = None
    return artifact if isinstance(artifact, dict) else None


def patch_atelier_scene(
    model: StageModel,
    *,
    slug: str,
    scene_id: str,
    skill_text: str,
    retries: int,
    regen_prompt: str = "",
    rewrite_hint: str = "",
    images: list[Path] | None = None,
) -> str:
    tsx_path = project_path(slug) / "Composition.tsx"
    if not tsx_path.is_file():
        raise AtelierError(f"atelier Composition.tsx missing for {slug}")
    existing = tsx_path.read_text(encoding="utf-8")
    user = json.dumps(
        {
            "scene_id": scene_id,
            "regen_prompt": regen_prompt,
            "rewrite_hint": rewrite_hint,
            "existing_tsx": existing,
            "stills": [str(p.name) for p in (images or []) if p.is_file()],
            "rules": "Return JSON composition_tsx. Keep other scenes. Rewrite only the named scene Sequence.",
        },
        default=str,
    )
    stills = [p for p in (images or []) if p.is_file()]
    if stills and hasattr(model, "generate_vision"):
        last = "atelier vision patch produced no JSON"
        for attempt in range(max(1, retries)):
            turn = model.generate_vision(  # type: ignore[attr-defined]
                system=(
                    "You author a Remotion atelier composition. Return ONE JSON object with keys "
                    "composition_tsx, art_direction_md, props. Rewrite only the named scene. "
                    "Do not import remotion-composer/src/components or Explainer."
                    + "\n\n"
                    + skill_text
                ),
                user=user if attempt == 0 else f"{user}\nPrevious attempt failed: {last}",
                images=stills,
            )
            authored = _parse_author_artifact(turn)
            if authored and authored.get("composition_tsx"):
                tsx = str(authored.get("composition_tsx") or "")
                return _commit_scene_tsx(slug, tsx)
            last = getattr(turn, "text", None) or last
        raise AtelierError(last)
    authored = author_with_gemini(model, skill_text=skill_text, user=user, retries=retries)
    tsx = str(authored.get("composition_tsx") or "")
    if not tsx:
        raise AtelierError("atelier scene patch produced empty TSX")
    return _commit_scene_tsx(slug, tsx)


def patch_kinetic_html(
    model: StageModel,
    *,
    workspace: Path,
    skill_text: str,
    retries: int,
    regen_prompt: str = "",
    rewrite_hint: str = "",
    images: list[Path] | None = None,
) -> str:
    html_path = Path(workspace) / "index.html"
    if not html_path.is_file():
        raise AtelierError(f"kinetic HTML missing at {html_path}")
    existing = html_path.read_text(encoding="utf-8")
    user = json.dumps(
        {
            "regen_prompt": regen_prompt,
            "rewrite_hint": rewrite_hint,
            "existing_html": existing,
            "stills": [str(p.name) for p in (images or []) if p.is_file()],
            "rules": (
                "Return JSON {composition_html, art_direction_md}. Keep data-composition-id=\"root\". "
                "Fix T-pose, broken IK, empty frames, or title-stack kline layouts."
            ),
        },
        default=str,
    )
    system = (
        "Author a HyperFrames kinetic HTML composition. Return ONE JSON object with "
        "composition_html and art_direction_md. Root div: data-composition-id=\"root\"."
        + ("\n\n" + skill_text if skill_text else "")
    )
    stills = [p for p in (images or []) if p.is_file()]
    last = "kinetic html patch missing"
    for attempt in range(max(1, retries)):
        if stills and hasattr(model, "generate_vision"):
            turn = model.generate_vision(  # type: ignore[attr-defined]
                system=system,
                user=user if attempt == 0 else f"{user}\nPrevious attempt failed: {last}",
                images=stills,
            )
        else:
            turn = model.run_stage(
                system=system,
                user=user if attempt == 0 else f"{user}\nPrevious attempt failed: {last}",
                tools=[],
                retry=attempt,
                max_rounds=3,
            )
        authored = _parse_author_artifact(turn)
        html = str((authored or {}).get("composition_html") or "")
        if html and not is_kinetic_stub(html) and 'data-composition-id="root"' in html:
            html_path.write_text(html, encoding="utf-8")
            return html
        last = getattr(turn, "text", None) or last
    raise AtelierError(last or "kinetic HTML patch failed")


def rerender_kinetic(
    *,
    model: StageModel,
    workspace: Path,
    dest: Path,
    skill_text: str,
    retries: int,
    prefs: dict[str, Any],
    regen_prompt: str = "",
    rewrite_hint: str = "",
    images: list[Path] | None = None,
    canned: bool = False,
) -> Path:
    patch_kinetic_html(
        model,
        workspace=workspace,
        skill_text=skill_text,
        retries=retries,
        regen_prompt=regen_prompt,
        rewrite_hint=rewrite_hint,
        images=images,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    width, height = platform_size((prefs or {}).get("platform_profile"))
    result = tools_exec.execute(
        "hyperframes_compose",
        {
            "operation": "render_existing",
            "workspace_path": str(workspace),
            "output_path": str(dest),
            "skip_contrast": True,
            "strict_check": False,
            "quality": "draft",
        },
    )
    src = Path(result.artifacts[0]) if result and result.success and result.artifacts else dest
    if not result.success or not src.is_file():
        raise AtelierError(result.error or "hyperframes patch render failed")
    if src != dest:
        shutil.copy2(src, dest)
    from runner import compose

    compose.ensure_audio(dest, None, max(1, int((prefs or {}).get("duration_seconds") or 8)), canned=canned, width=width, height=height)
    return dest


def kinetic_html(*, topic: str, script: dict[str, Any] | None, duration: float) -> str:
    lines = []
    for section in (script or {}).get("sections") or []:
        lines.append(str(section.get("text") or "")[:90])
    if not lines:
        lines = [topic]
    items = "\n".join(f'    <h1 class="kline">{_esc(line)}</h1>' for line in lines[:8])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <style>
    body {{ margin: 0; background: #0b1020; color: #f8fafc; font-family: Georgia, serif; }}
    [data-composition-id="root"] {{ position: relative; width: 1920px; height: 1080px; overflow: hidden; }}
    .kline {{ position: absolute; left: 160px; font-size: 72px; opacity: 0; letter-spacing: -0.03em; }}
  </style>
</head>
<body>
  <div data-composition-id="root" data-start="0" data-duration="{duration:.3f}" data-width="1920" data-height="1080">
{items}
  </div>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
    gsap.utils.toArray('.kline').forEach((node, i) => {{
      node.style.top = (180 + i * 90) + 'px';
      tl.fromTo(node, {{ y: 40, opacity: 0 }}, {{ y: 0, opacity: 1, duration: 0.55, ease: 'power3.out' }}, i * 0.35);
    }});
    window.__timelines['root'] = tl;
  </script>
</body>
</html>
"""


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def run_atelier(
    *,
    model: StageModel,
    work_dir: Path,
    job_id: str,
    topic: str,
    pipeline: str,
    duration: int,
    prefs: dict[str, Any],
    scene_plan: dict[str, Any],
    script: dict[str, Any] | None,
    asset_manifest: dict[str, Any],
    research: dict[str, Any] | None,
    skill_text: str,
    retries: int,
    playbook: str | None,
    rewrite_hint: str = "",
) -> dict[str, Any]:
    slug = slug_for(job_id, topic)
    composition_id = _scaffold_mod().to_camel(slug)[:48] or "StudioPiece"
    from runner import progress

    progress.note(f"atelier author {slug} composition={composition_id} duration={duration}s", kind="atelier")
    captions = []
    from runner.edl import captions_from_timestamps, load_word_timestamps

    captions = captions_from_timestamps(load_word_timestamps(asset_manifest))
    user = json.dumps(
        {
            "topic": topic,
            "pipeline": pipeline,
            "duration_seconds": duration,
            "scene_plan": scene_plan,
            "script": script,
            "research_data_points": (research or {}).get("data_points"),
            "assets": [
                {"id": a.get("id"), "type": a.get("type"), "path": Path(str(a.get("path") or "")).name, "scene_id": a.get("scene_id")}
                for a in (asset_manifest.get("assets") or [])
            ],
            "captions": captions[:80],
            "composition_id": composition_id,
            "rules": "No stock Explainer imports. Karaoke from props.captions. Charts from researched numbers.",
            "rewrite_hint": rewrite_hint,
        },
        default=str,
    )
    authored = author_with_gemini(model, skill_text=skill_text, user=user, retries=retries)
    props = authored.get("props") if isinstance(authored.get("props"), dict) else {}
    props.setdefault("captions", captions)
    props.setdefault("fps", 30)
    props.setdefault("durationInSeconds", duration)
    props.setdefault("scene_plan", scene_plan)
    proj = write_files(
        slug=slug,
        composition_id=composition_id,
        composition_tsx=str(authored.get("composition_tsx") or ""),
        art_direction_md=str(authored.get("art_direction_md") or ""),
        props=props,
        scene_plan=scene_plan,
        asset_manifest=asset_manifest,
        root_tsx=authored.get("root_tsx") if isinstance(authored.get("root_tsx"), str) else None,
    )
    try:
        typecheck(proj)
    except AtelierError:
        if retries <= 1:
            raise
        authored = author_with_gemini(model, skill_text=skill_text, user=user + "\nFix TypeScript errors.", retries=1)
        proj = write_files(
            slug=slug,
            composition_id=composition_id,
            composition_tsx=str(authored.get("composition_tsx") or ""),
            art_direction_md=str(authored.get("art_direction_md") or ""),
            props=authored.get("props") if isinstance(authored.get("props"), dict) else props,
            scene_plan=scene_plan,
            asset_manifest=asset_manifest,
        )
        typecheck(proj)
    width, height = platform_size((prefs or {}).get("platform_profile"))
    dest = project_dir(work_dir) / "renders" / "atelier_master.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    render(
        slug=slug,
        composition_id=composition_id,
        dest=dest,
        duration_seconds=duration,
        width=width,
        height=height,
        playbook=playbook,
    )
    if not snapshots_ok(slug, composition_id):
        raise AtelierError("atelier snapshots are black/empty — refusing scaffold render")
    snap_dir = repo_root() / "projects" / slug / "snapshots"
    stills = sorted(p for p in snap_dir.glob("*.png") if p.is_file())[:8]
    if stills:
        from runner import picture_patch

        def _patch(qa: dict[str, Any], images: list[Path]) -> Path:
            patch_atelier_scene(
                model,
                slug=slug,
                scene_id=str(((scene_plan.get("scenes") or [{}])[0] or {}).get("id") or "sc1"),
                skill_text=skill_text,
                retries=max(1, retries),
                rewrite_hint=str(qa.get("rewrite_hint") or "Vision loop: match stills to scene_plan, no black frames, no Explainer salvage"),
                images=images or stills,
            )
            render(
                slug=slug,
                composition_id=composition_id,
                dest=dest,
                duration_seconds=duration,
                width=width,
                height=height,
                playbook=playbook,
            )
            return dest

        def _review(_path: Path) -> dict[str, Any]:
            ok = snapshots_ok(slug, composition_id)
            return {
                "qa_status": "pass" if ok else "fail",
                "stills": [str(p) for p in stills],
                "rewrite_hint": "" if ok else "Snapshots still empty or off-brief",
            }

        picture_patch.run_rounds(
            initial_path=dest,
            initial_qa={"qa_status": "fail", "stills": [str(p) for p in stills], "rewrite_hint": "Review atelier stills before lock"},
            patch=_patch,
            review=_review,
            canned=False,
        )
    return {
        "slug": slug,
        "composition_id": composition_id,
        "project": str(proj),
        "master": str(dest),
        "entry": str(proj / "index.tsx"),
    }


def rerender_scene(
    *,
    model: StageModel,
    work_dir: Path,
    slug: str,
    composition_id: str,
    scene_id: str,
    scenes: list[dict[str, Any]],
    scenes_dir: Path,
    duration: int,
    prefs: dict[str, Any],
    skill_text: str,
    retries: int,
    playbook: str | None,
    regen_prompt: str = "",
    rewrite_hint: str = "",
    images: list[Path] | None = None,
) -> Path:
    """Patch one scene Sequence, re-render the master, ffmpeg-split that scene only."""
    from runner import compose

    patch_atelier_scene(
        model,
        slug=slug,
        scene_id=scene_id,
        skill_text=skill_text,
        retries=retries,
        regen_prompt=regen_prompt,
        rewrite_hint=rewrite_hint,
        images=images,
    )
    dest = project_dir(work_dir) / "renders" / "atelier_master.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    width, height = platform_size((prefs or {}).get("platform_profile"))
    render(
        slug=slug,
        composition_id=composition_id,
        dest=dest,
        duration_seconds=duration,
        width=width,
        height=height,
        playbook=playbook,
    )
    target = [s for s in scenes if str(s.get("id")) == str(scene_id)]
    if not target:
        raise AtelierError(f"scene {scene_id} missing from scene_plan")
    files = compose.split_master(dest, target, scenes_dir)
    if not files:
        raise AtelierError(f"split_master produced no file for {scene_id}")
    return files[0]
