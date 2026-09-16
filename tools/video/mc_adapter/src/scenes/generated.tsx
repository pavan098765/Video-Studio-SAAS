import {makeScene2D} from '@motion-canvas/2d';
import {Circle, Layout, Line, Rect, Txt} from '@motion-canvas/2d/lib/components';
import {createRef} from '@motion-canvas/core';
import {all, waitFor, waitUntil} from '@motion-canvas/core/lib/flow';

export default makeScene2D(function* (view) {
  view.fill('#0f172a');
  const box = createRef<Rect>();
  const label = createRef<Txt>();
  const dot = createRef<Circle>();
  view.add(
    <Layout layout direction="column" gap={36} alignItems="center">
      <Rect ref={box} width={520} height={160} fill="#1e293b" radius={18} />
      <Txt ref={label} fill="#f8fafc" fontSize={44} text="Motion Canvas adapter" />
      <Circle ref={dot} size={40} fill="#38bdf8" />
    </Layout>,
  );
  yield* all(box().opacity(0).opacity(1, 0.6), label().opacity(0).opacity(1, 0.6));
  yield* waitUntil('hit');
  yield* dot().position.x(220, 1.2).to(-220, 1.2).to(0, 1.2);
  yield* waitFor(1);
});
