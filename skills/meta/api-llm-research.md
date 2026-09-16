# API LLM Research Overlay

Cursor / IDE agents: **ignore this file.** Job-runner only. When this overlay conflicts with a Research Director on shell or invented URLs, **this overlay wins**.

IDE OpenMontage: the agent *is* the searcher. SaaS is the same: you call `web_search` and `web_fetch`. Python only executes those tools.

## Hard rules

1. Call `web_search` (and `web_fetch` on the best hits) before writing `research_brief`. Do not invent URLs from training memory.
2. Copy result URLs character-for-character into `data_points.source_url`, `sources[].url`, and `landscape.existing_content[].url`.
3. Write specific claims from snippets/excerpts, not generic textbook filler.
4. Produce at least 3 distinct `angles_discovered` (different hooks), 3 `data_points` with real claims, 3 audience questions, and 1 misconception pair.
5. `research_summary` is one paragraph: the single most useful insight for the Proposal Director.
6. Do not wait for a human. Do not call `shell` / `npx`.
