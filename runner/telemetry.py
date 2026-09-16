"""Job telemetry: LLM tokens, step timings, tool calls, flags. No-op if not attached."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError

_state: ContextVar["JobTelemetry | None"] = ContextVar("studio_job_telemetry", default=None)


def usage_from_payload(payload: dict[str, Any] | None) -> dict[str, int]:
    """Normalize Gemini / OpenAI / Anthropic usage blobs to input/output/total tokens."""
    empty = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if not isinstance(payload, dict):
        return dict(empty)
    meta = payload.get("usageMetadata") or payload.get("usage_metadata")
    if isinstance(meta, dict) and meta:
        inp = int(meta.get("promptTokenCount") or meta.get("prompt_token_count") or 0)
        out = int(meta.get("candidatesTokenCount") or meta.get("candidates_token_count") or 0)
        thoughts = int(meta.get("thoughtsTokenCount") or meta.get("thoughts_token_count") or 0)
        total = int(meta.get("totalTokenCount") or meta.get("total_token_count") or 0)
        if not total:
            total = inp + out + thoughts
        return {"input_tokens": inp, "output_tokens": out + thoughts, "total_tokens": total}
    usage = payload.get("usage")
    if isinstance(usage, dict) and usage:
        inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        total = int(usage.get("total_tokens") or 0)
        if not total:
            total = inp + out + cache_read + cache_create
        return {"input_tokens": inp + cache_read + cache_create, "output_tokens": out, "total_tokens": total}
    return dict(empty)


@dataclass
class JobTelemetry:
    job_id: str = ""
    pipeline: str = ""
    work_dir: str = ""
    started_at: str = ""
    started_mono: float = 0.0
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    _span_stack: list[tuple[str, float]] = field(default_factory=list)
    _role: str = ""

    def snapshot(self) -> dict[str, Any]:
        elapsed_ms = int((time.perf_counter() - self.started_mono) * 1000) if self.started_mono else 0
        llm_in = sum(int(c.get("input_tokens") or 0) for c in self.llm_calls)
        llm_out = sum(int(c.get("output_tokens") or 0) for c in self.llm_calls)
        llm_total = sum(int(c.get("total_tokens") or 0) for c in self.llm_calls)
        llm_ms = sum(int(c.get("elapsed_ms") or 0) for c in self.llm_calls)
        llm_fail = sum(1 for c in self.llm_calls if not c.get("ok", True))
        by_model: dict[str, dict[str, int]] = {}
        for call in self.llm_calls:
            key = f"{call.get('provider') or '?'}:{call.get('model') or '?'}"
            row = by_model.setdefault(
                key,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "elapsed_ms": 0, "errors": 0},
            )
            row["calls"] += 1
            row["input_tokens"] += int(call.get("input_tokens") or 0)
            row["output_tokens"] += int(call.get("output_tokens") or 0)
            row["total_tokens"] += int(call.get("total_tokens") or 0)
            row["elapsed_ms"] += int(call.get("elapsed_ms") or 0)
            if not call.get("ok", True):
                row["errors"] += 1
        tool_fail = sum(1 for t in self.tool_calls if not t.get("ok", True))
        open_spans = [name for name, _ in self._span_stack]
        return {
            "version": "1.0",
            "job_id": self.job_id,
            "pipeline": self.pipeline,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
            "elapsed_seconds": round(elapsed_ms / 1000.0, 3),
            "llm": {
                "calls": len(self.llm_calls),
                "errors": llm_fail,
                "input_tokens": llm_in,
                "output_tokens": llm_out,
                "total_tokens": llm_total,
                "elapsed_ms": llm_ms,
                "by_model": by_model,
            },
            "tools": {
                "calls": len(self.tool_calls),
                "errors": tool_fail,
                "elapsed_ms": sum(int(t.get("elapsed_ms") or 0) for t in self.tool_calls),
            },
            "stages": list(self.stages),
            "open_spans": open_spans,
            "flags": list(self.flags),
            "errors": list(self.errors),
            "meta": dict(self.meta),
            "budget": _budget_snapshot(),
            "llm_calls": list(self.llm_calls),
            "tool_calls": list(self.tool_calls),
        }


def attach(*, job_id: str, pipeline: str, work_dir: Path | None = None) -> JobTelemetry:
    state = JobTelemetry(
        job_id=str(job_id or ""),
        pipeline=str(pipeline or ""),
        work_dir=str(work_dir or ""),
        started_at=datetime.now(timezone.utc).isoformat(),
        started_mono=time.perf_counter(),
    )
    _state.set(state)
    return state


def current() -> JobTelemetry | None:
    return _state.get()


def set_role(role: str) -> None:
    state = current()
    if state is not None:
        state._role = str(role or "")


def set_meta(**values: Any) -> None:
    state = current()
    if state is None:
        return
    for key, value in values.items():
        state.meta[key] = value


def clear() -> None:
    _state.set(None)


def persist(work_dir: Path | None = None) -> Path | None:
    state = current()
    if state is None:
        return None
    root = Path(work_dir or state.work_dir or "")
    if not str(root):
        return None
    dest = root / "project" / "artifacts" / "job_telemetry.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(state.snapshot(), indent=2, default=str), encoding="utf-8")
    return dest


@contextmanager
def span(name: str) -> Iterator[None]:
    from runner import progress

    state = current()
    label = str(name or "span")
    t0 = time.perf_counter()
    progress.note(f"start {label}", kind="stage")
    if state is not None:
        state._span_stack.append((label, t0))
    err: str | None = None
    try:
        yield
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        record_error(label, err)
        raise
    finally:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if state is not None:
            if state._span_stack and state._span_stack[-1][0] == label:
                state._span_stack.pop()
            row = {"name": label, "elapsed_ms": elapsed_ms, "ok": err is None}
            if err:
                row["error"] = err
            state.stages.append(row)
        mark = "FAIL" if err else "ok"
        progress.note(f"{mark} {label} ({elapsed_ms / 1000:.1f}s)", kind="stage")


def record_llm(
    *,
    provider: str,
    model: str,
    purpose: str,
    ok: bool,
    elapsed_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    error: str = "",
    http_status: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    state = current()
    if state is None:
        return
    stage = state._span_stack[-1][0] if state._span_stack else ""
    row: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "role": state._role or "",
        "purpose": purpose,
        "stage": stage,
        "ok": bool(ok),
        "elapsed_ms": int(elapsed_ms),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int(total_tokens or 0),
    }
    if error:
        row["error"] = error[:800]
    if http_status is not None:
        row["http_status"] = http_status
    if extra:
        row["extra"] = extra
    state.llm_calls.append(row)
    from runner import progress

    mark = "ok" if ok else "FAIL"
    progress.note(
        f"{mark} {provider}:{model} {purpose} {elapsed_ms / 1000:.1f}s tokens={int(total_tokens or 0)}"
        + (f" {error[:120]}" if error else ""),
        kind="llm",
    )
    if not ok:
        record_error(stage or purpose or "llm", error or "llm call failed")


def record_tool(
    *,
    name: str,
    ok: bool,
    elapsed_ms: int,
    error: str = "",
    query: Any = None,
) -> None:
    state = current()
    if state is None:
        return
    stage = state._span_stack[-1][0] if state._span_stack else ""
    row: dict[str, Any] = {
        "name": name,
        "ok": bool(ok),
        "elapsed_ms": int(elapsed_ms),
        "stage": stage,
    }
    if query not in {None, ""}:
        row["query"] = str(query)[:240]
    if error:
        row["error"] = str(error)[:800]
    state.tool_calls.append(row)
    from runner import progress

    mark = "ok" if ok else "FAIL"
    extra = f" {row.get('query')}" if row.get("query") else ""
    err = f" {error[:120]}" if error else ""
    progress.note(f"{mark} {name} {elapsed_ms / 1000:.1f}s{extra}{err}", kind="tool")
    if not ok:
        record_error(stage or name, error or f"tool {name} failed")


def record_flag(kind: str, message: str, **extra: Any) -> None:
    state = current()
    if state is None:
        return
    row: dict[str, Any] = {"kind": kind, "message": str(message)}
    row.update(extra)
    state.flags.append(row)
    from runner import progress

    progress.note(f"{kind}: {message}", kind="flag")


def record_error(where: str, message: str) -> None:
    state = current()
    if state is None:
        return
    state.errors.append({"where": str(where), "message": str(message)[:800]})


def timed_llm(*, provider: str, model: str, purpose: str, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Time an LLM HTTP round-trip and record token usage (or the error)."""
    t0 = time.perf_counter()
    try:
        payload = call()
        usage = usage_from_payload(payload if isinstance(payload, dict) else None)
        record_llm(
            provider=provider,
            model=model,
            purpose=purpose,
            ok=True,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            **usage,
        )
        return payload
    except HTTPError as exc:
        status, detail = http_error_detail(exc)
        record_llm(
            provider=provider,
            model=model,
            purpose=purpose,
            ok=False,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            error=detail,
            http_status=status,
        )
        raise
    except Exception as exc:
        record_llm(
            provider=provider,
            model=model,
            purpose=purpose,
            ok=False,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def format_report(snap: dict[str, Any] | None) -> str:
    """Human-readable telemetry block for demo_studio / logs."""
    if not snap:
        return "  telemetry: (none)"
    lines: list[str] = []
    elapsed = snap.get("elapsed_seconds")
    llm = snap.get("llm") or {}
    tools = snap.get("tools") or {}
    budget = snap.get("budget") or {}
    lines.append(
        f"  telemetry  {elapsed}s  llm {llm.get('calls', 0)} calls  "
        f"in={llm.get('input_tokens', 0)} out={llm.get('output_tokens', 0)} "
        f"total={llm.get('total_tokens', 0)}  "
        f"tools {tools.get('calls', 0)} ({tools.get('errors', 0)} failed)"
    )
    spent = budget.get("budget_spent_usd")
    cap = budget.get("budget_total_usd")
    if spent is not None or cap is not None:
        lines.append(f"    budget  ${float(spent or 0):.4f} / ${float(cap or 0):.2f}")
    for stage in snap.get("stages") or []:
        mark = "ok" if stage.get("ok", True) else "FAIL"
        lines.append(f"    stage  {stage.get('name')}  {int(stage.get('elapsed_ms') or 0) / 1000:.2f}s  {mark}")
        if stage.get("error"):
            lines.append(f"           {stage['error'][:200]}")
    by_model = llm.get("by_model") or {}
    for key, row in by_model.items():
        lines.append(
            f"    model  {key}  {row.get('calls', 0)} calls  "
            f"in={row.get('input_tokens', 0)} out={row.get('output_tokens', 0)} "
            f"total={row.get('total_tokens', 0)}  {int(row.get('elapsed_ms') or 0) / 1000:.2f}s"
            + (f"  errors={row.get('errors')}" if row.get("errors") else "")
        )
    for flag in snap.get("flags") or []:
        lines.append(f"    flag   {flag.get('kind')}: {flag.get('message')}")
    for err in snap.get("errors") or []:
        lines.append(f"    error  {err.get('where')}: {err.get('message')}")
    meta = snap.get("meta") or {}
    useful = {k: meta[k] for k in ("composition_mode", "render_runtime", "compose_strategy") if meta.get(k)}
    if useful:
        lines.append("    meta   " + " ".join(f"{k}={v}" for k, v in useful.items()))
    return "\n".join(lines)


def compact(snap: dict[str, Any] | None) -> dict[str, Any]:
    """Summary.json-sized telemetry (no per-call dumps)."""
    if not snap:
        return {}
    llm = snap.get("llm") or {}
    budget = snap.get("budget") or {}
    return {
        "elapsed_seconds": snap.get("elapsed_seconds"),
        "elapsed_ms": snap.get("elapsed_ms"),
        "llm": {
            "calls": llm.get("calls"),
            "errors": llm.get("errors"),
            "input_tokens": llm.get("input_tokens"),
            "output_tokens": llm.get("output_tokens"),
            "total_tokens": llm.get("total_tokens"),
            "elapsed_ms": llm.get("elapsed_ms"),
            "by_model": llm.get("by_model"),
        },
        "tools": snap.get("tools"),
        "stages": [
            {"name": s.get("name"), "elapsed_ms": s.get("elapsed_ms"), "ok": s.get("ok"), "error": s.get("error")}
            for s in snap.get("stages") or []
        ],
        "flags": snap.get("flags"),
        "errors": snap.get("errors"),
        "budget": {
            "budget_spent_usd": budget.get("budget_spent_usd"),
            "budget_total_usd": budget.get("budget_total_usd"),
        },
        "meta": snap.get("meta"),
    }


def _budget_snapshot() -> dict[str, Any] | None:
    try:
        from runner import budget as budget_mod

        return budget_mod.snapshot()
    except Exception:
        return None


def http_error_detail(exc: BaseException) -> tuple[int | None, str]:
    status = getattr(exc, "code", None)
    body = getattr(exc, "_studio_body", None)
    if not isinstance(body, str):
        body = ""
        read = getattr(exc, "read", None)
        if callable(read):
            try:
                raw = read()
                body = raw.decode("utf-8", errors="replace")[:800] if isinstance(raw, (bytes, bytearray)) else str(raw)[:800]
            except Exception:
                body = ""
            try:
                exc._studio_body = body  # type: ignore[attr-defined]
            except Exception:
                pass
    reason = getattr(exc, "reason", "") or str(exc)
    msg = f"{reason}"
    if body:
        msg = f"{msg}: {body}"
    return (int(status) if status is not None else None, msg[:800])
