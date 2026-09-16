import {createRequire} from 'node:module';
import {defineConfig} from 'vite';

const require = createRequire(import.meta.url);
const mcMod = require('@motion-canvas/vite-plugin');
const ffMod = require('@motion-canvas/ffmpeg');
const motionCanvas = mcMod.default || mcMod;
const ffmpeg = ffMod.default || ffMod;

export default defineConfig({
  plugins: [motionCanvas({project: './src/project.ts'}), ffmpeg()],
});
