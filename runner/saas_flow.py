"""SaaS helpers for hydrating research URLs. Live jobs search via web_search tools."""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse

from runner import tools_exec

_MAX_HITS = 24
_FETCH = 4
_MIN_SOURCES = 5

_NARRATIVE = {
    "analogy",
    "problem_solution",
    "journey",
    "debate",
    "myth_busting",
    "timeline",
    "comparison",
    "tutorial",
    "story",
    "data_narrative",
}
_RENDER_RUNTIMES = {"remotion", "hyperframes", "ffmpeg"}
_RENDERER_FAMILIES = {
    "explainer-data",
    "explainer-teacher",
    "cinematic-trailer",
    "documentary-montage",
    "product-reveal",
    "screen-demo",
    "presenter",
    "animation-first",
}
_COMPOSITION_MODES = {"templated", "atelier"}
_CONCEPT_KEYS = {
    "id",
    "title",
    "hook",
    "narrative_structure",
    "visual_approach",
    "suggested_playbook",
    "target_audience",
    "target_platform",
    "target_duration_seconds",
    "key_points",
    "core_message",
    "cta",
    "tone",
    "grounded_in",
    "why_this_works",
}
_PLATFORMS = {"youtube", "instagram", "tiktok", "linkedin", "generic"}


def research_queries(topic: str, pipeline: str = "animated-explainer") -> list[tuple[str, str]]:
    """Compressed Research Director batches (landscape, trending, data, audience, visual)."""
    year = date.today().year
    month = date.today().strftime("%B")
    t = (topic or "topic").strip() or "topic"
    if pipeline == "cinematic":
        return [
            ("landscape", f"{t} cinematic trailer OR brand film"),
            ("landscape", f"{t} color grading cinematography reference"),
            ("landscape", f"{t} {month} {year} trailer OR teaser"),
            ("trending", f"{t} campaign OR launch {year}"),
            ("data_points", f"{t} visual style breakdown making-of"),
            ("data_points", f"{t} cinematography (shot list OR color palette)"),
            ("audience_insights", f"{t} film audience reception"),
            ("audience_insights", f"{t} site:reddit.com trailer OR brand film"),
            ("visual", f"{t} mood board cinematography"),
            ("visual", f"{t} (explainer OR animation OR infographic) cinematic"),
        ]
    if pipeline == "character-animation":
        return [
            ("landscape", f"{t} character animation explained"),
            ("landscape", f"{t} 2d character rig {year}"),
            ("data_points", f"{t} walk cycle OR rig animation"),
            ("data_points", f"{t} squash stretch animation principles"),
            ("audience_insights", f"{t} animation beginner questions"),
            ("audience_insights", f"{t} (common mistakes OR misconceptions) animation"),
            ("trending", f"{t} character animation {year}"),
            ("visual", f"{t} 2d vs 3d animation"),
            ("visual", f"{t} (walk cycle OR blink OR head turn) reference"),
            ("landscape", f"{t} {year}"),
        ]
    queries = [
        ("landscape", f"{t} explained site:youtube.com"),
        ("landscape", f"{t} (guide OR tutorial OR explained OR breakdown) -site:youtube.com"),
        ("landscape", f"{t} {month} {year}"),
        ("trending", f"{t} (announcement OR launch OR update OR controversy) {year}"),
        ("trending", f"{t} site:reddit.com"),
        ("trending", f"{t} site:news.ycombinator.com"),
        ("data_points", f"{t} statistics {year}"),
        ("data_points", f"{t} (study OR research OR survey OR report) {year}"),
        ("data_points", f'{t} (surprisingly OR counterintuitively OR "most people don\'t know")'),
        ("audience_insights", f"{t} site:reddit.com (help OR confused OR ELI5 OR \"why does\")"),
        ("audience_insights", f"{t} (common mistakes OR myths OR misconceptions)"),
        ("audience_insights", f"why is {t} so (hard OR confusing OR important)"),
        ("visual", f"{t} (explainer OR animation OR infographic OR diagram)"),
    ]
    if pipeline == "animation":
        queries.append(("visual", f"{t} (manim OR motion graphics OR kinetic typography)"))
    return queries


def gather_research(topic: str, pipeline: str = "animated-explainer") -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    queries = research_queries(topic, pipeline)
    from runner import progress

    progress.note(f"research corpus {len(queries)} Brave queries for {topic!r}", kind="research")
    for qi, (used_for, query) in enumerate(queries, start=1):
        progress.note(f"search {qi}/{len(queries)} [{used_for}] {query}", kind="research")
        result = tools_exec.execute("web_search", {"query": query, "count": 5})
        rows = (result.data or {}).get("results") if result.success else None
        for row in rows or []:
            url = str((row or {}).get("url") or "").strip()
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            hits.append(
                {
                    "id": len(hits),
                    "url": url,
                    "title": str(row.get("title") or url)[:200],
                    "snippet": str(row.get("snippet") or "")[:400],
                    "used_for": used_for,
                    "query": query,
                }
            )
            if len(hits) >= _MAX_HITS:
                break
        if len(hits) >= _MAX_HITS:
            break
    tools_exec.remember_urls([h["url"] for h in hits])
    fetched: list[dict[str, Any]] = []
    targets = _fetch_targets(hits)
    progress.note(f"fetch {len(targets)} pages ({len(hits)} unique hits)", kind="research")
    for fi, hit in enumerate(targets, start=1):
        progress.note(f"fetch {fi}/{len(targets)} {hit['url']}", kind="research")
        page = tools_exec.execute("web_fetch", {"url": hit["url"]})
        if not page.success or not isinstance(page.data, dict):
            continue
        text = str(page.data.get("text") or "")[:2500]
        if text:
            hit["excerpt"] = text
            fetched.append({"id": hit["id"], "url": hit["url"], "title": page.data.get("title") or hit["title"]})
    return {"topic": topic, "hits": hits, "fetched": fetched}


def corpus_for_llm(corpus: dict[str, Any]) -> dict[str, Any]:
    hits = []
    for hit in corpus.get("hits") or []:
        hits.append(
            {
                "id": hit["id"],
                "url": hit["url"],
                "title": hit["title"],
                "snippet": hit.get("excerpt") or hit.get("snippet") or "",
                "used_for": hit.get("used_for"),
                "query": hit.get("query"),
            }
        )
    return {
        "topic": corpus.get("topic"),
        "instruction": (
            "Python already ran the Research Director search batches "
            "(landscape, trending, data, audience, visual). "
            "Analyze this corpus. Cite hits by id (source_id). Do not invent URLs. "
            "Do not call web_search. Python will write sources[] and stamp every URL."
        ),
        "hits": hits,
    }


def hydrate_research(artifact: dict[str, Any] | None, corpus: dict[str, Any]) -> dict[str, Any]:
    hits: list[dict[str, Any]] = list(corpus.get("hits") or [])
    topic = str(corpus.get("topic") or (artifact or {}).get("topic") or "topic")
    art = dict(artifact or {}) if isinstance(artifact, dict) else {}
    urls = [h["url"] for h in hits if h.get("url")]
    if not urls:
        raise ValueError("research corpus has no URLs — cannot hydrate research_brief")
    by_id = {int(h["id"]): h for h in hits if "id" in h}

    def stamp(url: Any = None, source_id: Any = None, prefer: str = "data_points") -> str:
        if isinstance(source_id, int) and source_id in by_id:
            return str(by_id[source_id]["url"])
        raw = str(url or "").rstrip("/")
        for hit in hits:
            hay = str(hit["url"]).rstrip("/")
            if raw and (raw in hay or hay in raw):
                return hit["url"]
        pool = [h for h in hits if h.get("used_for") == prefer] or hits
        return str(pool[0]["url"])

    sources = []
    seen_src: set[str] = set()
    for hit in hits:
        url = hit["url"]
        if url in seen_src:
            continue
        seen_src.add(url)
        sources.append(
            {
                "url": url,
                "title": hit.get("title") or urlparse(url).netloc or "Source",
                "used_for": hit.get("used_for") or "data_points",
                "reliability": "secondary",
            }
        )

    points = []
    for row in art.get("data_points") or []:
        if not isinstance(row, dict) or not row.get("claim"):
            continue
        item = {
            "claim": str(row["claim"]),
            "source_url": stamp(row.get("source_url"), row.get("source_id"), "data_points"),
            "credibility": row["credibility"]
            if row.get("credibility") in {"primary_source", "secondary_source", "anecdotal"}
            else "secondary_source",
        }
        if row.get("source_name"):
            item["source_name"] = str(row["source_name"])[:120]
        if row.get("surprise_factor") in {"expected", "notable", "surprising", "counterintuitive"}:
            item["surprise_factor"] = row["surprise_factor"]
        if row.get("usable_as"):
            item["usable_as"] = str(row["usable_as"])[:80]
        points.append(item)
    if len(points) < 3:
        for hit in [h for h in hits if h.get("used_for") == "data_points"] or hits:
            if len(points) >= 5:
                break
            claim = (hit.get("excerpt") or hit.get("snippet") or hit.get("title") or "").strip()
            if not claim:
                continue
            points.append({"claim": claim[:400], "source_url": hit["url"], "credibility": "secondary_source"})

    landscape_hits = [h for h in hits if h.get("used_for") == "landscape"] or hits
    existing = []
    for row in (art.get("landscape") or {}).get("existing_content") or []:
        if not isinstance(row, dict):
            continue
        existing.append(
            {
                "title": str(row.get("title") or "Existing piece")[:200],
                "url": stamp(row.get("url"), row.get("source_id"), "landscape"),
                "source": str(row.get("source") or "web"),
                "angle": str(row.get("angle") or "overview"),
                "what_it_covers": str(row.get("what_it_covers") or row.get("title") or "Coverage from cited source"),
                **({"what_it_misses": str(row["what_it_misses"])} if row.get("what_it_misses") else {}),
                **({"engagement_signal": str(row["engagement_signal"])} if row.get("engagement_signal") else {}),
            }
        )
    listed = {str(row.get("url") or "") for row in existing}
    for hit in landscape_hits + hits:
        if len(existing) >= 5:
            break
        url = hit["url"]
        if url in listed:
            continue
        listed.add(url)
        existing.append(
            {
                "title": hit.get("title") or topic,
                "url": url,
                "source": _source_kind(url),
                "angle": "overview" if hit.get("used_for") == "landscape" else str(hit.get("used_for") or "overview"),
                "what_it_covers": (hit.get("snippet") or hit.get("title") or topic)[:300],
            }
        )

    insights = art.get("audience_insights") if isinstance(art.get("audience_insights"), dict) else {}
    questions = [str(q) for q in (insights.get("common_questions") or []) if str(q).strip()]
    if len(questions) < 3:
        questions.extend(_questions_from_hits(hits, topic))
    myths = []
    for row in insights.get("misconceptions") or []:
        if isinstance(row, dict) and row.get("myth") and row.get("reality"):
            item = {"myth": str(row["myth"]), "reality": str(row["reality"])}
            if row.get("source"):
                item["source"] = str(row["source"])
            myths.append(item)
    if not myths:
        myths = _myths_from_hits(hits, topic)

    angles = []
    for row in art.get("angles_discovered") or []:
        if not isinstance(row, dict) or not row.get("name") or not row.get("hook"):
            continue
        kind = row.get("type") if row.get("type") in {"trending", "evergreen", "contrarian", "narrative", "data_driven"} else "evergreen"
        item = {
            "name": str(row["name"])[:80],
            "hook": str(row["hook"]),
            "type": kind,
            "why_now": str(row.get("why_now") or _why_now_from_hits(hits, kind)),
        }
        if isinstance(row.get("grounded_in"), list) and row["grounded_in"]:
            item["grounded_in"] = [str(x) for x in row["grounded_in"][:8]]
        angles.append(item)
    for row in _angles_from_hits(hits, topic):
        if len(angles) >= 3:
            break
        if any(a["name"] == row["name"] for a in angles):
            continue
        angles.append(row)

    landscape_in = art.get("landscape") if isinstance(art.get("landscape"), dict) else {}
    saturated = [str(x) for x in (landscape_in.get("saturated_angles") or []) if str(x).strip()]
    if not saturated and landscape_hits:
        saturated = [f"Generic overviews in the vein of {(landscape_hits[0].get('title') or topic)}"]
    gaps = [str(x) for x in (landscape_in.get("underserved_gaps") or []) if str(x).strip()]
    if not gaps:
        aud_q = next((h.get("query") for h in hits if h.get("used_for") == "audience_insights" and h.get("query")), None)
        gaps = [aud_q or "Mechanism-first explanation with sourced misconceptions"]

    summary = str(art.get("research_summary") or "").strip()
    if not summary:
        lead = next((h for h in hits if h.get("used_for") == "data_points"), hits[0])
        summary = (
            f"{(lead.get('snippet') or lead.get('title') or topic).strip()} "
            f"Live sources: {len(hits)} hits across landscape, trending, data, and audience batches."
        )

    out: dict[str, Any] = {
        "version": "1.0",
        "topic": topic,
        "research_date": str(art.get("research_date") or date.today().isoformat())[:10],
        "landscape": {
            "existing_content": existing[:12],
            "saturated_angles": saturated[:8],
            "underserved_gaps": gaps[:8],
        },
        "data_points": points[:8],
        "audience_insights": {
            "common_questions": questions[:8],
            "misconceptions": myths[:6],
            "knowledge_level": str(insights.get("knowledge_level") or "curious beginner"),
            **(
                {"pain_points": [str(p) for p in insights["pain_points"][:6]]}
                if isinstance(insights.get("pain_points"), list) and insights["pain_points"]
                else {}
            ),
        },
        "angles_discovered": angles[:6],
        "sources": sources[:12],
        "research_summary": summary,
    }
    trending = _trending_block(art.get("trending") if isinstance(art.get("trending"), dict) else {}, hits, stamp)
    if trending:
        out["trending"] = trending
    visuals = _visual_block(art.get("visual_references") if isinstance(art.get("visual_references"), list) else [], hits, stamp)
    if visuals:
        out["visual_references"] = visuals
    experts = _expert_block(art.get("expert_voices") if isinstance(art.get("expert_voices"), list) else [], stamp)
    if experts:
        out["expert_voices"] = experts
    return out


def hydrate_proposal(
    artifact: dict[str, Any] | None,
    *,
    pipeline: str,
    render_runtime: str,
    renderer_family: str,
    composition_mode: str,
    topic: str,
    duration_seconds: int,
) -> dict[str, Any]:
    """Lock runtime/approval/cost. Do not invent concept pitches — the model writes those."""
    art = dict(artifact or {}) if isinstance(artifact, dict) else {}
    concepts = [c for c in (art.get("concept_options") or []) if isinstance(c, dict)]
    cleaned: list[dict[str, Any]] = []
    for i, row in enumerate(concepts, start=1):
        item = {k: row[k] for k in _CONCEPT_KEYS if k in row}
        item["id"] = str(item.get("id") or f"c{i}")
        if "target_duration_seconds" not in item:
            item["target_duration_seconds"] = duration_seconds
        if item.get("narrative_structure") not in _NARRATIVE:
            item.pop("narrative_structure", None)
        if item.get("target_platform") not in _PLATFORMS:
            item.pop("target_platform", None)
        if "key_points" in item and (not isinstance(item["key_points"], list) or len(item["key_points"]) < 2):
            item.pop("key_points", None)
        cleaned.append(item)
    selected = art.get("selected_concept") if isinstance(art.get("selected_concept"), dict) else {}
    ids = {c["id"] for c in cleaned}
    concept_id = str(selected.get("concept_id") or (cleaned[0]["id"] if cleaned else "c1"))
    if ids and concept_id not in ids:
        concept_id = cleaned[0]["id"]
    prior = art.get("production_plan") if isinstance(art.get("production_plan"), dict) else {}
    stages = [
        {
            "stage": "assets",
            "tools": [{"tool_name": "tts_selector", "role": "narration", "available": True}],
            "approach": "Generate narration and picture",
        }
    ]
    cost = art.get("cost_estimate") if isinstance(art.get("cost_estimate"), dict) else {}
    line_items = []
    for row in cost.get("line_items") or []:
        if isinstance(row, dict) and row.get("tool"):
            line_items.append(
                {
                    "tool": str(row["tool"]),
                    "operation": str(row.get("operation") or "run"),
                    "estimated_usd": float(row.get("estimated_usd") or 0),
                }
            )
    if not line_items:
        line_items = [{"tool": "tts_selector", "operation": "speak", "estimated_usd": 0}]
    verdict = cost.get("budget_verdict")
    if verdict not in {"within_budget", "near_limit", "over_budget", "no_budget_set"}:
        verdict = "no_budget_set"
    approval: dict[str, Any] = {"status": "approved"}
    raw_approval = art.get("approval") if isinstance(art.get("approval"), dict) else {}
    status = str(raw_approval.get("status") or "")
    if status in {"approved", "approved_with_changes", "rejected"}:
        approval["status"] = status
    notes = str(raw_approval.get("user_notes") or "").strip()
    if notes:
        approval["user_notes"] = notes[:2000]
    elif approval["status"] == "approved" and not notes:
        approval["user_notes"] = "API job: EP auto-approved after director pass (no IDE human gate)."
    if isinstance(raw_approval.get("approved_budget_usd"), (int, float)):
        approval["approved_budget_usd"] = float(raw_approval["approved_budget_usd"])
    family = renderer_family if renderer_family in _RENDERER_FAMILIES else "explainer-data"
    plan: dict[str, Any] = {
        "pipeline": pipeline,
        "playbook": str(prior.get("playbook") or "clean-professional"),
        "stages": stages,
        "render_runtime": render_runtime if render_runtime in _RENDER_RUNTIMES else "remotion",
        "renderer_family": family,
        "composition_mode": composition_mode if composition_mode in _COMPOSITION_MODES else "atelier",
    }
    if prior.get("art_direction"):
        plan["art_direction"] = str(prior["art_direction"])
    return {
        "version": "1.0",
        "concept_options": cleaned[:6],
        "selected_concept": {
            "concept_id": concept_id,
            "rationale": str(selected.get("rationale") or "Strongest research-backed option for this job."),
        },
        "production_plan": plan,
        "cost_estimate": {
            "total_estimated_usd": float(cost.get("total_estimated_usd") or 0),
            "line_items": line_items,
            "budget_verdict": verdict,
        },
        "approval": approval,
    }


def _fetchable(url: str) -> bool:
    """Skip hosts that bot-block HTML GET (YouTube 429, Reddit/HN 403). Snippets stay in the corpus."""
    host = urlparse(url).netloc.lower()
    blocked = (
        "youtube.com",
        "youtu.be",
        "reddit.com",
        "redd.it",
        "news.ycombinator.com",
        "ycombinator.com",
        "ck12.org",
        "the-scientist.com",
    )
    return bool(url.startswith("http")) and not any(token in host for token in blocked)


def _fetch_targets(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    seen_for: set[str] = set()
    for kind in ("data_points", "audience_insights", "landscape", "trending", "visual"):
        for hit in hits:
            if hit.get("used_for") == kind and kind not in seen_for and _fetchable(str(hit.get("url") or "")):
                picked.append(hit)
                seen_for.add(kind)
                break
        if len(picked) >= _FETCH:
            return picked[:_FETCH]
    for hit in hits:
        if len(picked) >= _FETCH:
            break
        if hit not in picked and _fetchable(str(hit.get("url") or "")):
            picked.append(hit)
    return picked[:_FETCH]


def _source_kind(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "youtube" in host:
        return "youtube"
    if "reddit" in host:
        return "reddit"
    if "ycombinator" in host or host.endswith("news.ycombinator.com"):
        return "hackernews"
    return "web"


def _questions_from_hits(hits: list[dict[str, Any]], _topic: str) -> list[str]:
    questions: list[str] = []
    for hit in hits:
        if hit.get("used_for") not in {"audience_insights", "trending"}:
            continue
        title = str(hit.get("title") or "").strip()
        snippet = str(hit.get("snippet") or "").strip()
        for text in (title, snippet):
            if "?" in text:
                q = text.split("?")[0].strip() + "?"
                if 12 <= len(q) <= 180 and q not in questions:
                    questions.append(q)
        if len(questions) >= 8:
            break
    if len(questions) < 3:
        for hit in hits:
            if hit.get("used_for") != "audience_insights":
                continue
            title = str(hit.get("title") or "").strip()
            if title and title not in questions:
                questions.append(title[:180])
            if len(questions) >= 3:
                break
    if len(questions) < 3:
        for hit in hits:
            title = str(hit.get("title") or "").strip()
            if title and title not in questions:
                questions.append(title[:180])
            if len(questions) >= 3:
                break
    return questions


def _myths_from_hits(hits: list[dict[str, Any]], topic: str) -> list[dict[str, str]]:
    myths: list[dict[str, str]] = []
    for hit in hits:
        blob = f"{hit.get('title') or ''} {hit.get('snippet') or ''}".lower()
        if not any(w in blob for w in ("myth", "misconception", "wrong", "actually", "don't", "not true")):
            continue
        myths.append(
            {
                "myth": str(hit.get("title") or f"A common take on {topic}")[:200],
                "reality": str(hit.get("snippet") or hit.get("excerpt") or hit.get("title") or topic)[:400],
                "source": hit["url"],
            }
        )
        if len(myths) >= 2:
            break
    if not myths:
        aud = next((h for h in hits if h.get("used_for") == "audience_insights"), hits[0])
        myths = [
            {
                "myth": str(aud.get("title") or f"The usual {topic} explanation is complete"),
                "reality": str(aud.get("snippet") or aud.get("excerpt") or aud.get("title") or topic)[:400],
                "source": aud["url"],
            }
        ]
    return myths


def _why_now_from_hits(hits: list[dict[str, Any]], kind: str) -> str:
    prefer = "trending" if kind == "trending" else "data_points" if kind == "data_driven" else "audience_insights"
    hit = next((h for h in hits if h.get("used_for") == prefer), hits[0] if hits else None)
    if not hit:
        return "Grounded in the live search corpus"
    return f"Grounded in: {hit.get('title') or hit['url']}"


def _angles_from_hits(hits: list[dict[str, Any]], topic: str) -> list[dict[str, Any]]:
    def pack(hit: dict[str, Any], kind: str) -> dict[str, Any]:
        title = str(hit.get("title") or topic)[:80]
        hook = str(hit.get("snippet") or hit.get("title") or topic)[:240]
        return {
            "name": title,
            "hook": hook,
            "type": kind,
            "why_now": f"From live search: {hit.get('title') or hit['url']}",
            "grounded_in": [hit["url"]],
        }

    out: list[dict[str, Any]] = []
    data = next((h for h in hits if h.get("used_for") == "data_points"), None)
    trend = next((h for h in hits if h.get("used_for") == "trending"), None)
    aud = next((h for h in hits if h.get("used_for") == "audience_insights"), None)
    land = next((h for h in hits if h.get("used_for") == "landscape"), None)
    if data:
        out.append(pack(data, "data_driven"))
    if aud:
        out.append(pack(aud, "contrarian"))
    if trend:
        out.append(pack(trend, "trending"))
    elif land:
        out.append(pack(land, "evergreen"))
    if len(out) < 3:
        for hit in hits:
            if len(out) >= 3:
                break
            if any(a["grounded_in"][0] == hit["url"] for a in out if a.get("grounded_in")):
                continue
            out.append(pack(hit, "evergreen"))
    return out[:3]


def _trending_block(raw: dict[str, Any], hits: list[dict[str, Any]], stamp) -> dict[str, Any] | None:
    developments = []
    for row in raw.get("recent_developments") or []:
        if not isinstance(row, dict) or not row.get("headline"):
            continue
        developments.append(
            {
                "headline": str(row["headline"]),
                "date": str(row.get("date") or date.today().isoformat()[:7]),
                "relevance": str(row.get("relevance") or "Timely hook for the video"),
                **({"url": stamp(row.get("url"), row.get("source_id"), "trending")} if row.get("url") or row.get("source_id") is not None else {}),
            }
        )
    for hit in [h for h in hits if h.get("used_for") == "trending"]:
        if len(developments) >= 3:
            break
        developments.append(
            {
                "headline": hit.get("title") or hit["url"],
                "url": hit["url"],
                "date": str(date.today().year),
                "relevance": (hit.get("snippet") or "Recent coverage")[:240],
            }
        )
    discussions = []
    for row in raw.get("active_discussions") or []:
        if isinstance(row, dict) and row.get("platform") and row.get("topic_or_url") and row.get("sentiment"):
            item = {
                "platform": str(row["platform"]),
                "topic_or_url": str(row["topic_or_url"]),
                "sentiment": str(row["sentiment"]),
            }
            if isinstance(row.get("key_quotes"), list) and row["key_quotes"]:
                item["key_quotes"] = [str(q) for q in row["key_quotes"][:4]]
            discussions.append(item)
    window = raw.get("timeliness_window") if isinstance(raw.get("timeliness_window"), str) else None
    if not developments and not discussions and not window:
        return None
    block: dict[str, Any] = {}
    if developments:
        block["recent_developments"] = developments[:6]
    if discussions:
        block["active_discussions"] = discussions[:6]
    block["timeliness_window"] = window or ("weeks" if developments else "evergreen")
    return block


def _visual_block(raw: list[Any], hits: list[dict[str, Any]], stamp) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        item: dict[str, Any] = {}
        if row.get("description"):
            item["description"] = str(row["description"])
        if row.get("what_works"):
            item["what_works"] = str(row["what_works"])
        if row.get("url") or row.get("source_id") is not None:
            item["url"] = stamp(row.get("url"), row.get("source_id"), "visual")
        if item:
            out.append(item)
    for hit in [h for h in hits if h.get("used_for") == "visual"]:
        if len(out) >= 3:
            break
        out.append(
            {
                "description": hit.get("title") or hit["url"],
                "url": hit["url"],
                "what_works": (hit.get("snippet") or "Visual treatment found in landscape scan")[:240],
            }
        )
    return out[:6]


def _expert_block(raw: list[Any], stamp) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict) or not row.get("name") or not row.get("position"):
            continue
        item = {"name": str(row["name"]), "position": str(row["position"])}
        if row.get("title_or_affiliation"):
            item["title_or_affiliation"] = str(row["title_or_affiliation"])
        if row.get("source_url") or row.get("source_id") is not None:
            item["source_url"] = stamp(row.get("source_url"), row.get("source_id"), "data_points")
        if isinstance(row.get("contrarian"), bool):
            item["contrarian"] = row["contrarian"]
        out.append(item)
    return out[:6]
