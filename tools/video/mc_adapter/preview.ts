import {
  PlaybackManager,
  PlaybackState,
  PlaybackStatus,
  SharedWebGLContext,
  Stage,
  Vector2,
} from '@motion-canvas/core';
import {ReadOnlyTimeEvents} from '@motion-canvas/core/lib/scenes/timeEvents';
import project from './src/project';

/**
 * Headless capture must follow Renderer.js, not Player.
 * Player.requestRender only sets a flag; paint happens on requestAnimationFrame.
 * Chrome headless often never fires that loop, so the canvas stays the stage fill.
 *
 * Scene2D layout uses getBoundingClientRect() on nodes inside #motion-canvas-2d-frame.
 * If that host is 0×0 (Motion Canvas default), every layout node has size 0 and
 * only the stage background is encoded.
 */
type McWindow = Window & {
  __mcStage: Stage;
  __mcFrame: number;
  __mcReady: boolean;
  __mcError: string;
  __mcInfo: Record<string, unknown>;
  __mcSeek: (frame: number) => Promise<void>;
};

const w = window as unknown as McWindow;
const size = new Vector2(1920, 1080);

function ensureLayoutHost() {
  let frame = document.getElementById('motion-canvas-2d-frame') as HTMLDivElement | null;
  if (!frame) {
    frame = document.createElement('div');
    frame.id = 'motion-canvas-2d-frame';
    document.body.prepend(frame);
  }
  frame.style.position = 'absolute';
  frame.style.pointerEvents = 'none';
  frame.style.top = '0';
  frame.style.left = '0';
  frame.style.width = `${size.x}px`;
  frame.style.height = `${size.y}px`;
  frame.style.overflow = 'visible';
  frame.style.opacity = '1';
}

ensureLayoutHost();

const stage = new Stage();
stage.configure({
  size,
  resolutionScale: 1,
  background: '#0f172a',
});
document.getElementById('root')!.append(stage.finalBuffer);

const playback = new PlaybackManager();
const status = new PlaybackStatus(playback);
const sharedWebGLContext = new SharedWebGLContext(project.logger);
playback.fps = 30;
playback.state = PlaybackState.Rendering;

const scenes = [];
for (const description of project.scenes) {
  const scene = new description.klass({
    ...description,
    meta: description.meta.clone(),
    logger: project.logger,
    playback: status,
    size,
    resolutionScale: 1,
    timeEventsClass: ReadOnlyTimeEvents,
    sharedWebGLContext,
    experimentalFeatures: project.experimentalFeatures,
  });
  scenes.push(scene);
}
playback.setup(scenes);

for (let i = 0; i < project.scenes.length; i++) {
  const description = project.scenes[i];
  const scene = scenes[i];
  scene.reload({
    config: description.config,
    size,
    resolutionScale: 1,
  });
  scene.meta.set(description.meta.get());
  scene.variables.updateSignals(project.variables ?? {});
}

w.__mcStage = stage;
w.__mcFrame = -1;
w.__mcReady = false;
w.__mcError = '';
w.__mcInfo = {};

function fail(err: unknown) {
  const message = err instanceof Error ? err.stack || err.message : String(err);
  w.__mcError = message;
  console.error(err);
}

function snapshotInfo() {
  try {
    const scene = playback.currentScene as {
      getSize: () => {x: number; y: number};
      getView: () => {
        children: () => Array<{computedSize?: () => {width: number; height: number}}>;
        size: () => {x: number; y: number};
        element?: HTMLElement;
      };
    };
    const view = scene.getView();
    const kids = view.children();
    w.__mcInfo = {
      scene: {x: scene.getSize().x, y: scene.getSize().y},
      view: {x: view.size().x, y: view.size().y},
      viewRect: view.element?.getBoundingClientRect().toJSON?.() ?? null,
      childCount: kids.length,
      childSizes: kids.slice(0, 6).map((kid) => {
        const box = kid.computedSize?.() ?? {width: -1, height: -1};
        return {width: box.width, height: box.height};
      }),
      canvas: [stage.finalBuffer.width, stage.finalBuffer.height],
    };
  } catch (err) {
    w.__mcInfo = {error: String(err)};
  }
}

const boot = (async () => {
  ensureLayoutHost();
  await playback.recalculate();
  await playback.reset();
  await playback.seek(0);
  ensureLayoutHost();
  await stage.render(playback.currentScene, playback.previousScene);
  snapshotInfo();
  w.__mcFrame = playback.frame;
  w.__mcReady = true;
})();

w.__mcSeek = async (frame: number) => {
  await boot;
  ensureLayoutHost();
  await playback.seek(frame);
  await stage.render(playback.currentScene, playback.previousScene);
  w.__mcFrame = playback.frame;
};

boot.catch(fail);
setTimeout(() => {
  if (!w.__mcReady && !w.__mcError) {
    fail(new Error('Motion Canvas PlaybackManager boot did not finish in 20s'));
  }
}, 20000);
