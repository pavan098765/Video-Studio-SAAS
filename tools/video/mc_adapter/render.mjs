#!/usr/bin/env node
/**
 * Headless Motion Canvas render via Vite + Chrome canvas capture + ffmpeg.
 * Refuses silent color-card fallbacks.
 */
import {spawn, spawnSync} from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {createServer} from 'vite';

const project = process.argv[2] || 'src/project.ts';
const output = path.resolve(process.argv[3] || 'output.mp4');
const duration = Math.max(1, Number(process.argv[4] || 5));
const fps = 30;
const cwd = process.cwd();

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const remotion = path.resolve(cwd, '../../../remotion-composer/node_modules/.remotion');
  const walk = (dir, depth = 0) => {
    if (!fs.existsSync(dir) || depth > 8) return null;
    for (const name of fs.readdirSync(dir)) {
      const full = path.join(dir, name);
      let stat;
      try {
        stat = fs.statSync(full);
      } catch {
        continue;
      }
      if (stat.isFile() && /chrome-headless-shell(\.exe)?$/i.test(name)) return full;
      if (stat.isDirectory()) {
        const hit = walk(full, depth + 1);
        if (hit) return hit;
      }
    }
    return null;
  };
  const fromRemotion = walk(remotion);
  if (fromRemotion) return fromRemotion;
  for (const name of ['chrome', 'google-chrome', 'chromium', 'chromium-browser']) {
    const r = spawnSync(name, ['--version'], {encoding: 'utf8'});
    if (r.status === 0) return name;
  }
  return null;
}

async function waitFor(url, tries = 40) {
  for (let i = 0; i < tries; i++) {
    const ok = await new Promise((resolve) => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve(res.statusCode && res.statusCode < 500);
      });
      req.on('error', () => resolve(false));
      req.setTimeout(500, () => {
        req.destroy();
        resolve(false);
      });
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`Vite preview never became ready: ${url}`);
}

async function main() {
  const chrome = findChrome();
  if (!chrome) {
    console.error('Chrome/Chromium missing for Motion Canvas capture; refusing color-card fallback.');
    process.exit(2);
  }

  let puppeteer;
  try {
    puppeteer = await import('puppeteer-core');
  } catch {
    console.error('puppeteer-core missing; run npm install in tools/video/mc_adapter.');
    process.exit(2);
  }

  const server = await createServer({
    configFile: path.join(cwd, 'vite.config.ts'),
    server: {port: 0, strictPort: false, host: '127.0.0.1'},
  });
  await server.listen();
  const addr = server.httpServer.address();
  const port = typeof addr === 'object' && addr ? addr.port : 9000;
  const url = `http://127.0.0.1:${port}/preview.html`;
  await waitFor(url);

  const frameDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mc-frames-'));
  const browser = await puppeteer.default.launch({
    executablePath: chrome,
    headless: true,
    args: ['--no-sandbox', '--hide-scrollbars', `--window-size=1920,1080`],
  });
  try {
    const page = await browser.newPage();
    page.on('pageerror', (err) => console.error('[mc pageerror]', err));
    page.on('console', (msg) => {
      const type = msg.type();
      if (type === 'error' || type === 'warning') {
        console.error(`[mc ${type}]`, msg.text());
      }
    });
    await page.setViewport({width: 1920, height: 1080, deviceScaleFactor: 1});
    await page.goto(url, {waitUntil: 'networkidle0', timeout: 60000});
    await page.waitForFunction(
      () => window.__mcReady === true || Boolean(window.__mcError),
      {timeout: 30000},
    );
    const bootErr = await page.evaluate(() => window.__mcError || '');
    if (bootErr) {
      throw new Error(`Motion Canvas boot failed: ${bootErr}`);
    }
    const ready = await page.evaluate(() => window.__mcReady === true && window.__mcFrame >= 0);
    if (!ready) {
      throw new Error('Motion Canvas PlaybackManager never drew frame 0');
    }
    const info = await page.evaluate(() => window.__mcInfo || {});
    console.error('[mc info]', JSON.stringify(info));
    const frames = Math.max(30, Math.round(duration * fps));
    for (let i = 0; i < frames; i++) {
      const dataUrl = await page.evaluate(async (frame) => {
        await window.__mcSeek(frame);
        return window.__mcStage.finalBuffer.toDataURL('image/png');
      }, i);
      const dest = path.join(frameDir, `frame_${String(i).padStart(4, '0')}.png`);
      const b64 = String(dataUrl || '').replace(/^data:image\/png;base64,/, '');
      if (!b64) {
        throw new Error(`Motion Canvas frame ${i} produced an empty PNG`);
      }
      fs.writeFileSync(dest, Buffer.from(b64, 'base64'));
    }
  } finally {
    await browser.close();
    await server.close();
  }

  const ff = spawnSync(
    process.env.FFMPEG || 'ffmpeg',
    [
      '-y',
      '-framerate',
      String(fps),
      '-i',
      path.join(frameDir, 'frame_%04d.png'),
      '-vf',
      'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p',
      '-c:v',
      'libx264',
      '-pix_fmt',
      'yuv420p',
      '-r',
      String(fps),
      output,
    ],
    {stdio: 'inherit'},
  );
  if (ff.status !== 0 || !fs.existsSync(output)) {
    console.error('ffmpeg failed to encode Motion Canvas frames');
    process.exit(2);
  }
  process.exit(0);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});
