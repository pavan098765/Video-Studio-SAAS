# API LLM Atelier Overlay

Cursor / IDE agents: **ignore this file.** Prepended when authoring Remotion TSX or HyperFrames HTML. When this overlay conflicts with picture-pack skills on shell/`npx`, **this overlay wins**.

Picture packs tell an IDE agent to write files and run `npx remotion` / `npx hyperframes`. SaaS exposes `write_file`, `read_file`, `atelier_still`, `typecheck_atelier`, `hyperframes_lint`, and `hyperframes_validate` instead. Do **not** dump TSX as a JSON string and hope Python scaffolds it.

## Hard rules

1. Author files with `write_file` under the project workspace. Then `typecheck_atelier` or `hyperframes_lint` / `hyperframes_validate`, then `atelier_still` per scene (`composition_id` required; stills write into this job workspace, not repo `projects/<slug>`), then patch via another `write_file`. Keep iterating until distinctness review passes. After render, inspect stills/frames — the reviewer looks at the picture, not JSON alone.
2. Do not emit `npx`, `npm`, or `shell`. Do not return `composition_tsx` as the stage artifact unless the director skill's canonical artifact is JSON — the compose artifact is still `render_report` after `video_compose` / `hyperframes_compose`.
3. Do not import `remotion-composer/src` stock scene components or Explainer. Do not use HyperFrames `kline` title stacks.
4. Motion must exist (Sequence / interpolate / spring / GSAP). Overwrite any black scaffold.
5. `Composition.tsx` must export `Scene`, `SceneProps`, and `calculateMetadata`. If the main component is named `Composition`, also export `export const Scene = Composition`.
6. Every `<OffthreadVideo>` must set `acceptableTimeShiftInSeconds={1}` and `toneMapped={false}`.
7. Distinctness: could this be any other product's video? If yes, rewrite.
