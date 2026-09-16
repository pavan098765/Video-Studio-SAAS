# API LLM Script / Scene Overlay

Cursor / IDE agents: **ignore this file.** Job-runner only. Wins over script/scene directors when they ask for shell, Skill Creator, or waiting on a human.

## Hard rules

1. Prefer facts already in `prior_artifacts.research_brief`. Do not duplicate the research stage.
2. **Script** may call `web_search` when that tool is declared if a specific claim is missing. Copy result URLs if you mention them in prose; the script schema has no `source_url` — do not invent image or paper URLs as "references."
3. **scene_plan** uses only declared tools. Describe `required_assets` for the asset-director. Put real `mermaid` / `code_snippet` / `formula_tex` in scene_plan fields for diagram/code/math scenes. Do not emit a 3-node stub.
4. Do not wait for approval. Speaker directions in the script JSON are enough.
5. Cover the selected concept's duration. Ground claims in the research brief, not training-data statistics.
6. Screen-demo: lock `real_capture` vs `synthetic_terminal` in the idea/brief artifact — do not leave mode selection to Python keyword heuristics.
