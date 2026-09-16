# Pipeline smokes (engine fixtures)

Zero-GPU snapshot must complete documentary, explainer, screen-demo, character-animation, clip-factory, and dub **with the locked compositor** (Remotion / HyperFrames / Motion Canvas / FFmpeg). Color cards are not a pass. Cinematic and avatar without video keys fail the delivery promise instead of a silent slideshow.

`run_job` must call the same CLIs as `smokes/` (Remotion render, HyperFrames, Motion Canvas adapter, FFmpeg stitch). Tests skip a compositor when its doctor is false; they **fail** if production compose is `_write_silent_mp4` / debug cards.

| Pipeline | Fixture | Result |
| --- | --- | --- |
| animated-explainer | `fixtures/explainer_auto.json` | Auto → watchable `final.mp4`; `render_report.metadata.generators` is remotion and/or motion_canvas |
| animated-explainer | `fixtures/explainer_review.json` | Real scene MP4s + storyboard, no stitch |
| documentary-montage | `fixtures/documentary_auto.json` | Stock/VO + map scene Motion Canvas when doctor OK; end-tag Remotion (`scene_assemble`); asset paths are video/audio |
| animation | `fixtures/animation_auto.json` | Remotion |
| character-animation | `fixtures/character_animation_auto.json` | HyperFrames workspace `index.html`; `character_rig_renderer` ran |
| cinematic | `fixtures/cinematic_novideo.json` | `delivery_promise` fail (no video keys) |
| avatar-spokesperson | `fixtures/avatar_novideo.json` | `delivery_promise` fail (no video keys) |
| clip-factory | `fixtures/clip_factory_auto.json` | FFmpeg family; dummy ingest when canned keys missing |
| hybrid | `fixtures/hybrid_auto.json` | map/etymology → Motion Canvas insert when doctor OK |
| localization-dub | `fixtures/localization_dub_auto.json` | Whisper API, or canned transcript; no local whisper |
| podcast-repurpose | `fixtures/podcast_repurpose_auto.json` | FFmpeg family |
| screen-demo | `fixtures/screen_demo_auto.json` | Remotion `TerminalScene` (no `screen_recorder` on the VM) |
| talking-head | `fixtures/talking_head_auto.json` | Remotion TalkingHead on ingested footage |
| auto (router) | Genedit `route_auto_pipeline` | talking clip → talking-head; long video → clip-factory; else explainer |

Assertions:

- Audio mean volume is above a silence floor (not `anullsrc` cards)
- Scene `*.runtime.json` generator ∈ remotion / hyperframes / motion_canvas / ffmpeg
- `tests/test_runner.py` fails if `runner/loop.py` still defines `_write_silent_mp4`

Run: `python tests/test_runner.py`
