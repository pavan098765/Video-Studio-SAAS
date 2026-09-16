import {makeScene2D} from '@motion-canvas/2d';
import {Circle, Layout, Rect, Txt} from '@motion-canvas/2d/lib/components';
import {createRef} from '@motion-canvas/core';
import {all, waitFor} from '@motion-canvas/core/lib/flow';

export default makeScene2D(function* (view) {
  view.fill('#0f172a');
  const box = createRef<Rect>();
  const label = createRef<Txt>();
  const dot = createRef<Circle>();

  view.add(
    <Layout layout direction="column" gap={40} alignItems="center">
      <Rect ref={box} width={420} height={180} fill="#1e293b" radius={16} />
      <Txt ref={label} fill="#f8fafc" fontSize={48} text="Diagram smoke" />
      <Circle ref={dot} size={48} fill="#38bdf8" />
    </Layout>,
  );

  yield* all(box().opacity(0).opacity(1, 1), label().opacity(0).opacity(1, 1));
  yield* dot().position.x(200, 2).to(-200, 2).to(0, 2);
  yield* waitFor(3);
});
