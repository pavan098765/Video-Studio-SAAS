# Picture completeness — implementation plan

**Status:** locked 2026-09-11 (updated same day: 1M planner context; atelier skills are runtime packs, not per-scene and not the whole tree). Implement from this file.  
**Source:** [`PICTURE_COMPLETENESS.md`](PICTURE_COMPLETENESS.md) + your replies.  
**Repo:** this engine only. No Kala / genedit / Vultr.

---

## Locked decisions

| Topic | Decision |
|---|---|
| Models | **Not provider-locked.** Planner, Visual QA, and Atelier each have their own `provider` + `model` (+ optional `base_url` / `api_key_env`). You will set IDs while testing (planner + QA on Gemini Flash Lite; atelier = Claude or GPT or GLM or anything else). |
| Atelier loop | **3** vision-patch rounds after first author. Stop early on `pass` or `warn`. After 3 still-`fail`: **keep the last round’s file**. Do not Explainer-swap. Do not drop the scene. |
| Character / HyperFrames | **Same loop** as atelier (3 rounds, salvage last render). |
| Skills | **No 24k/4k truncates.** Whole files only. Planner + Visual QA on Gemini 3.5 Flash Lite (**1,048,576 input tokens**) so director + bespoke + reviewer all fit — **do not drop reviewer**. Atelier gets a **runtime pack** (Remotion *or* HyperFrames), not every skill and not a different pack per scene. |
| EDL / Explainer | See §3. Plain-language: a diagram scene must not become a title card just because Motion Canvas skipped. |

Defaults in `config/saas.yaml` for first test (you can change any line without a code change):

```yaml
llm:
  planner:
    provider: gemini
    model: gemini-3.5-flash-lite   # 1,048,576 input / 65,536 output
  visual_qa:
    provider: gemini
    model: gemini-3.5-flash-lite   # vision-capable Flash Lite
  atelier:
    provider: anthropic            # example; swap to openai / openai_compat
    model: claude-sonnet-4-5
    # base_url: https://api.z.ai/api/paas/v4   # GLM etc.
    # api_key_env: GLM_API_KEY
```

Env overrides for A/B tests (no yaml edit): `STUDIO_LLM_PLANNER_PROVIDER`, `STUDIO_LLM_PLANNER_MODEL`, `STUDIO_LLM_ATELIER_*`, `STUDIO_LLM_VISUAL_QA_*`, plus `STUDIO_LLM_ATELIER_BASE_URL`.

---

## 1. Per-role models (not one global provider)

### Problem today

List A added `llm.provider` as a **single** switch. If it is `openai`, a Gemini-named `atelier_model` is rewritten to `gpt-4o-mini`. That fights testing: you cannot keep planner on Gemini and atelier on Claude/GLM at once.

### Design

`default_model(kind)` resolves **only that role**:

| `kind` | Config block | Used for |
|---|---|---|
| `default` | `llm.planner` | research, proposal, script, scene_plan |
| `visual_qa` | `llm.visual_qa` | still review |
| `atelier` | `llm.atelier` | Remotion TSX, kinetic HTML, vision-patch, character/HF patch |

Adapters (same `StageModel` protocol as now):

- `gemini` → existing `GeminiFlash`
- `openai` → existing `OpenAIChat` (`OPENAI_API_KEY`)
- `anthropic` → existing `AnthropicChat` (`ANTHROPIC_API_KEY`)
- `openai_compat` → `OpenAIChat` with `base_url` + `api_key_env` (GLM, Together, Fireworks, custom)

**Do not** rewrite model IDs. Send exactly the string you configured. GLM is not a special case beyond `openai_compat`.

**Remove** the List A gemini→gpt / gemini→claude fallback in `runner/config.py`.

### Tests

- Planner Gemini + atelier Anthropic in one process (two clients).
- `openai_compat` posts to `base_url`, not `api.openai.com`.
- Changing only `llm.atelier.model` does not change planner.

---

## 2. Atelier / character vision-patch loop (3 rounds, salvage)

Shared helper, two strategies (`remotion_atelier` | `hyperframes`), so we do not grow a second rewriter.

```
author once
for round in 1..3:
  render stills
  visual_qa (planner-cheap vision model)
  if pass or warn: stop
  atelier model sees stills + current file + rewrite_hint + regen_prompt
  apply patch (Python only; no shell tool)
  re-render
keep last file always
qa_status may stay fail on the storyboard (so Kala can regen later)
never compose.render_scene / Explainer
```

- First author stays one-shot (current `run_atelier` / `author_kinetic_html` / `run_character_chain`).
- Loop runs **only on scenes that failed** vision, not every scene.
- `budget_cap_usd` can abort mid-loop (`error: budget`); last successful render is still kept.
- Character: same after first HTML; patch HTML/CSS/JS; stills check T-pose / broken IK / empty frame.

### Tests

- Three failing QA turns ⇒ three patch calls, then original-or-last file kept; `compose.render_scene` never called.
- Pass on round 2 ⇒ no round 3.
- Character path uses the same helper (`kind=hyperframes`).

---

## 3. Explainer `text_card` — what this actually means

This section is the clarity you asked for. It is a **compiler** issue, not a “pick a better model” issue.

### Two different machines draw a “diagram”

| Machine | What it is | What you see |
|---|---|---|
| **Picture renderer** | Motion Canvas clip, or atelier Remotion Sequence, or HyperFrames HTML | Nodes, arrows, code, motion |
| **Explainer catalog** | Fixed Remotion templates: `text_card`, `callout`, `hero_title`, `bar_chart`, `stat_card`, `terminal_scene` | A **title (and maybe a number) on a designed slide** |

The scene plan can say `type: diagram`. That is a **wish**. Something still has to draw it.

### What the EDL does today

`runner/edl.py` translates scene types into Explainer template names:

```text
diagram  →  text_card
map      →  text_card
timeline →  text_card
code     →  terminal_scene   (ok if we have real steps)
math     →  stat_card        (ok if we have a real number)
```

Then, if Motion Canvas produced an mp4, that file is attached as `backgroundVideo` **on top of** a `text_card` cut. If Motion Canvas **skipped** (empty mermaid — List A already refuses the two-circle fixture), there is **no** video. Explainer still renders `text_card`: big title, dark slide. That is the “Cursor would have drawn a diagram; Studio showed a caption” gap.

There is a second copy of the same bug at compose time: if a scene is locked to `motion_canvas` and generate/render fails, `runner/loop.py` sets `explainer_fallback_from_mc` and calls Explainer anyway.

```1088:1113:runner/loop.py
                    if runtime == "motion_canvas":
                        warnings.append(f"MC failed for {sid}: {exc}; explainer fallback")
                        meta = compose.render_scene(
                            dest=dest,
                            scene={**scene, "type": "diagram"},
                            runtime="remotion",
                            ...
                        )
                        meta["generator"] = "explainer_fallback_from_mc"
```

So: **even a perfect Claude atelier file can be ignored**, and the user still sees a title card, because Python asked Explainer to draw a diagram.

### Tiny story

Scene plan: `sc2 type=diagram “Calvin cycle”`.

1. Planner did not emit mermaid (Flash Lite often won’t).
2. `insert_motion_canvas` skips sc2 (no mermaid, fixture off).
3. EDL: sc2 becomes Explainer `text_card` with text “Calvin cycle”.
4. Compose draws a title slide.
5. Visual QA may even pass if the type is readable.

Cursor + Claude would have written mermaid or TSX and drawn the cycle. The engine never gave that model the job for sc2; it **translated “diagram” into “title template.”**

### What we will change (aligned with salvage, not hard-fail)

Keep Explainer for things that **are** catalog slides: hook copy, end card, a real bar chart, a real stat, a real terminal snippet.

For `diagram | map | etymology | timeline | code | math | character_scene`:

1. If a picture clip exists (MC `animation` asset, atelier scene mp4, HF render) → the cut is **that video full-frame**. Do not also set Explainer `text_card` / `hero_title` as the drawing. A small caption overlay is fine; a title standing in for the diagram is not.
2. If no picture clip → **do not mint an Explainer title and call it a diagram.** Keep the last atelier/MC/HF file if one exists (your salvage rule). If none exists, leave the scene as a failed picture attempt (`qa_status: fail`, rewrite_hint: needs mermaid/code/tex or atelier), and still **do not** swap in `text_card`.
3. Delete `explainer_fallback_from_mc` in `loop.py` / `compose.py`. MC fail ⇒ skip or keep last file, never Explainer diagram.
4. Keep `bar_chart` / `stat_card` / `terminal_scene` only when the cut actually has `chartData` / a formula / code steps. Empty code scene is not a fake terminal of `print(topic)`.

The job can still stitch. The storyboard can show sc2 as failed so you regen it. The MP4 for sc2 is either a real picture or the last salvage — not a catalog title pretending to be a diagram.

### Tests

- Diagram + MC mp4 ⇒ cut is not `text_card` (video passthrough).
- Diagram + no mermaid + no MC ⇒ no Explainer `text_card` cut; no `explainer_fallback_from_mc`.
- `end_tag` still `hero_title`; research charts still `bar_chart` on a real text/hook scene (List A).

---

## 4. Skill budgets (measured, per call, never half a file)

Measured 2026-09-11 (character counts, UTF-8).

### What is wrong today

`runner/skills.py`:

- Global `TOKEN_CHAR_CAP = 24_000` slices the **combined** blob and appends `[truncated]`.
- `load_layer3` also caps **each** Layer 3 file at **4_000** (`min(remain, 4000)`).
- Explainer `proposal-director.md` alone is **32,559** chars — already over 24k — so proposal never includes a full director, and Remotion/HF skills arrive as a 4k stub.

### File sizes that matter

| File | Chars |
|---|---|
| `skills/pipelines/explainer/proposal-director.md` | 32,559 |
| Other typical directors | 16k–20k |
| `skills/meta/bespoke-composition.md` | 19,409 |
| `skills/meta/reviewer.md` | 25,219 |
| `skills/core/remotion.md` | 18,296 |
| `skills/core/hyperframes.md` | 21,952 |
| `.agents/skills/remotion/SKILL.md` | 6,165 |
| `.agents/skills/remotion-best-practices/SKILL.md` | 4,240 |
| `.agents/skills/hyperframes/SKILL.md` | 17,073 |
| `.agents/skills/gsap-core/SKILL.md` | 14,678 |

**Packs (full files, concatenated):**

| Pack | Files | Chars | Rule |
|---|---|---|---|
| Planner, one stage | that director + extras | director up to 32,559 + bespoke 19k + reviewer 25k ≈ **77k** | Send **all complete**. Flash Lite 1M-token input makes this cheap. Never drop reviewer. |
| Atelier Remotion (author + every patch round) | bespoke + core remotion + L3 remotion + remotion-best-practices | **48,110** | Whole pack every atelier Remotion call |
| Character / HF (author + every patch round) | core HF + L3 hyperframes family + svg-character + rigging + qa + gsap-core | **78,225** | Whole pack every HF/character call |
| Visual QA | code rubric only (`visual_qa.py`) | ~1k | No picture skills |
| Both picture packs at once | 126k | **never** — pick Remotion *or* HF from the runtime, not scene type |

### How atelier skills are scoped (confirmed)

**Not** “all 89 Layer 3 skills on every call.” **Not** a unique skill file per scene type.

| Call | Skills (system prompt) | Scene-specific part (user turn) |
|---|---|---|
| Atelier **author** (one Composition.tsx / one kinetic HTML for the job) | Runtime pack only: Remotion pack **or** HF pack | Full scene_plan + script + assets JSON |
| Atelier **patch round** (one failing scene) | **Same runtime pack** (must keep Remotion/HF APIs in context) | That scene’s stills + rewrite_hint + `regen_prompt` + current file |
| Planner research/script/proposal | That stage’s **director file** + listed extras (bespoke, reviewer) + Layer 3 for **that stage’s tools**, minus Remotion/HF/gsap | Stage user JSON |
| Visual QA | Rubric in code | Stills + scene id/type |

Why not mermaid-only when patching a diagram: the file being patched is still Remotion TSX (or HF HTML). Dropping the Remotion pack would make the model forget Sequence/karaoke rules. The scene is selected in the **user** payload, not by swapping the skill pack.

`author_kinetic_html` today gets **no** skill_text — that is a wiring gap this pass fixes (HF pack).

Playbooks are YAML (`styles/*.yaml`), not the `.md` `load_playbook` looks for — so playbook injection is currently a no-op. Out of scope unless we later wire YAML excerpts.

### Rules to implement

1. `load_complete(paths) -> str` — concatenate full files. Missing file ⇒ skip. **Never slice.**
2. If the pack would exceed its table budget, **omit the lowest-priority file in that pack**, log which, do not cut mid-file.
3. Planner stages: director file complete; `extra_skills` complete including **reviewer** (Flash Lite 1M context). Layer 3 for that stage’s tools, **excluding** Remotion/HF/gsap picture skills (wrong job, not a token problem).
4. Atelier author + patch rounds: Remotion pack (48k), complete, every call. User turn is per-scene on patch rounds.
5. Character / kinetic HTML + patch rounds: HF pack (78k), complete, every call.
6. Visual QA: rubric in code only.

Priority inside the HF pack if we ever have to omit: drop `hyperframes-creative` then `gsap-core` last (animation rules matter more than extra creative prose). For Remotion pack everything fits in 55k — omit nothing.

### Tests

- Atelier system prompt contains the full `bespoke-composition.md` (no `[truncated]`, length ≥ file size).
- Planner research prompt does **not** contain `makeScene2D` / Remotion Layer 3.
- `load_complete` never returns a string that includes `[truncated]`.

---

## 5. Implementation order (this repo, one pass unless you split)

| Step | Work | Files (primary) |
|---|---|---|
| 1 | Per-role LLM factory + `openai_compat` + yaml/env; delete global provider rewrite | `config/saas.yaml`, `runner/config.py`, `runner/llm_gemini.py`, `runner/llm_openai.py` |
| 2 | Skill loaders per pack; delete 24k/4k truncates for these paths | `runner/skills.py`, `runner/loop.py` planner vs atelier skill wiring |
| 3 | Shared 3-round vision-patch + salvage last file | `runner/atelier.py`, `runner/visual_qa.py`, `runner/loop.py` |
| 4 | Character/HF uses that helper | `runner/directors.py`, `runner/loop.py` |
| 5 | EDL + kill `explainer_fallback_from_mc` | `runner/edl.py`, `runner/compose.py`, `runner/loop.py` |
| 6 | Thread `regen_prompt` into patch rounds | atelier/character patch user payload |

Canned jobs stay one-shot (no live vision loop). `STUDIO_LLM=mock` still injects `MockGemini`.

Out of scope: Kala filmstrip, genedit, Vultr, cinematic keys, local Whisper/GPU.

---

## 6. Locked (do not re-ask)

1. Yaml atelier example: Anthropic Sonnet (overwrite while testing).
2. Planner **keeps reviewer + bespoke** complete. Gemini 3.5 Flash Lite 1,048,576-token input.
3. Salvage = keep last picture file; `qa_status` may stay `fail`; job still stitches (do **not** abort when every scene still fails QA).
4. EDL: no Explainer title standing in for diagram/map/code/math.
5. Atelier skills = runtime pack (Remotion **or** HF), not all skills, not per-scene-type.
