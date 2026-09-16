"""Driver contracts: the API agent loop owns YAML stages the way Cursor OM does."""

from __future__ import annotations

from pathlib import Path

from runner.llm_gemini import LLMTurn
from runner.om_agent import interpret_review, stage_tools
from runner.pipeline import load, stages
from tools.base_tool import ToolResult

PIPELINES = [
    "animated-explainer",
    "animation",
    "character-animation",
    "cinematic",
    "documentary-montage",
    "hybrid",
    "talking-head",
    "screen-demo",
    "avatar-spokesperson",
    "clip-factory",
    "podcast-repurpose",
    "localization-dub",
]

FOOTAGE = {
    "talking-head",
    "hybrid",
    "clip-factory",
    "podcast-repurpose",
    "localization-dub",
    "cinematic",
    "avatar-spokesperson",
}


def _artifact(stage: str, pipeline: str) -> dict:
    from runner import canned, stubs

    topic = "driver topic"
    if stage == "research":
        return stubs.research_brief(topic)
    if stage == "proposal":
        return stubs.proposal_packet(topic, pipeline, "remotion")
    if stage == "idea":
        brief = stubs.brief(topic)
        if pipeline == "screen-demo":
            brief["metadata"] = {"production_mode": "synthetic_terminal"}
        return brief
    if stage == "script":
        return stubs.script(topic)
    if stage == "scene_plan":
        return stubs.scene_plan(pipeline, topic)
    if stage == "character_design":
        return {
            "version": "1.0",
            "characters": [
                {
                    "id": "hero",
                    "role": "lead",
                    "body_type": "biped",
                    "style": "flat",
                    "required_emotions": ["neutral"],
                    "required_actions": ["walk"],
                }
            ],
        }
    if stage == "rig_plan":
        return {
            "version": "1.0",
            "characters": [
                {
                    "character_id": "hero",
                    "parts": [{"id": "torso", "kind": "body", "layer": 1}],
                    "joints": {"hip": {"pivot": [0.5, 0.5]}},
                    "layers": ["body"],
                    "required_poses": ["idle"],
                }
            ],
        }
    if stage == "assets":
        return canned.explainer_10s(topic)["asset_manifest"]
    if stage == "edit":
        return canned.explainer_10s(topic)["edit_decisions"]
    if stage == "compose":
        return stubs.render_report()
    if stage == "publish":
        return stubs.publish_log()
    return {"version": "1.0", "stage": stage}


class RecordingModel:
    is_mock = True

    def __init__(self) -> None:
        self.stages: list[str] = []
        self.tools_by_stage: dict[str, list[str]] = {}
        self.tool_execs: list[str] = []
        self.pipeline = "animated-explainer"

    def generate_vision(self, *, system, user, images):
        return LLMTurn(artifact={"pass": True, "severity": "ok", "issues": [], "rewrite_hint": "", "verdict": "pass"})

    def run_stage(self, *, system, user, tools, retry, on_tool=None, max_rounds=8, **_kwargs):
        stage = "unknown"
        if "Stage=" in user:
            stage = user.split("Stage=", 1)[1].split(".", 1)[0].strip()
        names = [str(t.get("name")) for t in tools or []]
        self.stages.append(stage)
        self.tools_by_stage[stage] = names
        if on_tool and stage == "research" and "web_search" in names:
            on_tool("web_search", {"query": "driver research"})
            self.tool_execs.append("web_search")
        if on_tool and stage == "compose":
            if "write_file" in names:
                on_tool("write_file", {"path": "atelier/Scene.tsx", "content": "export const Scene = () => null"})
                self.tool_execs.append("write_file")
            if "atelier_still" in names:
                on_tool("atelier_still", {"slug": "driver", "composition_id": "Scene"})
                self.tool_execs.append("atelier_still")
            if "write_file" in names:
                on_tool("write_file", {"path": "atelier/Scene.tsx", "content": "export const Scene = () => null; // patch"})
                self.tool_execs.append("write_file_patch")
            if "video_compose" in names:
                self.tool_execs.append("video_compose")
            if "hyperframes_compose" in names:
                self.tool_execs.append("hyperframes_compose")
        return LLMTurn(artifact=_artifact(stage, self.pipeline))


def _job(pipeline: str, tmp_path: Path) -> dict:
    job = {
        "job_id": f"driver-{pipeline}",
        "pipeline": pipeline,
        "review_mode": False,
        "prefs": {"topic": "driver topic", "duration_seconds": 10, "composition_mode": "templated"},
    }
    if pipeline in FOOTAGE:
        dummy = tmp_path / "source.mp4"
        dummy.write_bytes(b"not-a-real-mp4")
        job["asset_keys"] = [str(dummy)]
    return job


def _patch_runtime(monkeypatch) -> None:
    from runner import om_agent, tools_exec

    monkeypatch.setattr(om_agent.envelope, "required_missing", lambda _m: [])
    monkeypatch.setattr(
        om_agent.envelope,
        "summary",
        lambda: {"composition_runtimes": {"remotion": True, "hyperframes": True}, "runtime_warnings": []},
    )
    monkeypatch.setattr(
        om_agent,
        "doctors",
        lambda: {"remotion": True, "hyperframes": True, "ffmpeg": True, "motion_canvas": False},
    )
    monkeypatch.setattr(
        om_agent,
        "review_source_media",
        lambda *a, **k: {
            "version": "1.0",
            "files": [{"path": "source.mp4", "media_type": "video", "reviewed": True, "content_summary": "driver source", "usable_for": ["source footage"]}],
            "summary": "driver source footage",
            "planning_implications": ["Use supplied media as source"],
        },
    )
    monkeypatch.setattr(om_agent.reference, "analyze", lambda *a, **k: None)
    monkeypatch.setattr(
        tools_exec,
        "execute",
        lambda name, inputs: ToolResult(success=True, data={"results": [{"url": "https://example.com/a", "title": "A"}]}),
    )


def test_yaml_tools_plus_file_tools_offered() -> None:
    explainer = load("animated-explainer")
    research = stage_tools(explainer, "research")
    assert research[:2] == ["web_search", "web_fetch"]
    assert "read_file" in research
    compose = stage_tools(explainer, "compose")
    assert "video_compose" in compose
    assert "hyperframes_compose" in compose
    assert "write_file" in compose
    assert "atelier_still" in compose
    assert "typecheck_atelier" in compose
    character = load("character-animation")
    assert stages(character).index("character_design") < stages(character).index("rig_plan")
    assert "character_spec_generator" in stage_tools(character, "character_design")
    screen = load("screen-demo")
    assert stages(screen)[0] == "idea"
    doc = load("documentary-montage")
    assert "publish" not in stages(doc)


def test_all_twelve_pipelines_enter_every_yaml_stage(tmp_path, monkeypatch) -> None:
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    for name in PIPELINES:
        model = RecordingModel()
        model.pipeline = name
        result = run(_job(name, tmp_path), work_dir=tmp_path / name, model=model)
        assert result["status"] == "done", f"{name}: {result}"
        expected = stages(load(name))
        assert model.stages == expected, f"{name}: {model.stages} != {expected}"
        if name == "documentary-montage":
            assert "publish" not in model.stages
        if name == "screen-demo":
            brief = (tmp_path / name / "project" / "artifacts" / "brief.json").read_text(encoding="utf-8")
            assert "synthetic_terminal" in brief
        if "research" in expected:
            assert "web_search" in model.tools_by_stage["research"]
            assert "web_search" in model.tool_execs
            yaml_research = load(name)
            for tool in ["web_search", "web_fetch"]:
                if tool in (yaml_research["stages"][0].get("tools_available") or []):
                    assert tool in model.tools_by_stage["research"]
        if "assets" in expected:
            yaml_tools = list(load(name)["stages"])
            assets_entry = next(s for s in yaml_tools if s["name"] == "assets")
            offered = model.tools_by_stage["assets"]
            for tool in assets_entry.get("tools_available") or []:
                assert tool in offered, f"{name} assets missing {tool}"
            assert "read_file" in offered
        if "compose" in expected:
            compose_tools = model.tools_by_stage["compose"]
            compose_entry = next(s for s in load(name)["stages"] if s["name"] == "compose")
            for tool in compose_entry.get("tools_available") or []:
                assert tool in compose_tools, f"{name} compose missing {tool}"
            assert "write_file" in compose_tools
            assert "atelier_still" in compose_tools
            assert "write_file" in model.tool_execs
            assert "atelier_still" in model.tool_execs
            assert "write_file_patch" in model.tool_execs


def test_live_loop_does_not_presearch_or_skip_llm() -> None:
    root = Path(__file__).resolve().parents[1]
    agent = (root / "runner" / "om_agent.py").read_text(encoding="utf-8")
    loop = (root / "runner" / "loop.py").read_text(encoding="utf-8")
    assert "gather_research" not in agent
    assert "assets.gather" not in agent
    assert "compile_edit" not in agent
    assert "author_with_gemini" not in agent
    assert "run_character_chain" not in agent
    assert "generate_shots" not in agent
    assert "picture_patch" not in agent
    assert "if not canned_mode:" in loop
    assert "om_agent.run" in loop
    from runner import ep_loop

    assert "assets" not in ep_loop.SKIP_LLM
    assert "compose" not in ep_loop.SKIP_LLM
    assert "assets" in ep_loop.LLM_STAGES
    assert "compose" in ep_loop.LLM_STAGES


def test_mode_does_not_silent_default_atelier() -> None:
    from runner import mode

    assert mode.default_mode("animated-explainer") == "undecided"
    assert mode.resolve("animated-explainer", {}, None, canned=False) == "undecided"
    assert mode.resolve("animated-explainer", {}, None, canned=True) == "templated"


def test_reviewer_critical_requires_proposed_fix() -> None:
    gate = interpret_review(
        {
            "verdict": "pass",
            "stage": "compose",
            "findings": [{"id": "f1", "severity": "critical", "description": "black frame"}],
        }
    )
    assert gate["verdict"] == "revise"
    ok = interpret_review(
        {
            "verdict": "pass",
            "stage": "compose",
            "findings": [
                {
                    "id": "f1",
                    "severity": "critical",
                    "description": "black frame",
                    "proposed_fix": "raise contrast",
                }
            ],
        }
    )
    assert ok["verdict"] == "pass"


def test_decision_log_appends_same_subject() -> None:
    from runner import decisions

    log = decisions.empty("p1")
    log = decisions.append(
        log,
        decision_id="d1",
        stage="proposal",
        category="voice_selection",
        subject="Narration TTS provider",
        options=[decisions.option("a", "A", 0.5, "first")],
        selected="a",
        reason="first pick",
        project_id="p1",
    )
    log = decisions.append(
        log,
        decision_id="d2",
        stage="assets",
        category="voice_selection",
        subject="Narration TTS provider",
        options=[
            decisions.option("a", "A", 0.2, "first", "changed"),
            decisions.option("b", "B", 0.9, "fallback"),
        ],
        selected="b",
        reason="fallback after quota",
        project_id="p1",
    )
    current = decisions.current(log, "voice_selection", "Narration TTS provider")
    assert current is not None
    assert current["selected"] == "b"
    assert len([d for d in log["decisions"] if d["category"] == "voice_selection"]) == 2


def test_human_gate_stamp_replaces_director_approval() -> None:
    from runner.om_agent import stamp_human_gate

    pending = {
        "version": "1.0",
        "approval": {
            "status": "pending",
            "approved_by": "auto-decide-agent",
            "timestamp": "2026-03-30T13:00:00Z",
        },
    }
    stamped = stamp_human_gate(pending, artifact_name="proposal_packet", prefs={"budget_cap_usd": 60})
    assert stamped["approval"] == {"status": "approved", "approved_budget_usd": 60.0}
    idea = stamp_human_gate({"approval": {"status": "pending"}}, artifact_name="brief")
    assert idea["approval"] == {"status": "pending"}


def test_proposal_director_extra_approval_keys_do_not_fail_schema(tmp_path, monkeypatch) -> None:
    from runner.artifacts import read_artifact
    from runner.llm_gemini import LLMTurn
    from runner.om_agent import run

    class PendingApprovalModel(RecordingModel):
        def run_stage(self, *, system, user, tools, retry, on_tool=None, max_rounds=8, **_kwargs):
            turn = super().run_stage(
                system=system, user=user, tools=tools, retry=retry, on_tool=on_tool, max_rounds=max_rounds
            )
            stage = "unknown"
            if "Stage=" in user:
                stage = user.split("Stage=", 1)[1].split(".", 1)[0].strip()
            if stage != "proposal" or not isinstance(turn.artifact, dict):
                return turn
            packet = dict(turn.artifact)
            packet["approval"] = {
                "status": "pending",
                "approved_by": "auto-decide-agent",
                "approved_budget_usd": 60.0,
                "timestamp": "2026-03-30T13:00:00Z",
            }
            return LLMTurn(artifact=packet)

    _patch_runtime(monkeypatch)
    job = _job("animated-explainer", tmp_path)
    job["prefs"] = {**job["prefs"], "budget_cap_usd": 60}
    result = run(job, work_dir=tmp_path, model=PendingApprovalModel())
    assert result["status"] == "done", result
    packet = read_artifact(tmp_path, "proposal_packet")
    assert packet is not None
    assert packet["approval"] == {"status": "approved", "approved_budget_usd": 60.0}


def test_api_overlays_leave_approval_to_the_runner() -> None:
    root = Path(__file__).resolve().parents[1]
    proposal = (root / "skills" / "meta" / "api-llm-proposal.md").read_text(encoding="utf-8")
    runner = (root / "skills" / "meta" / "api-llm-runner.md").read_text(encoding="utf-8")
    assert '{"status": "pending"}' in proposal
    assert "set `approval.status` to `approved`" not in proposal
    assert "set `approval.status` to `approved`" not in runner
    assert "runner stamps the human gate" in runner


def test_gated_checkpoint_is_completed_and_approved(tmp_path, monkeypatch) -> None:
    from lib.checkpoint import read_checkpoint
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    model = RecordingModel()
    result = run(_job("animated-explainer", tmp_path), work_dir=tmp_path, model=model)
    assert result["status"] == "done", result
    cp = read_checkpoint(tmp_path, "project", "proposal")
    assert cp is not None
    assert cp["status"] == "completed"
    assert cp["human_approved"] is True
    assert "proposal_packet" in (cp.get("artifacts") or {})


def test_resume_skips_completed_research(tmp_path, monkeypatch) -> None:
    from lib.checkpoint import get_next_stage
    from runner import checkpoint
    from runner.om_agent import run
    from runner.stubs import research_brief

    _patch_runtime(monkeypatch)
    work = tmp_path
    checkpoint.init_job(work, "animated-explainer", "driver topic")
    from runner.artifacts import write_artifact

    brief = research_brief("driver topic")
    write_artifact(work, "research_brief", brief)
    checkpoint.write_stage(
        work,
        "animated-explainer",
        "research",
        "completed",
        artifacts={"research_brief": brief},
    )
    assert get_next_stage(work, "project", "animated-explainer") == "proposal"
    model = RecordingModel()
    result = run(_job("animated-explainer", tmp_path), work_dir=work, model=model)
    assert result["status"] == "done", result
    assert "research" not in model.stages
    assert model.stages[0] == "proposal"


def test_file_tools_stay_inside_workspace(tmp_path) -> None:
    from runner.tools_exec import execute, registry
    from runner.workspace_root import set_root

    registry()
    set_root(tmp_path)
    ok = execute("write_file", {"path": "hello.txt", "content": "hi"})
    assert ok.success, ok.error
    listed = execute("list_dir", {"path": "."})
    assert listed.success
    assert "hello.txt" in (listed.data or {}).get("entries", [])
    escaped = execute("write_file", {"path": "../outside.txt", "content": "nope"})
    assert escaped.success is False


def test_blocked_names_removed() -> None:
    from runner import tools_exec

    assert not hasattr(tools_exec, "BLOCKED_NAMES")
    assert tools_exec.registry().get("read_file") is not None
    assert tools_exec.registry().get("atelier_still") is not None
    assert tools_exec.registry().get("hyperframes_lint") is not None


def test_json_only_tsx_dump_is_not_a_compose_artifact() -> None:
    from jsonschema import ValidationError
    from schemas.artifacts import validate_artifact

    try:
        validate_artifact("render_report", {"version": "1.0", "composition_tsx": "export const Scene = () => null"})
    except ValidationError:
        return
    raise AssertionError("JSON-only TSX dump must fail render_report schema")


def test_cinematic_fails_without_keys_or_media(tmp_path, monkeypatch) -> None:
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    monkeypatch.setattr("runner.directors.has_video_api_keys", lambda: False)
    result = run(
        {
            "job_id": "cin-fail",
            "pipeline": "cinematic",
            "prefs": {"topic": "trailer", "duration_seconds": 10},
        },
        work_dir=tmp_path,
        model=RecordingModel(),
    )
    assert result["status"] == "failed"
    assert result["error"] == "delivery_promise"


def test_reference_analyst_runs_before_stages(tmp_path, monkeypatch) -> None:
    from runner.om_agent import run

    calls: list[str] = []

    def fake_analyze(job, work_dir):
        calls.append(str(job.get("reference_url")))
        return {"source": job.get("reference_url"), "steps": ["video_analyzer"]}

    _patch_runtime(monkeypatch)
    from runner import om_agent

    monkeypatch.setattr(om_agent.reference, "analyze", fake_analyze)
    job = _job("animated-explainer", tmp_path)
    job["reference_url"] = "https://example.com/ref.mp4"
    result = run(job, work_dir=tmp_path, model=RecordingModel())
    assert result["status"] == "done", result
    assert calls == ["https://example.com/ref.mp4"]


def test_canned_checkpoints_attach_canonical_artifacts(tmp_path) -> None:
    from lib.checkpoint import read_checkpoint
    from runner import canned, checkpoint, pipeline, stubs
    from runner.artifacts import read_artifact, stage_artifact_name, write_artifact
    from runner.loop import SKIP_STAGES

    name = "animated-explainer"
    canned.write_canned(tmp_path, canned.for_pipeline(name, "t"))
    write_artifact(tmp_path, "research_brief", stubs.research_brief("t"))
    write_artifact(tmp_path, "proposal_packet", stubs.proposal_packet("t", name, "remotion"))
    checkpoint.init_job(tmp_path, name, "t")
    for st in pipeline.stages(pipeline.load(name)):
        if st in SKIP_STAGES:
            continue
        art_name = stage_artifact_name(st)
        payload = read_artifact(tmp_path, art_name)
        artifacts = {art_name: payload} if isinstance(payload, dict) else {}
        checkpoint.write_stage(tmp_path, name, st, artifacts=artifacts)
    cp = read_checkpoint(tmp_path, "project", "proposal")
    assert cp is not None
    assert cp["status"] == "completed"
    assert cp["human_approved"] is True
    assert "proposal_packet" in cp["artifacts"]


def test_cost_tracker_writes_project_log(tmp_path, monkeypatch) -> None:
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    result = run(_job("animated-explainer", tmp_path), work_dir=tmp_path, model=RecordingModel())
    assert result["status"] == "done", result
    log = tmp_path / "project" / "cost_log.json"
    artifact = tmp_path / "project" / "artifacts" / "cost_log.json"
    assert log.is_file() or artifact.is_file()


def test_model_role_uses_atelier_for_compose() -> None:
    from runner.om_agent import model_role, reviewer_role

    assert model_role("compose") == "atelier"
    assert model_role("assets") == "atelier"
    assert model_role("research") == "planner"
    assert reviewer_role("compose") == "atelier"


def test_live_clients_request_atelier_role(tmp_path, monkeypatch) -> None:
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    kinds: list[str] = []
    shared = RecordingModel()

    def fake_default(kind: str = "default"):
        kinds.append(kind)
        return shared

    monkeypatch.setattr("runner.om_agent.default_model", fake_default)
    result = run(_job("animated-explainer", tmp_path), work_dir=tmp_path, model=None)
    assert result["status"] == "done", result
    assert "planner" in kinds
    assert "atelier" in kinds
    assert "visual_qa" in kinds


def test_decision_log_shortlist_before_proposal_gate(tmp_path, monkeypatch) -> None:
    from runner.artifacts import read_artifact
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    result = run(_job("animated-explainer", tmp_path), work_dir=tmp_path, model=RecordingModel())
    assert result["status"] == "done", result
    log = read_artifact(tmp_path, "decision_log")
    assert log is not None
    cats = {row["category"] for row in log["decisions"]}
    assert "pipeline_selection" in cats
    assert "concept_selection" in cats
    assert "composition_mode" in cats
    assert "render_runtime_selection" in cats
    assert "playbook_selection" in cats
    assert "budget_tradeoff" in cats


def test_exhausted_review_fails_the_job(tmp_path, monkeypatch) -> None:
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "runner.om_agent._reviewer_pass",
        lambda *a, **k: {
            "verdict": "revise",
            "feedback": "stills failed",
            "target": "compose",
            "source": "vision",
            "blocking": True,
        },
    )
    result = run(_job("animated-explainer", tmp_path), work_dir=tmp_path, model=RecordingModel())
    assert result["status"] == "failed"
    assert result["error"] == "review"


def test_exhausted_chai_review_passes_with_warnings(tmp_path, monkeypatch) -> None:
    from lib.checkpoint import read_checkpoint
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "runner.om_agent._reviewer_pass",
        lambda *a, **k: {
            "verdict": "revise",
            "feedback": "taste could be sharper",
            "target": "proposal",
            "source": "chai",
            "blocking": False,
        },
    )
    result = run(_job("animated-explainer", tmp_path), work_dir=tmp_path, model=RecordingModel())
    assert result["status"] == "done", result
    cp = read_checkpoint(tmp_path, "project", "proposal")
    assert cp is not None
    assert cp["status"] == "completed"
    assert (cp.get("review") or {}).get("pass_with_warnings") is True


def test_live_exhausted_review_does_not_pass_with_warnings() -> None:
    from runner.om_agent import _exhausted_review_gate

    live = _exhausted_review_gate(
        {"verdict": "revise", "feedback": "stock cut-list", "blocking": False, "source": "chai"},
        mock=False,
        stage="edit",
    )
    assert live["verdict"] == "revise"
    assert live["blocking"] is True
    canned = _exhausted_review_gate(
        {"verdict": "revise", "feedback": "taste", "blocking": False, "source": "chai"},
        mock=True,
        stage="edit",
    )
    assert canned["verdict"] == "pass"
    assert canned.get("pass_with_warnings") is True


def test_compose_review_without_stills_is_blocking() -> None:
    from runner.om_agent import _reviewer_pass
    from runner.stubs import render_report

    gate = _reviewer_pass(
        RecordingModel(),
        stage="compose",
        artifact=render_report(),
        mock=False,
        stills=[],
    )
    assert gate["verdict"] == "revise"
    assert gate["blocking"] is True
    assert gate["source"] == "stills"


def test_live_persist_does_not_stub_yaml_siblings(tmp_path) -> None:
    from runner.om_agent import persist_produces
    from runner.stubs import render_report

    written, missing = persist_produces(
        tmp_path,
        produces=["render_report", "final_review", "character_qa_report"],
        primary="render_report",
        artifact=render_report(),
        extras={},
        mock=False,
        stills=[],
    )
    assert "final_review" in missing
    assert "character_qa_report" in missing
    assert "final_review" not in written
    assert "character_qa_report" not in written

    pose_written, pose_missing = persist_produces(
        tmp_path,
        produces=["rig_plan", "pose_library"],
        primary="rig_plan",
        artifact={
            "version": "1.0",
            "characters": [
                {
                    "character_id": "hero",
                    "parts": [{"id": "torso", "kind": "body", "layer": 1}],
                    "joints": {"hip": {"pivot": [0.5, 0.5]}},
                    "layers": ["body"],
                    "required_poses": ["idle"],
                }
            ],
        },
        extras={},
        mock=False,
        stills=[],
    )
    assert "pose_library" in pose_missing
    assert "pose_library" not in pose_written


def test_claimed_files_are_disk_facts(tmp_path, monkeypatch) -> None:
    from runner.om_agent import claimed_files_missing
    from runner.stubs import render_report

    monkeypatch.setattr("runner.ffmpeg_bin.find_ffprobe", lambda: None)
    assert claimed_files_missing("compose", render_report(), tmp_path)
    dest = tmp_path / "project" / "renders"
    dest.mkdir(parents=True)
    (dest / "final.mp4").write_bytes(b"x" * 64)
    assert claimed_files_missing("compose", render_report(), tmp_path) == []

    manifest = {
        "version": "1.0",
        "assets": [
            {
                "id": "a1",
                "type": "image",
                "path": "assets/images/scene-1.png",
                "source_tool": "image_selector",
                "scene_id": "s1",
            }
        ],
    }
    assert claimed_files_missing("assets", manifest, tmp_path)
    png = tmp_path / "project" / "assets" / "images" / "scene-1.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(b"png")
    assert claimed_files_missing("assets", manifest, tmp_path) == []


def test_reviewer_sees_existing_decision_log() -> None:
    from runner.om_agent import review_siblings_satisfy, schema_prompt

    log = {
        "version": "1.0",
        "project_id": "project",
        "decisions": [
            {
                "category": "render_runtime_selection",
                "options_considered": [{"option_id": "remotion"}, {"option_id": "hyperframes"}],
                "selected": "remotion",
            },
            {"category": "composition_mode", "selected": "atelier"},
            {"category": "playbook_selection", "selected": "clean-professional"},
            {"category": "budget_tradeoff", "selected": "proceed"},
            {"category": "concept_selection", "selected": "c1"},
        ],
    }
    gate = {
        "verdict": "revise",
        "feedback": "Please add the decision_log artifact to satisfy the audit trail requirements.",
        "target": "decision_log",
    }
    engines = {"remotion": True, "hyperframes": True}
    out = review_siblings_satisfy(gate, {"decision_log": log}, stage="proposal", engines=engines)
    assert out["verdict"] == "pass"
    assert out["resolved_by"] == "workspace_decision_log"
    schema = schema_prompt("proposal_packet")
    assert len(schema) > 8000
    assert "line_items" in schema
    assert "budget_verdict" in schema


def test_yaml_produces_persist_pose_library_and_final_review(tmp_path, monkeypatch) -> None:
    from runner.artifacts import read_artifact
    from runner.om_agent import run

    _patch_runtime(monkeypatch)
    name = "character-animation"
    result = run(_job(name, tmp_path), work_dir=tmp_path / name, model=RecordingModel())
    assert result["status"] == "done", result
    pose = read_artifact(tmp_path / name, "pose_library")
    timeline = read_artifact(tmp_path / name, "action_timeline")
    final = read_artifact(tmp_path / name, "final_review")
    qa = read_artifact(tmp_path / name, "character_qa_report")
    assert pose and pose.get("characters")
    assert timeline and timeline.get("scenes")
    assert final and final.get("status") == "pass"
    assert qa and qa.get("status") == "pass"


def test_continue_after_does_not_python_stitch() -> None:
    from pathlib import Path

    agent = (Path(__file__).resolve().parents[1] / "runner" / "om_agent.py").read_text(encoding="utf-8")
    overlay = (Path(__file__).resolve().parents[1] / "skills" / "meta" / "api-llm-runner.md").read_text(encoding="utf-8")
    assert "assemble_final" not in agent
    assert "JSON report is not a video" in overlay
    assert "Python will not generate" in overlay
    assert "[:8000]" not in agent.split("def schema_prompt", 1)[-1].split("def _reviewer_pass", 1)[0]
    assert "json.dumps(load_schema(name), default=str)[:8000]" not in agent
    assert "tools[:32]" not in (Path(__file__).resolve().parents[1] / "runner" / "llm_gemini.py").read_text(encoding="utf-8")


def test_function_declarations_are_not_truncated() -> None:
    from runner.llm_gemini import function_declarations

    decls = function_declarations([{"name": f"tool_{i}", "parameters": {"type": "object"}} for i in range(40)])
    assert len(decls) == 40


def _assert_gemini_schema(node: object) -> None:
    from runner.llm_gemini import _GEMINI_KEYS

    assert isinstance(node, dict)
    extra = set(node) - _GEMINI_KEYS
    assert not extra, extra
    if "enum" in node:
        assert all(isinstance(item, str) for item in node["enum"]), node["enum"]
    typ = node.get("type")
    if typ == "array":
        assert "items" in node
        _assert_gemini_schema(node["items"])
    elif "items" in node:
        _assert_gemini_schema(node["items"])
    props = node.get("properties")
    if isinstance(props, dict):
        for child in props.values():
            _assert_gemini_schema(child)


def test_gemini_parameters_fix_assets_and_compose_400_shapes() -> None:
    from runner.llm_gemini import function_declarations, gemini_parameters

    raw = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "image_list": {"type": "array", "description": "refs"},
            "workflow_model_stack": {"type": "array", "items": {"type": "object"}},
            "edit_decisions": {"type": "object", "description": "Full edit_decisions artifact"},
            "const_version": {"const": "1.0"},
            "fps": {"type": "string", "enum": [24, 30, 60]},
            "crf": {"type": "number", "exclusiveMinimum": 0, "minimum": 0},
        },
    }
    out = gemini_parameters(raw)
    _assert_gemini_schema(out)
    assert out["properties"]["image_list"]["items"]["type"] == "string"
    assert out["properties"]["workflow_model_stack"]["items"]["type"] == "string"
    assert out["properties"]["edit_decisions"]["type"] == "string"
    assert out["properties"]["const_version"]["enum"] == ["1.0"]
    assert out["properties"]["fps"]["enum"] == ["24", "30", "60"]
    assert "exclusiveMinimum" not in out["properties"]["crf"]
    assert "minimum" not in out["properties"]["crf"]
    decls = function_declarations([{"name": "image_selector", "parameters": raw}])
    _assert_gemini_schema(decls[0]["parameters"])


def test_all_yaml_stage_tools_are_gemini_safe() -> None:
    from runner.llm_gemini import function_declarations
    from runner.om_agent import stage_tools
    from runner.pipeline import load, stages
    from runner.tools_exec import tool_schemas

    for name in PIPELINES:
        manifest = load(name)
        for stage in stages(manifest):
            decls = function_declarations(tool_schemas(stage_tools(manifest, stage)))
            for decl in decls:
                _assert_gemini_schema(decl["parameters"])


def test_http_error_detail_preserves_gemini_body() -> None:
    from io import BytesIO
    from urllib.error import HTTPError

    from runner.llm_gemini import _http_body
    from runner.telemetry import http_error_detail

    payload = b'{"error":{"message":"Invalid function_declarations: items must be specified"}}'
    exc = HTTPError("https://example.invalid", 400, "Bad Request", hdrs=None, fp=BytesIO(payload))
    body = _http_body(exc)
    assert "function_declarations" in body
    status, msg = http_error_detail(exc)
    assert status == 400
    assert "function_declarations" in msg


def test_layer3_omits_files_over_api_budget() -> None:
    from runner import skills

    blob = skills.load_layer3(
        ["elevenlabs", "manimce-best-practices", "beautiful-mermaid", "music"],
        budget=400,
    )
    assert "Layer 3 omitted" in blob


def test_coerce_tool_inputs_parses_json_object_strings() -> None:
    from types import SimpleNamespace

    from runner.tools_exec import coerce_tool_inputs

    tool = SimpleNamespace(
        input_schema={
            "type": "object",
            "properties": {"edit_decisions": {"type": "object"}, "cuts": {"type": "array", "items": {"type": "object"}}},
        }
    )
    out = coerce_tool_inputs(tool, {"edit_decisions": '{"version":"1.0"}', "cuts": ['{"id":"c1"}']})
    assert out["edit_decisions"] == {"version": "1.0"}
    assert out["cuts"] == [{"id": "c1"}]

    numeric = SimpleNamespace(
        input_schema={
            "type": "object",
            "properties": {
                "fps": {"type": "integer", "enum": [24, 30, 60]},
                "price": {"type": "number"},
                "strict": {"type": "boolean"},
            },
        }
    )
    scalars = coerce_tool_inputs(numeric, {"fps": "24", "price": "1.5", "strict": "false"})
    assert scalars == {"fps": 24, "price": 1.5, "strict": False}


def test_atelier_still_targets_job_workspace(tmp_path, monkeypatch) -> None:
    import subprocess

    from runner.tools_exec import execute, registry
    from runner.workspace_root import set_root

    registry()
    set_root(tmp_path)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append([str(c) for c in cmd])

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        (tmp_path / "snapshots").mkdir(parents=True, exist_ok=True)
        (tmp_path / "snapshots" / "sc1.png").write_bytes(b"png")
        return Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = execute("atelier_still", {"composition_id": "Scene"})
    assert result.success, result.error
    assert captured
    assert "--project-dir" in captured[0]
    assert str(tmp_path) in captured[0]
    assert str(tmp_path / "snapshots" / "sc1.png") in (result.artifacts or [])


def test_saas_atelier_is_not_flash_lite() -> None:
    from runner.config import llm_role

    role = llm_role("atelier")
    assert "lite" not in role["model"]
    planner = llm_role("planner")
    assert planner["model"] == "gemini-3.5-flash-lite"


def test_gemini_posts_any_tool_config(monkeypatch) -> None:
    import json
    from urllib.request import Request

    from runner.llm_gemini import GeminiFlash

    monkeypatch.setenv("STUDIO_GEMINI_RATE_LIMIT", "0")
    captured: dict[str, object] = {}

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(req: Request, timeout: int = 0):
        captured["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr("runner.llm_gemini.urlopen", fake_urlopen)
    client = GeminiFlash(api_key="k", model="gemini-2.0-flash")
    client._post(
        "s",
        [{"role": "user", "parts": [{"text": "hi"}]}],
        [{"name": "video_compose", "parameters": {"type": "object", "properties": {}}}],
        function_calling_mode="ANY",
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"


def test_gemini_continues_until_tools_write_files() -> None:
    from runner.llm_gemini import GeminiFlash
    from tools.base_tool import ToolResult

    seq = [
        {"candidates": [{"content": {"parts": [{"text": '{"version":"1.0"}'}]}}]},
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"functionCall": {"name": "video_compose", "args": {"output_path": "renders/final.mp4"}, "id": "c1"}}
                        ]
                    }
                }
            ]
        },
        {"candidates": [{"content": {"parts": [{"text": '{"version":"1.0","ok":true}'}]}}]},
    ]
    modes: list[str | None] = []

    class Client(GeminiFlash):
        def _generate(self, system, contents, decls, *, purpose="run_stage", function_calling_mode=None):
            modes.append(function_calling_mode)
            return seq.pop(0)

    called: list[str] = []

    def on_tool(name, _args):
        called.append(name)
        return ToolResult(success=True, data={"ok": True})

    def on_artifact(raw):
        if not raw.get("ok"):
            return "claimed files missing from disk — call YAML tools"
        return None

    client = Client(api_key="k", model="gemini-3.5-flash")
    turn = client.run_stage(
        system="s",
        user="compose",
        tools=[{"name": "video_compose", "parameters": {"type": "object", "properties": {}}}],
        retry=0,
        on_tool=on_tool,
        on_artifact=on_artifact,
        force_tools=True,
        max_rounds=8,
    )
    assert modes[0] == "ANY"
    assert "video_compose" in called
    assert turn.artifact == {"version": "1.0", "ok": True}
