"""Python-owned asset gather: TTS, stock, diagrams, footage cuts. Fail loud in production."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from runner import telemetry, tools_exec
from runner.artifacts import project_dir
from runner.karaoke import audio_path as tts_audio_path
from runner.karaoke import extract_words
from runner.tts import speak


class AssetError(RuntimeError):
    pass


def _add(
    assets: list[dict[str, Any]],
    *,
    asset_id: str,
    atype: str,
    path: str,
    source_tool: str,
    scene_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    row = {
        "id": asset_id,
        "type": atype,
        "path": path,
        "source_tool": source_tool,
        "scene_id": scene_id or "all",
    }
    if extra:
        row.update(extra)
    assets.append(row)


def _clip_weights_ok() -> bool:
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    if not cache.is_dir():
        return False
    return any(cache.glob("*clip-vit-base-patch32*"))


def _mermaid_for(scene: dict[str, Any], topic: str) -> str:
    """Canned/tests only. Production uses scene mermaid or a Gemini JSON mermaid."""
    label = str(scene.get("description") or scene.get("id") or topic)[:48].replace('"', "")
    sid = str(scene.get("id") or "n")
    return (
        "flowchart LR\n"
        f'  A["{topic[:32]}"] --> B["{label}"]\n'
        f'  B --> C["{sid}"]\n'
    )


def _resolve_mermaid(
    scene: dict[str, Any],
    topic: str,
    research: dict[str, Any] | None,
    *,
    canned: bool,
) -> str:
    existing = str(scene.get("mermaid") or "").strip()
    if existing:
        return existing
    if canned:
        return _mermaid_for(scene, topic)
    try:
        from runner.llm_gemini import default_model

        model = default_model()
        turn = model.run_stage(
            system="Return ONE JSON object {\"mermaid\": \"flowchart ...\"} for this scene. No fences.",
            user=json.dumps(
                {
                    "topic": topic,
                    "scene": {"id": scene.get("id"), "type": scene.get("type"), "description": scene.get("description")},
                    "research": (research or {}).get("data_points"),
                },
                default=str,
            ),
            tools=[],
            retry=0,
            max_rounds=1,
        )
        blob = turn.artifact if isinstance(turn.artifact, dict) else None
        if blob and blob.get("mermaid"):
            return str(blob["mermaid"]).strip()
    except Exception:
        return ""
    return ""


def _code_for(scene: dict[str, Any], script: dict[str, Any] | None) -> str:
    desc = str(scene.get("description") or "scene")
    text = ""
    sid = scene.get("id")
    for section in (script or {}).get("sections") or []:
        if section.get("id") == sid or not text:
            text = str(section.get("text") or "")
    return (
        f"# {desc}\n"
        "def explain(topic: str) -> str:\n"
        f"    evidence = {text[:120]!r}\n"
        "    return evidence.strip()\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    print(explain('topic'))\n"
    )


def _music_file(result: Any, hint: Path) -> Path | None:
    if not getattr(result, "success", False):
        return None
    for artifact in result.artifacts or []:
        path = Path(str(artifact))
        if path.is_file() and path.stat().st_size > 32:
            return path
    if hint.is_file() and hint.stat().st_size > 32:
        return hint
    return None


def _tool_available(name: str) -> bool:
    tool = tools_exec.registry().get(name)
    if tool is None:
        return False
    status = getattr(tool, "get_status", None)
    if not callable(status):
        return True
    return str(getattr(status(), "value", status())) == "available"


def _pick_music(assets_dir: Path, prompt: str, duration: int) -> tuple[Path | None, str]:
    """Library, then stock, then Lyria, then ElevenLabs Music. TTS keys are not Music SKUs."""
    dest_dir = assets_dir / "audio"
    dest_dir.mkdir(parents=True, exist_ok=True)
    listed = tools_exec.execute("music_library", {})
    if listed.success and isinstance(listed.data, dict):
        for track in listed.data.get("tracks") or []:
            path = Path(str(track.get("path") or ""))
            if path.is_file():
                dest = dest_dir / path.name
                if path.resolve() != dest.resolve():
                    shutil.copy2(path, dest)
                found = dest if dest.is_file() else path
                return found, "music_library"
    stock_query = "calm educational instrumental underscore"
    lyria_seconds = min(184, max(15, duration if duration <= 184 else 60))
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("pixabay_music", {"query": stock_query, "output_path": str(dest_dir / "bed.mp3")}),
        ("freesound_music", {"query": stock_query, "output_path": str(dest_dir / "bed.mp3")}),
        (
            "google_music",
            {
                "prompt": f"{stock_query}, no vocals. {prompt[:120]}",
                "output_path": str(dest_dir / "bed_lyria.mp3"),
                "duration_seconds": lyria_seconds,
            },
        ),
        (
            "music_gen",
            {
                "prompt": f"{stock_query}. {prompt[:120]}",
                "output_path": str(dest_dir / "bed.wav"),
                "duration_seconds": min(32, max(8, duration)),
                "force_instrumental": True,
            },
        ),
    ]
    for name, inputs in attempts:
        if name != "pixabay_music" and not _tool_available(name):
            continue
        result = tools_exec.execute(name, inputs)
        found = _music_file(result, Path(str(inputs.get("output_path") or dest_dir / "bed.mp3")))
        if found is not None:
            return found, name
    return None, ""


def _documentary_stock(assets_dir: Path, scene_plan: dict[str, Any], assets: list[dict[str, Any]]) -> None:
    queries = []
    for scene in scene_plan.get("scenes") or []:
        desc = scene.get("description") or scene.get("id") or "archive footage"
        queries.append({"query": str(desc), "scene_id": scene.get("id")})
    if not queries:
        return
    stock_dir = assets_dir / "video" / "stock"
    stock_dir.mkdir(parents=True, exist_ok=True)
    used_clip = False
    if _clip_weights_ok():
        corpus = tools_exec.execute(
            "corpus_builder",
            {
                "corpus_dir": str(stock_dir / "corpus"),
                "queries": [{"query": q["query"]} for q in queries[:6]],
            },
        )
        if corpus.success:
            corpus_dir = None
            if isinstance(corpus.data, dict):
                corpus_dir = corpus.data.get("corpus_dir") or corpus.data.get("output_dir")
            if not corpus_dir and corpus.artifacts:
                corpus_dir = str(Path(str(corpus.artifacts[0])))
            if corpus_dir:
                for j, q in enumerate(queries[:6]):
                    ranked = tools_exec.execute(
                        "clip_search",
                        {
                            "operation": "rank_for_slot",
                            "corpus_dir": str(corpus_dir),
                            "query_text": q["query"],
                            "k": 1,
                        },
                    )
                    if not ranked.success:
                        continue
                    rows = []
                    if isinstance(ranked.data, dict):
                        rows = ranked.data.get("results") or ranked.data.get("clips") or []
                    for clip in rows:
                        rec = clip.get("record") if isinstance(clip, dict) else {}
                        if not isinstance(rec, dict):
                            rec = clip if isinstance(clip, dict) else {}
                        rel = rec.get("local_path") or rec.get("path") or ""
                        path = Path(str(rel))
                        if not path.is_file():
                            path = Path(str(corpus_dir)) / rel
                        if path.is_file():
                            graded = assets_dir / "video" / f"grade_{j}{path.suffix}"
                            grade = tools_exec.execute(
                                "color_grade",
                                {"input_path": str(path), "output_path": str(graded), "profile": "neutral"},
                            )
                            final = Path(str(grade.artifacts[0])) if grade.success and grade.artifacts else path
                            if grade.success and graded.is_file():
                                final = graded
                            _add(
                                assets,
                                asset_id=f"stock_{j}",
                                atype="video",
                                path=str(final),
                                source_tool="clip_search",
                                scene_id=str(q["scene_id"] or "sc1"),
                            )
                            used_clip = True
                            break
    if used_clip:
        return
    result = tools_exec.execute(
        "direct_clip_search",
        {
            "output_dir": str(stock_dir),
            "queries": [{"query": q["query"]} for q in queries[:4]],
            "clips_per_query": 1,
            "extract_thumbnails": False,
            "timeout_seconds": 90,
        },
    )
    clips: list[Any] = []
    if result.success and isinstance(result.data, dict):
        clips = result.data.get("clips") or result.data.get("results") or []
    for artifact in result.artifacts or []:
        p = Path(str(artifact))
        if p.suffix.lower() in {".mp4", ".mov", ".webm"} and p.is_file():
            clips.append({"path": str(p)})
    added = 0
    for j, clip in enumerate(clips):
        path = Path(str(clip.get("path") if isinstance(clip, dict) else clip))
        if not path.is_file():
            continue
        graded = assets_dir / "video" / f"grade_{j}{path.suffix}"
        grade = tools_exec.execute(
            "color_grade",
            {"input_path": str(path), "output_path": str(graded), "profile": "neutral"},
        )
        final = graded if grade.success and graded.is_file() else path
        _add(
            assets,
            asset_id=f"stock_{j}",
            atype="video",
            path=str(final),
            source_tool="direct_clip_search",
            scene_id=str((queries[j]["scene_id"] if j < len(queries) else "sc1") or "sc1"),
        )
        added += 1
    if added == 0:
        raise AssetError("documentary B-roll search returned no real clips")


def gather(
    work_dir: Path,
    *,
    pipeline: str,
    scene_plan: dict[str, Any],
    script: dict[str, Any] | None,
    footage: list[Path],
    canned: bool,
    topic: str = "",
    duration: int = 10,
    require_karaoke: bool = False,
    require_music: bool = False,
    voice_id: str | None = None,
    research: dict[str, Any] | None = None,
    skip_types: set[str] | None = None,
    language_code: str | None = None,
    transcript: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assets_dir = project_dir(work_dir) / "assets"
    (assets_dir / "audio").mkdir(parents=True, exist_ok=True)
    (assets_dir / "video").mkdir(parents=True, exist_ok=True)
    (assets_dir / "images").mkdir(parents=True, exist_ok=True)
    assets: list[dict[str, Any]] = []
    extras: dict[str, Any] = {"mermaid": {}, "code": {}, "formula": {}}
    timestamps_path = assets_dir / "audio" / "timestamps.json"
    word_ts: list[dict[str, Any]] = []
    voice_performance = (script or {}).get("voice_performance") if isinstance(script, dict) else None

    skip = skip_types or set()
    sections = list((script or {}).get("sections") or [])
    if not canned and "narration" not in skip:
        if not sections and pipeline not in {"clip-factory", "documentary-montage", "podcast-repurpose"}:
            raise AssetError("script has no sections to narrate")
        for i, section in enumerate(sections):
            sid = section.get("id") or f"s{i+1}"
            text = str(section.get("text") or "").strip()
            if not text:
                continue
            from runner import progress

            progress.note(f"TTS {i + 1}/{len(sections)} {sid} ({len(text)} chars)", kind="assets")
            cues = section.get("delivery_cues") if isinstance(section.get("delivery_cues"), dict) else None
            performance = dict(voice_performance or {})
            if cues:
                performance["section_cues"] = cues
            out = assets_dir / "audio" / f"{sid}.wav"
            data = speak(
                text,
                output_path=str(out),
                voice_id=voice_id,
                voice_performance=performance or None,
                timestamps=require_karaoke,
                language_code=language_code,
            )
            resolved = tts_audio_path(data if isinstance(data, dict) else None, str(out))
            if isinstance(data, dict):
                word_ts.extend(extract_words(data))
            path = Path(resolved)
            if not path.is_file():
                raise AssetError(f"tts_selector produced no audio for {sid}")
            _add(
                assets,
                asset_id=f"vo_{sid}",
                atype="narration",
                path=str(path),
                source_tool="tts_selector",
                scene_id=str(sid),
            )
        if require_karaoke and not word_ts:
            raise AssetError("TTS returned no word timestamps; karaoke cannot be built")
    else:
        for i, section in enumerate(sections):
            sid = section.get("id") or f"s{i+1}"
            text = str(section.get("text") or "").strip()
            if not text:
                continue
            if "narration" in skip:
                continue
            out = assets_dir / "audio" / f"{sid}.wav"
            try:
                data = speak(
                    text,
                    output_path=str(out),
                    voice_id=voice_id,
                    voice_performance=voice_performance,
                    timestamps=False,
                    language_code=language_code,
                )
                resolved = tts_audio_path(data if isinstance(data, dict) else None, str(out))
                if isinstance(data, dict):
                    word_ts.extend(extract_words(data))
                path = Path(resolved)
                if path.is_file():
                    _add(
                        assets,
                        asset_id=f"vo_{sid}",
                        atype="narration",
                        path=str(path),
                        source_tool="tts_selector",
                        scene_id=str(sid),
                    )
            except Exception:
                continue

    if word_ts:
        timestamps_path.write_text(json.dumps({"words": word_ts}, indent=2), encoding="utf-8")

    for i, clip in enumerate(footage):
        src = clip
        if pipeline in {"talking-head", "podcast-repurpose"}:
            polished = assets_dir / "video" / f"speech_{i}.mp4"
            cut = tools_exec.execute(
                "silence_cutter",
                {"input_path": str(clip), "output_path": str(polished), "mode": "remove"},
            )
            if cut.success and polished.is_file():
                src = polished
            elif cut.success and cut.artifacts:
                cand = Path(cut.artifacts[0])
                if cand.is_file():
                    src = cand
            enhanced = assets_dir / "video" / f"face_{i}.mp4"
            face = tools_exec.execute("face_enhance", {"input_path": str(src), "output_path": str(enhanced)})
            if face.success and enhanced.is_file():
                src = enhanced
            elif face.success and face.artifacts:
                cand = Path(face.artifacts[0])
                if cand.is_file():
                    src = cand
            framed = assets_dir / "video" / f"reframe_{i}.mp4"
            reframe = tools_exec.execute(
                "auto_reframe",
                {"input_path": str(src), "output_path": str(framed), "preset": "landscape"},
            )
            if reframe.success and framed.is_file():
                src = framed
        if pipeline == "localization-dub" and not canned:
            vo = next((Path(a["path"]) for a in assets if a.get("type") == "narration" and Path(str(a.get("path"))).is_file()), None)
            if vo and src.is_file():
                lips = assets_dir / "video" / f"lipsync_{i}.mp4"
                sync = tools_exec.execute(
                    "lip_sync",
                    {"video_path": str(src), "audio_path": str(vo), "output_path": str(lips)},
                )
                if sync.success and lips.is_file():
                    src = lips
                elif sync.success and sync.artifacts:
                    cand = Path(sync.artifacts[0])
                    if cand.is_file():
                        src = cand
        _add(
            assets,
            asset_id=f"footage_{i}",
            atype="video",
            path=str(src),
            source_tool="silence_cutter" if src != clip else "ingest",
            scene_id=None,
        )

    if pipeline == "documentary-montage" and not canned:
        _documentary_stock(assets_dir, scene_plan, assets)
    elif pipeline == "documentary-montage" and canned:
        from runner.ingest import _dummy_clip

        fallback = assets_dir / "video" / "stock_fallback.mp4"
        _dummy_clip(fallback)
        _add(
            assets,
            asset_id="stock_fallback",
            atype="video",
            path=str(fallback),
            source_tool="ingest",
            scene_id="sc1",
        )

    for scene in scene_plan.get("scenes") or []:
        stype = (scene.get("type") or "").lower()
        sid = scene.get("id") or "sc"
        if stype in {"diagram", "map", "timeline", "etymology"}:
            definition = _resolve_mermaid(scene, topic or pipeline, research, canned=canned)
            extras["mermaid"][str(sid)] = definition
            if not definition.strip():
                continue
            png = assets_dir / "images" / f"{sid}.png"
            result = tools_exec.execute(
                "diagram_gen",
                {
                    "diagram_type": "mermaid",
                    "definition": definition,
                    "title": scene.get("description") or sid,
                    "output_path": str(png),
                },
            )
            path = png if png.is_file() else None
            if result.success and result.artifacts:
                cand = Path(result.artifacts[0])
                if cand.is_file():
                    path = cand
            if path and path.is_file():
                extras["mermaid"][str(sid)] = definition
                _add(
                    assets,
                    asset_id=f"diagram_{sid}",
                    atype="diagram",
                    path=str(path),
                    source_tool="diagram_gen",
                    scene_id=str(sid),
                )
            elif not canned:
                raise AssetError(f"diagram_gen produced no image for {sid}")

        if stype == "code" and not canned:
            snippet = assets_dir / "images" / f"{sid}.png"
            code = str(scene.get("code_snippet") or "").strip() or _code_for(scene, script)
            result = tools_exec.execute(
                "code_snippet",
                {"code": code, "language": "python", "output_path": str(snippet), "title": str(sid)},
            )
            path = None
            if result.success and result.artifacts:
                cand = Path(result.artifacts[0])
                if cand.is_file():
                    path = cand
            if path and path.is_file():
                extras["code"][str(sid)] = code
                _add(
                    assets,
                    asset_id=f"code_{sid}",
                    atype="code_snippet",
                    path=str(path),
                    source_tool="code_snippet",
                    scene_id=str(sid),
                )
            else:
                raise AssetError(f"code_snippet failed for {sid}")

        if stype == "math" and not canned:
            tex = assets_dir / "images" / f"{sid}_math.mp4"
            formula = str(scene.get("formula_tex") or scene.get("description") or "E = mc^2")
            result = tools_exec.execute(
                "math_animate",
                {
                    "scene_code": (
                        "from manim import *\n"
                        f"class {str(sid).title().replace('_','')}Scene(Scene):\n"
                        "    def construct(self):\n"
                        f"        t = MathTex(r\"{formula[:80]}\")\n"
                        "        self.play(Write(t))\n"
                        "        self.wait(1)\n"
                    ),
                    "output_path": str(tex),
                },
            )
            if result.success and result.artifacts:
                p = Path(str(result.artifacts[0]))
                if p.is_file():
                    extras["formula"][str(sid)] = formula
                    _add(
                        assets,
                        asset_id=f"math_{sid}",
                        atype="animation",
                        path=str(p),
                        source_tool="math_animate",
                        scene_id=str(sid),
                    )

    if not canned and pipeline in {"animated-explainer", "animation", "hybrid"} and "image" not in skip:
        still = assets_dir / "images" / "still.png"
        result = tools_exec.execute(
            "image_selector",
            {
                "prompt": (script or {}).get("title") or topic or pipeline,
                "output_path": str(still),
            },
        )
        path = still if still.is_file() else None
        if result.success and result.artifacts:
            cand = Path(result.artifacts[0])
            if cand.is_file():
                path = cand
        if path and path.is_file():
            if "local_diffusion" in str(result.data or "").lower():
                raise AssetError("image_selector selected blocked local_diffusion")
            _add(
                assets,
                asset_id="still_1",
                atype="image",
                path=str(path),
                source_tool="image_selector",
                scene_id="sc1",
            )

    music_missing = False
    if not canned and "music" not in skip:
        bed, music_tool = _pick_music(
            assets_dir,
            f"underscored bed for {(script or {}).get('title') or topic or pipeline}",
            duration,
        )
        if bed and bed.is_file():
            _add(
                assets,
                asset_id="music_1",
                atype="music",
                path=str(bed),
                source_tool=music_tool or "music_library",
                scene_id="sc1",
            )
        elif require_music:
            music_missing = True
            telemetry.record_flag(
                "music_missing",
                "no music bed from library, pixabay, freesound, lyria, or music_gen; continuing VO-only",
            )

    if pipeline == "localization-dub" and not canned:
        segs = (transcript or {}).get("segments") or []
        if not segs and (transcript or {}).get("text"):
            segs = [{"start": 0, "end": duration, "text": transcript.get("text")}]
        if segs:
            srt = assets_dir / "audio" / "captions.srt"
            sub = tools_exec.execute("subtitle_gen", {"segments": segs, "format": "srt", "output_path": str(srt)})
            path = srt if srt.is_file() else None
            if sub.success and sub.artifacts:
                cand = Path(sub.artifacts[0])
                if cand.is_file():
                    path = cand
            if path and path.is_file():
                _add(
                    assets,
                    asset_id="subs_1",
                    atype="subtitle",
                    path=str(path),
                    source_tool="subtitle_gen",
                    scene_id="sc1",
                )

    return {
        "version": "1.0",
        "assets": assets,
        "metadata": {
            "timestamps": str(timestamps_path) if word_ts else None,
            "mermaid": extras["mermaid"],
            "code": extras["code"],
            "formula": extras["formula"],
            "music_missing": music_missing,
        },
    }
