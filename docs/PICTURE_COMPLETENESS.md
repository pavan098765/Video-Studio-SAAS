# Picture completeness (List B)

**Status:** proposal (background). Locked implementation plan: [`PICTURE_COMPLETENESS_PLAN.md`](PICTURE_COMPLETENESS_PLAN.md).  
**Repo:** `kala-studio-engine` (this tree).  
**Out of scope here:** Kala UI, genedit `/studio/*`, Vultr pooling, List A wiring (already shipped).

This is the follow-up to List A (engine completeness). List A made the runner honest: the right STT is called, jobs ingest `asset_urls`, atelier QA cannot silently become Explainer, regen cannot render blind, karaoke timestamps are opt-in, Motion Canvas does not emit the two-circle fixture by default, Gemini vision actually sends pixels, budget caps are enforced, and `llm.provider` can swap Gemini / OpenAI / Anthropic.

List A does **not** make pictures as good as Cursor + Claude. This document is the plan for that gap.

---

## 1. What “picture completeness” means

A finished Studio job should look like a directed piece, not a catalog of title cards:

| Surface | Cursor + Claude today | Engine after List A |
|---|---|---|
| Diagram / map / math / code | Motion Canvas or atelier scene with real structure | MC only if mermaid/code/tex exists; otherwise skip. Empty mermaid never becomes two circles. Gap: EDL still maps leftover diagram scenes to Explainer `text_card`. |
| Kinetic / brand motion | Multi-turn Remotion/HyperFrames authoring with stills in the loop | One atelier JSON dump, `max_rounds=4`, no tools, skills truncated at 24k tokens |
| Character / HyperFrames | Vision on stills, patch the rig, re-render | One-shot `directors.run_character_chain` |
| Taste / “make sc3 punchier” | Human in Cursor, or a filmstrip + prompt | `regen_prompt` exists on the job; there is no Kala filmstrip loop in this repo |
| Cinematic / avatar | Fail unless video keys exist | Same (honest). Keys are a product/infra problem, not a model problem |

The quality gap is not “add more YAML.” It is **who looks at pixels, how many times, and which model is allowed to rewrite the composition.**

---

## 2. What a stronger model will not fix by itself

Do not buy Claude and expect these to disappear. They are control-plane bugs / product limits:

1. **Catalog EDL.** `runner/edl.py` still maps `diagram|map|etymology|timeline` to Explainer `text_card` when there is no MC clip. A better LLM will still emit those scene types; the compiler will still flatten them to a title on a dark slide unless we change the mapping (section 5).
2. **Empty mermaid skip.** Production compose already refuses the two-circle fixture. A stronger model that forgets to write mermaid still yields a skipped MC scene, then Explainer fallback. The fix is “no mermaid ⇒ rewrite the scene plan or fail the scene,” not a bigger model.
3. **Cinematic / avatar without gen-video keys.** Honest fail. Claude cannot mint a Kling/Runway/HeyGen key.
4. **Screen-demo real OS capture.** Intentionally skipped on the CPU snapshot. Synthetic terminal remains.
5. **Local Whisper / Blender / local GPU.** Policy. Not a model upgrade.
6. **Taste as a product loop.** `prefs.regen_prompt` is already on the job. The missing piece is Kala: filmstrip, per-scene approve, typed regen. That is the Flutter/genedit pass, not this engine pass.

If we only swap `llm.provider` to Anthropic and ship, we will still get Explainer cards for diagrams that never grew mermaid, and atelier will still be one JSON dump.

---

## 3. Recommended model split

Keep **three named roles**. Do not run the whole job on the expensive model.

| Role | `kind` already in code | Default (saas.yaml) | Proposed production | Why |
|---|---|---|---|---|
| Planner | `default` | `gemini-2.0-flash` | Cheap: Gemini Flash or `gpt-4o-mini` | Research, proposal, script, scene_plan. High volume, JSON artifacts, tool calls to search. |
| Picture author | `atelier` | `gemini-2.5-flash` | **Strongest paid model you will actually invoice** — Claude Sonnet 4.5 / Opus, or GPT-4.1, not Flash | TSX, kinetic HTML, Motion Canvas scene bodies, mermaid, HyperFrames HTML. This is where Cursor quality lives. |
| Vision QA | `visual_qa` | `gemini-2.0-flash` | A **vision-capable** model (Gemini Flash with camelCase inlineData is fine; Claude/GPT-4o also work) | Must see stills. Must return `{pass, severity, issues, rewrite_hint}`. |

**Factory (List A, already shipped):** `config/saas.yaml` `llm.provider` + `STUDIO_LLM_PROVIDER`, with `atelier_model` / `visual_qa_model` / `model`. If provider is `openai` / `anthropic` and the configured name still says `gemini-*`, the factory maps to `gpt-4o-mini` / `claude-sonnet-4-5` so we do not send a Gemini id to the wrong API.

**Proposed saas.yaml for the picture pass (do not apply until approved):**

```yaml
llm:
  provider: anthropic          # or openai; gemini stays valid
  model: claude-sonnet-4-5     # planner can stay cheaper via a new llm.planner_model if we add it
  atelier_model: claude-opus-4-6   # or the strongest Sonnet you will pay for
  visual_qa_model: gemini-2.0-flash  # vision is cheap; keep Gemini here even if atelier is Claude
```

**Tradeoff:** Mixing Gemini QA + Claude atelier is the best cost curve. Mixing also means two API keys on the VM. If we want one vendor, use Claude for atelier *and* visual_qa; keep Gemini or mini for planner.

**Do not** send atelier TSX through Flash “because it is the default.” That is the current quality ceiling.

---

## 4. Atelier as a loop (the main picture upgrade)

### Problem

Today `runner/atelier.py` asks the model for a JSON dump of a Remotion Sequence (or kinetic HTML). `max_rounds=4`, **no tools**, skills clipped by `TOKEN_CHAR_CAP` (~24k). Visual QA may trigger **one** `rerender_scene` patch. That is not how Claude in Cursor works. Cursor: write → render stills → look → patch → render again.

### Proposed contract

For `composition_mode: atelier` (and HyperFrames kinetic HTML):

1. **Author** Sequence / `index.html` (current `run_atelier` / `author_kinetic_html`).
2. **Render stills** for each scene (already exists in `visual_qa.extract_stills`).
3. **Send stills back into the atelier model** (`generate_vision` + the current TSX/HTML as the user turn). Prompt: “patch this file; return a unified diff or full file; do not switch to Explainer.”
4. **Apply patch** (`patch_atelier_scene` already exists) → re-render that scene.
5. Repeat **3–5 times** or until vision QA `pass` / `warn`.
6. If still `fail` after the last round: **keep the atelier file and fail that scene’s `qa_status`**. Never call `compose.render_scene` (List A already forbids the Explainer fall-through).

Suggested knobs (job prefs or saas.yaml, not hardcoded magic):

| Knob | Default | Cap |
|---|---|---|
| `atelier_vision_rounds` | 3 | 5 |
| `atelier_stop_on_warn` | true | — |
| `atelier_skill_budget_chars` | 80_000 | 120_000 |

### Why not tools inside the atelier model?

Cursor has a filesystem. The SaaS runner should **not** give the model `shell`. Keep Python as the only writer: model returns JSON / unified diff → `atelier.patch_*` applies → ffmpeg/remotion renders. That matches the bible (no free-form shell) and is testable with `MockGemini`.

### Cost

Worst case: 5 vision rounds × N scenes × Opus. Cap with `prefs.budget_cap_usd` (List A). Recommended: run the loop only on scenes that failed vision, not on every scene. First author pass stays one-shot; the loop is **QA-driven**, same as a human who only reopens the bad shots.

### Tests (when implementing)

- Atelier fail after N rounds never calls Explainer.
- Stills from round *k* are in the next `generate_vision` payload (`inlineData` / OpenAI `image_url` / Anthropic `image`).
- `budget_cap` aborts the loop with `error: budget` and a valid `cost_log`.

---

## 5. Stop mapping diagram/map/code/math to Explainer `text_card`

### Problem

Even with a perfect atelier or MC clip, EDL first-pass still does:

```text
diagram|map|etymology|timeline → type text_card
```

If `insert_motion_canvas` skipped the scene (empty mermaid), the Explainer cut is a title card. That is the “two circles / bouncing title” look users compare to Cursor.

### Proposed compiler rules (engine, not model)

1. If the scene has an `animation` (MC) or atelier generator in `scene_runtimes`, the cut is **background video**, not a typography template. (Partially true today when `mc` path exists.)
2. If the scene type is `diagram|map|etymology|timeline|code|math` **and** there is no MC/atelier clip:
   - **Do not** emit Explainer `text_card` / `hero_title`.
   - Either re-queue atelier/MC with a required mermaid/code/tex artifact, or mark the scene `qa_status: fail` with `rewrite_hint: "this scene needs a real diagram, not a title card"`.
3. Keep Explainer `bar_chart` / `stat_card` / `terminal_scene` for scenes that actually have chart data, a formula, or a code snippet. List A already stops the second chart pass from eating `end_tag`.
4. Optional later: a dedicated Remotion “diagram shell” is still a catalog. Prefer MC/atelier over a new Explainer variant.

### Tradeoff

Some jobs will **fail a scene** instead of showing a pretty title. That is the honest product. Kala can regen that scene. Silent title cards are how we lost Cursor parity.

---

## 6. Character / HyperFrames: same vision-patch loop

`directors.run_character_chain` is one-shot. Treat it like atelier:

1. Generate rig + first HTML.
2. Extract stills (and a short strip if cheap).
3. Vision model: “broken IK / T-pose / mesh explode?” → patch HTML/CSS/JS → re-render.
4. 3–5 rounds, then fail the scene, never swap to Explainer `hero_title`.

Reuse the atelier loop machinery with a `kind: "hyperframes"` strategy so we do not grow a second ad-hoc rewriter.

Layer 3: `skills/core/hyperframes.md` + `.agents/skills/svg-character-animation` must be in the prompt **untruncated** for this path (section 7).

---

## 7. Full Layer 3 skills for picture authors

### Problem

`TOKEN_CHAR_CAP` (~24k) truncates remotion / HyperFrames / Motion Canvas skills. Claude in Cursor reads the whole skill. Flash with a stub skill writes generic TSX.

### Proposed

| Consumer | Skill budget |
|---|---|
| Planner (`default`) | Keep ~24k. Research/script do not need MC internals. |
| Atelier / MC / HF author | **No cap**, or cap ≥ 80k, and pack **only** the Layer 3 files for that runtime (`remotion`, `hyperframes`, `motion-canvas`, `bespoke-composition`). |
| Visual QA | Short QA rubric only (already small). |

Implementation sketch: `runner/skills.py` gains `load_picture_skills(runtime) -> str` that concatenates the relevant Layer 3 files without the global cap. Planner keeps `load_stage_skill(..., limit=24000)`.

Do not dump the entire `.agents/skills` tree into every stage. That is how we blow the cap *and* the context window.

---

## 8. Taste pass (product, not this engine PR)

Engine already accepts `prefs.regen_prompt` and `regenerate_scene_id`. Picture completeness for a *user* is:

1. Storyboard filmstrip in Kala (posters already written by the runner).
2. Per-scene approve / regen with a typed note.
3. Genedit POST continues the same work_dir with `regenerate_scene_id` + `regen_prompt`.
4. Atelier regen patches TSX (already). Templated regen requires a non-empty `asset_manifest` (List A).

**Do not implement Kala in this repo.** When List B is approved, the engine work is: make sure regen_prompt is threaded into the vision-patch loop (section 4), not only the first author prompt.

---

## 9. Suggested implementation order (next pass, after approval)

Do these as separate PRs on `studio-v1`. Do not mix with genedit/Kala.

| Step | Change | Depends on approval of |
|---|---|---|
| B1 | saas.yaml model split + document which key each role needs | Section 3 |
| B2 | `load_picture_skills` without 24k cap for atelier/MC/HF | Section 7 |
| B3 | Atelier vision-patch loop (3–5 rounds, fail in place) | Section 4 |
| B4 | EDL: diagram-family without MC/atelier ≠ Explainer text_card | Section 5 |
| B5 | Character/HF uses the same loop | Section 6 |
| B6 | Thread `regen_prompt` into loop rounds | Section 8 (engine half) |

**Default recommendation if you only approve one thing:** B3 + B1. A Claude atelier loop on failing scenes is the largest visual delta. B4 is the second, because it stops the compiler from undoing B3.

---

## 10. Decisions needed from you

Reply with approve / change / skip per item. Implementation waits.

1. **Atelier model:** Claude Opus, Claude Sonnet, or GPT-4.1? (Affects invoice and `atelier_model`.)
2. **Planner model:** stay on Gemini Flash?
3. **Visual QA model:** stay on Gemini Flash (vision), or same vendor as atelier?
4. **Atelier loop rounds:** 3 or 5? Stop on `warn` or only on `pass`?
5. **EDL honesty:** fail leftover diagram scenes, or keep Explainer text_card as a last resort for v1?
6. **Skill budget:** 80k chars for picture authors, or unbounded?
7. **Character loop:** same PR as atelier loop, or a follow-up?

Anything not listed here is either already List A or explicitly not an engine gap (Kala, Vultr, local GPU).
