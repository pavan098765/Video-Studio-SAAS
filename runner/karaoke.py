"""Normalize TTS timestamp payloads into word rows for Explainer karaoke."""

from __future__ import annotations

from typing import Any


def audio_path(data: dict[str, Any] | None, fallback: str | None = None) -> str:
    if not isinstance(data, dict):
        return str(fallback or "")
    for key in ("output_path", "path", "output"):
        value = data.get(key)
        if value:
            return str(value)
    return str(fallback or "")


def words_from_alignment(alignment: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(alignment, dict):
        return []
    chars = alignment.get("characters") or alignment.get("chars") or []
    starts = alignment.get("character_start_times_seconds") or alignment.get("characterStartTimesSeconds") or []
    ends = alignment.get("character_end_times_seconds") or alignment.get("characterEndTimesSeconds") or []
    if not chars:
        return []
    words: list[dict[str, Any]] = []
    buf = ""
    start: float | None = None
    end = 0.0
    for i, raw in enumerate(chars):
        ch = str(raw)
        s = float(starts[i]) if i < len(starts) else end
        e = float(ends[i]) if i < len(ends) else s
        if ch.isspace():
            if buf.strip():
                words.append({"word": buf.strip(), "start": float(start if start is not None else s), "end": float(end)})
            buf = ""
            start = None
            end = e
            continue
        if start is None:
            start = s
        buf += ch
        end = e
    if buf.strip():
        words.append({"word": buf.strip(), "start": float(start if start is not None else 0.0), "end": float(end)})
    return words


def normalize_words(payload: Any) -> list[dict[str, Any]]:
    """Accept ElevenLabs, fal, or already-normalized word lists."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        if payload.get("characters") or payload.get("character_start_times_seconds"):
            return words_from_alignment(payload)
        inner = payload.get("words") or payload.get("timestamps") or payload.get("word_timestamps") or payload.get("alignment")
        if inner is not None and inner is not payload:
            return normalize_words(inner)
        word = str(payload.get("word") or payload.get("text") or payload.get("token") or "").strip()
        if word:
            return [payload]
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for row in payload:
        if isinstance(row, str):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("characters") or row.get("character_start_times_seconds"):
            out.extend(words_from_alignment(row))
            continue
        nested = row.get("words") or row.get("timestamps")
        if isinstance(nested, list) and not (row.get("word") or row.get("text") or row.get("token")):
            out.extend(normalize_words(nested))
            continue
        word = str(row.get("word") or row.get("text") or row.get("token") or "").strip()
        if not word:
            continue
        out.append(row)
    return out


def extract_words(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("timestamps", "word_timestamps", "words", "alignment", "normalized_alignment"):
        words = normalize_words(data.get(key))
        if words:
            return words
    return []
