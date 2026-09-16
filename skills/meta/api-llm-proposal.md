# API LLM Proposal / Idea Overlay

Cursor / IDE agents: **ignore this file.** Job-runner only. When this overlay conflicts with a proposal/idea director on shell or human wait, **this overlay wins**.

IDE OpenMontage waits for the user after pitching Remotion vs HyperFrames. SaaS auto-decides that wait **after** this director artifact is written — the runner stamps the human gate. You do not.

## Hard rules

1. Do not call `python -c`, `shell`, or `npx`. Do not wait for a human.
2. Write **three** distinct `concept_options` with different `narrative_structure` and hooks. `why_this_works` must cite research findings, not vibes.
3. Present **both** Remotion and HyperFrames in `decision_log` (`category: render_runtime_selection`) when `MACHINE_FACTS.render_engines` shows both. Then pick one and set `production_plan.render_runtime`. A log with only one runtime considered is a reviewer critical.
4. Log `composition_mode` (`templated` vs `atelier`) as its own decision. Default to atelier for hero explainers only after you wrote both options. Do not silently inherit a Python default.
5. Pick `selected_concept.concept_id`, fill `cost_estimate` with the schema keys (`total_estimated_usd`, `line_items` of `{tool, operation, estimated_usd}`, `budget_verdict`), and include `taste_profile` for atelier/hero work. Do not rename those to `items` / `verdict` / `cost_usd`. Set `approval` to `{"status": "pending"}` only — same as the proposal director. Do not set `approved`. Do not add `approved_by`, `timestamp`, or any other approval key. The runner applies the human-gate stamp after reviewer pass.
6. If you change a prior choice (provider, runtime, music), **append** a new `decision_log` row with the same `category` AND `subject`.
