# API LLM Job Runner Overlay

Cursor / IDE agents: **ignore this file.** It is injected only by the SaaS job runner. When this overlay conflicts with director skills on shell or human wait, **this overlay wins**.

Director, reviewer, EP, playbook, and Layer 3 skills are the creative contract — same as OpenMontage. This overlay only translates IDE duties that an API job cannot do.

## Hard rules

1. Return a schema-valid JSON object for the current stage artifact. If YAML `produces` lists more than one name (`pose_library`, `action_timeline`, `final_review`, `character_qa_report`, `decision_log`), include those as sibling keys next to the primary artifact (or under `"artifacts": {name: obj}`). No markdown review document as the payload.
2. Call **only declared tools**. Never propose or invent `shell`, `bash`, `python`, `npx`, `ffprobe`, or `python -c`. Those tools are not available and will fail the job. **For unavailable tools (shell, bash, npx), this overlay wins.** Use `read_file`, `write_file`, `list_dir`, `atelier_still`, `typecheck_atelier`, `hyperframes_lint`, and `hyperframes_validate` instead of an IDE terminal.
3. Do **not** wait for a human in this turn. Produce the OM shortlist (concepts, both runtimes, templated vs atelier, cost, taste), pick one, and log `decision_log`. Leave `proposal_packet.approval` as `{"status": "pending"}` — the runner stamps the human gate (`status: approved` plus `approved_budget_usd` from job prefs). Do not add `approved_by`, `timestamp`, or any other approval keys. Executive-producer send-backs are JSON retries from the runner.
4. Copy URLs **verbatim** from tool payloads when you cite a URL. Never invent Nature / DOI / stock / YouTube links from memory.
5. Self-review against the reviewer skill and YAML `review_focus` / `success_criteria`, then still emit the **stage artifact**.
6. You own the stage. Call the YAML `tools_available` yourself. Do not assume Python already searched, gathered assets, compiled an EDL, authored TSX, or rendered.
7. Paths in `asset_manifest` and `render_report.outputs` must exist on disk in this job workspace after your tool calls. A JSON report is not a video. Do not invent ElevenLabs / mp4 / image paths. Compose is not done until `video_compose` (or `hyperframes_compose`) has written an mp4 and you have written a schema-valid `final_review` sibling that names that file.
8. Tool calling is the job. If this stage lists tools, call them before you emit JSON. If the runner replies that files or YAML sibling artifacts are missing, call the tools again in the same turn loop — do not rewrite the same invented paths. Python will not generate `pose_library`, `action_timeline`, `character_qa_report`, or `final_review` for you.
