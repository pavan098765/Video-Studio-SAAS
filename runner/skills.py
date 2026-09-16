"""Load stage directors and picture runtime packs. Whole files only — never [truncated]."""

from __future__ import annotations

from pathlib import Path

from runner.config import repo_root

# Kept for callers that still import the name. Loaders no longer slice to this cap.
TOKEN_CHAR_CAP = 1_000_000

PICTURE_LAYER3 = {
    "remotion",
    "remotion-best-practices",
    "remotion-to-hyperframes",
    "hyperframes",
    "hyperframes-core",
    "hyperframes-creative",
    "hyperframes-animation",
    "hyperframes-cli",
    "hyperframes-media",
    "hyperframes-registry",
    "gsap-core",
    "gsap-utils",
    "gsap-timeline",
    "gsap-scrolltrigger",
    "gsap-react",
    "gsap-plugins",
    "gsap-performance",
    "gsap-frameworks",
    "svg-character-animation",
    "character-rigging",
    "character-animation-qa",
    "motion-graphics",
    "canvas-procedural-animation",
}

REMOTION_PACK = (
    "skills/meta/bespoke-composition.md",
    "skills/core/remotion.md",
    ".agents/skills/remotion/SKILL.md",
    ".agents/skills/remotion-best-practices/SKILL.md",
    ".agents/skills/remotion-best-practices/rules/tailwind.md",
    ".agents/skills/remotion-best-practices/rules/animations.md",
)

HF_PACK = (
    "skills/core/hyperframes.md",
    ".agents/skills/hyperframes/SKILL.md",
    ".agents/skills/hyperframes-core/SKILL.md",
    ".agents/skills/hyperframes-animation/SKILL.md",
    ".agents/skills/hyperframes-cli/SKILL.md",
    ".agents/skills/hyperframes-media/SKILL.md",
    ".agents/skills/hyperframes-registry/SKILL.md",
    ".agents/skills/hyperframes-creative/SKILL.md",
    ".agents/skills/svg-character-animation/SKILL.md",
    ".agents/skills/character-rigging/SKILL.md",
    ".agents/skills/character-animation-qa/SKILL.md",
    ".agents/skills/gsap-core/SKILL.md",
)

HF_OMIT_ORDER = ("hyperframes-creative", "gsap-core")
PACK_BUDGET = {"remotion": 55_000, "hyperframes": 90_000}


def _read_full(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _skill_slug(name: str) -> str:
    slug = name.replace("\\", "/").rstrip("/").split("/")[-1]
    if slug.endswith(".md"):
        slug = slug[:-3]
    if slug == "SKILL":
        parts = name.replace("\\", "/").rstrip("/").split("/")
        slug = parts[-2] if len(parts) >= 2 else slug
    return slug


def is_picture_layer3(name: str) -> bool:
    slug = _skill_slug(name)
    if slug in PICTURE_LAYER3:
        return True
    return slug.startswith("hyperframes") or slug.startswith("gsap-") or slug.startswith("remotion")


def playbook_path(name: str | None) -> Path:
    slug = (name or "clean-professional").strip() or "clean-professional"
    if slug.endswith(".md") or slug.endswith(".yaml"):
        slug = slug.rsplit(".", 1)[0]
    yaml_path = repo_root() / "styles" / f"{slug}.yaml"
    if yaml_path.is_file():
        return yaml_path
    return repo_root() / "styles" / f"{slug}.md"


def load_playbook(name: str | None, *, limit: int = 4000) -> str:
    del limit
    slug = (name or "clean-professional").strip() or "clean-professional"
    if slug.endswith(".md") or slug.endswith(".yaml"):
        slug = slug.rsplit(".", 1)[0]
    try:
        from styles.playbook_loader import load_playbook as load_yaml

        data = load_yaml(slug)
        import json

        text = json.dumps(data, indent=2, default=str)
        return f"\n\n# Playbook YAML ({slug})\n{text}"
    except Exception:
        path = playbook_path(slug)
        if not path.is_file():
            path = playbook_path("clean-professional")
        text = _read_full(path)
        return f"\n\n# Playbook ({path.stem})\n{text}" if text else ""


def load_complete(paths: list[str] | tuple[str, ...]) -> str:
    chunks: list[str] = []
    root = repo_root()
    for rel in paths:
        path = Path(rel)
        if not path.is_absolute():
            path = root / rel
        text = _read_full(path)
        if not text:
            continue
        chunks.append(f"\n\n# {rel}\n{text}")
    return "".join(chunks)


def load_layer3(skill_names: list[str], *, budget: int | None = None, exclude_picture: bool = False) -> str:
    """Include whole Layer 3 files up to budget. Omit extras by name (Cursor reads on demand)."""
    cap = 32_000 if budget is None else budget
    names = [n for n in skill_names if not (exclude_picture and is_picture_layer3(n))]
    chunks: list[str] = []
    omitted: list[str] = []
    used = 0
    root = repo_root()
    for name in names:
        candidates = [
            root / ".agents" / "skills" / name / "SKILL.md",
            root / ".claude" / "skills" / name / "SKILL.md",
            root / "skills" / f"{name}.md",
        ]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            continue
        text = _read_full(path)
        if not text:
            continue
        chunk = f"\n\n# Layer 3 skill: {name}\n{text}"
        if cap > 0 and used + len(chunk) > cap:
            omitted.append(name)
            continue
        chunks.append(chunk)
        used += len(chunk)
    if omitted:
        chunks.append(
            "\n\n# Layer 3 omitted (API prompt budget): "
            + ", ".join(omitted)
            + ". Call the declared tools; do not invent providers.\n"
        )
    return "".join(chunks)


def load_meta(rel: str, *, limit: int | None = None) -> str:
    del limit
    path = repo_root() / "skills" / rel
    if not path.suffix:
        path = path.with_suffix(".md")
    text = _read_full(path)
    return f"\n\n# {rel}\n{text}" if text else ""


def load_picture_pack(kind: str) -> str:
    if kind in {"hyperframes", "character", "kinetic"}:
        files = list(HF_PACK)
        blob = load_complete(files)
        budget = PACK_BUDGET["hyperframes"]
        omitted: list[str] = []
        for drop in HF_OMIT_ORDER:
            if len(blob) <= budget:
                break
            files = [f for f in files if drop not in f.replace("\\", "/")]
            omitted.append(drop)
            blob = load_complete(files)
        if omitted:
            blob = f"\n\n# omitted from hyperframes pack (over budget): {', '.join(omitted)}\n" + blob
        return blob
    return load_complete(REMOTION_PACK)


def load_stage_skill(
    manifest: dict,
    stage_name: str,
    *,
    playbook: str | None = None,
    extra_skills: list[str] | None = None,
    layer3: list[str] | None = None,
) -> str:
    skill_rel = None
    for stage in manifest.get("stages") or []:
        if stage.get("name") == stage_name:
            skill_rel = stage.get("skill")
            break
    if not skill_rel:
        text = f"Stage {stage_name}: produce the canonical artifact. Follow schemas."
    else:
        path = repo_root() / "skills" / f"{skill_rel}.md"
        if not path.is_file():
            path = repo_root() / "skills" / skill_rel
            if not path.suffix:
                path = path.with_suffix(".md")
        if not path.is_file():
            text = f"Stage {stage_name}: produce schema-valid artifacts. Skill missing: {skill_rel}"
        else:
            text = path.read_text(encoding="utf-8")

    extra = load_playbook(playbook)
    if extra_skills:
        for rel in extra_skills:
            extra += load_meta(rel)
    if layer3:
        extra += load_layer3(layer3, exclude_picture=True)
    return text + extra
