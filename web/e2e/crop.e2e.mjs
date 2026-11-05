// End-to-end test of video import and the crop tool, in Microsoft Edge (playwright-core, channel "msedge"),
// against a running server:
//
//   TL_CROP_VIDEO=<a video file, a few seconds or longer> node e2e/crop.e2e.mjs
//
// (server at TL_URL, default http://localhost:8091). It uploads the video through the Media page, imports it
// a second time through the link field (from the server's own /source URL: no internet needed), drives the
// crop tool with real pointer input — shapes, box drag and resize, circles, in / out, turning — saves, and
// checks that the item's edit and the server's rendering (/api/render.png) match what the tool's preview
// showed. Then "Save as a copy", the timeline editor's entry point, and the tool on a phone with touch input.
// Screenshots go to TL_SHOTS (default e2e/shots). It deletes what it made.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename } from 'node:path';
import { chromium } from 'playwright-core';

const BASE = process.env.TL_URL ?? 'http://localhost:8091';
const SHOTS = process.env.TL_SHOTS ?? 'e2e/shots';
const VIDEO = process.env.TL_CROP_VIDEO;
if (!VIDEO) {
  console.log('Set TL_CROP_VIDEO to a video file to upload.');
  process.exit(2);
}
mkdirSync(SHOTS, { recursive: true });

let failures = 0;
const check = (cond, msg) => {
  if (cond) console.log('  ok   ', msg);
  else {
    failures++;
    console.log('  FAIL ', msg);
  }
};
const section = (s) => console.log(`\n${s}`);
const near = (a, b, tol) => Math.abs(a - b) <= tol;

async function api(path, init = {}) {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...init });
  if (!r.ok) throw new Error(`${init.method ?? 'GET'} ${path}: ${r.status} ${await r.text()}`);
  return r.json();
}
async function waitItem(id, pred, ms = 60000) {
  const t0 = Date.now();
  for (;;) {
    const m = await api(`/api/media/${id}`);
    if (pred(m)) return m;
    if (Date.now() - t0 > ms) return m;
    await new Promise((r) => setTimeout(r, 200));
  }
}

const made = { media: new Set(), sets: new Set() };
const before = new Set((await api('/api/media')).map((m) => m.id));

// ---------------------------------------------------------------- tool helpers
function tool(page) {
  const edit = async () => JSON.parse(await page.locator('[data-testid=crop-tool]').getAttribute('data-edit'));
  const box = () => page.locator('[data-testid=crop-box]').boundingBox();
  const frame = () => page.locator('.crop-frame').boundingBox();
  const centre = (b) => ({ x: b.x + b.width / 2, y: b.y + b.height / 2 });
  const drag = async (from, to, steps = 12) => {
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    await page.mouse.move(to.x, to.y, { steps });
    await page.mouse.up();
    await page.waitForTimeout(120);
  };
  const handle = async (h) => centre(await page.locator(`[data-testid=crop-handle-${h}]`).boundingBox());
  /** RGBA of the preview canvas, as an array. */
  const preview = () => page.evaluate(() => {
    const c = document.querySelector('[data-testid=crop-preview]');
    return { w: c.width, h: c.height, data: Array.from(c.getContext('2d').getImageData(0, 0, c.width, c.height).data) };
  });
  const loaded = () => page.waitForSelector('[data-testid=crop-tool][data-loaded="1"]', { timeout: 20000 });
  const shape = (label) => page.locator('.crop-side .segmented button', { hasText: label }).first().click();
  const circles = async (on) => {
    const t = page.locator('.crop-circles [role=switch]');
    if ((await t.getAttribute('aria-checked')) !== String(on)) await t.click();
    await page.waitForTimeout(150);
  };
  return { edit, box, frame, centre, drag, handle, preview, loaded, shape, circles };
}

/** One frame of a stored set as RGBA rows y0..y1 (rendered by the server). */
async function serverRows(page, setId, t, y0, y1) {
  return page.evaluate(async ([id, tt, a, b]) => {
    const blob = await (await fetch(`/api/render.png?set=${id}&t=${tt}&nocache=${Math.random()}`)).blob();
    const bmp = await createImageBitmap(blob);
    const c = document.createElement('canvas');
    c.width = bmp.width;
    c.height = bmp.height;
    const ctx = c.getContext('2d');
    ctx.drawImage(bmp, 0, 0);
    return Array.from(ctx.getImageData(0, a, bmp.width, b - a).data);
  }, [setId, t, y0, y1]);
}

/** Mean absolute difference per channel (0..255) of two RGBA arrays, and how alike their patterns are. */
function compare(a, b) {
  let sum = 0;
  let n = 0;
  const xa = [];
  const xb = [];
  for (let i = 0; i < a.length; i += 4) {
    for (let k = 0; k < 3; k++) {
      sum += Math.abs(a[i + k] - b[i + k]);
      n++;
    }
    xa.push(a[i] + a[i + 1] + a[i + 2]);
    xb.push(b[i] + b[i + 1] + b[i + 2]);
  }
  const mean = (v) => v.reduce((p, q) => p + q, 0) / v.length;
  const ma = mean(xa);
  const mb = mean(xb);
  let cov = 0;
  let va = 0;
  let vb = 0;
  for (let i = 0; i < xa.length; i++) {
    cov += (xa[i] - ma) * (xb[i] - mb);
    va += (xa[i] - ma) ** 2;
    vb += (xb[i] - mb) ** 2;
  }
  // a nearly flat picture has no pattern to correlate: then only the difference counts
  const sd = Math.sqrt(va / xa.length) / 3;
  return { mad: sum / n, corr: va && vb ? cov / Math.sqrt(va * vb) : 1, flat: sd < 8, sd };
}

/** Save two RGBA pictures side by side (for looking at a failed comparison). */
async function dumpPair(page, a, b, w, h, path) {
  const url = await page.evaluate(([pa, pb, ww, hh]) => {
    const c = document.createElement('canvas');
    c.width = ww * 2 + 4;
    c.height = hh;
    const ctx = c.getContext('2d');
    ctx.putImageData(new ImageData(new Uint8ClampedArray(pa), ww, hh), 0, 0);
    ctx.putImageData(new ImageData(new Uint8ClampedArray(pb), ww, hh), ww + 4, 0);
    return c.toDataURL('image/png');
  }, [a, b, w, h]);
  writeFileSync(path, Buffer.from(url.split(',')[1], 'base64'));
}

async function makeSet(name, clip) {
  const s = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name, duration: 20 }) });
  made.sets.add(s.id);
  const track = s.tracks.find((t) => t.target === clip.target);
  track.clips = [{ type: clip.type, start: 0, duration: clip.duration, params: clip.params, mask: clip.mask ?? null }];
  return api(`/api/sets/${s.id}`, { method: 'PUT', body: JSON.stringify(s) });
}

const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => {
    failures++;
    console.log('  PAGE ERROR', e.message);
  });
  const T = tool(page);

  // ---------------------------------------------------------------- upload
  section('upload, and the tool offered when it is ready');
  await page.goto(`${BASE}/media`);
  await page.locator('input[type=file]').setInputFiles({ name: basename(VIDEO), mimeType: 'video/mp4', buffer: readFileSync(VIDEO) });
  const offer = page.locator('.fresh-offer button.primary');
  await offer.waitFor({ timeout: 60000 });
  const item = (await api('/api/media')).find((m) => !before.has(m.id) && m.kind === 'video');
  made.media.add(item.id);
  check(item.status === 'ready' && item.version === 1, `uploaded "${item.name}" is ready (${item.source_width}x${item.source_height}, ${item.source_duration.toFixed(2)} s)`);
  await page.screenshot({ path: `${SHOTS}/crop-0-media-fresh.png` });
  await offer.click();
  await T.loaded();
  await page.waitForTimeout(300);
  check((await T.edit()).crop === null && (await T.edit()).shape === 'free', 'the tool opens on the whole picture');
  await page.screenshot({ path: `${SHOTS}/crop-1-desktop-open.png` });

  // ---------------------------------------------------------------- shapes, box
  section('shapes, moving and resizing the box with the mouse');
  await T.shape('One light');
  let b = await T.box();
  let e = await T.edit();
  check(e.shape === 'light' && near(b.width, b.height, 2), `One light: a square box (${b.width.toFixed(0)}x${b.height.toFixed(0)})`);
  let pv = await T.preview();
  check(pv.w === 128 && pv.h === 128, 'the preview is one light at 128 x 128');
  // make room, then move
  await T.drag(await T.handle('se'), { x: (await T.handle('se')).x - 120, y: (await T.handle('se')).y - 120 });
  b = await T.box();
  const fr = await T.frame();
  const c0 = T.centre(b);
  await T.drag(c0, { x: c0.x + 40, y: c0.y + 25 });
  const b2 = await T.box();
  check(near(b2.x - b.x, 40, 3) || near(b2.x + b2.width, fr.x + fr.width, 2), `dragging moves the box (dx ${(b2.x - b.x).toFixed(1)})`);
  check(near(b2.y - b.y, 25, 3) || near(b2.y + b2.height, fr.y + fr.height, 2), `... and down (dy ${(b2.y - b.y).toFixed(1)})`);
  const before2 = b2.width;
  await T.drag(await T.handle('nw'), { x: (await T.handle('nw')).x - 30, y: (await T.handle('nw')).y - 10 });
  const b3 = await T.box();
  check(b3.width > before2 + 10 && near(b3.width, b3.height, 2), `resizing from a corner keeps 1:1 (${b3.width.toFixed(0)}x${b3.height.toFixed(0)})`);

  section('show circles: what the round light lets through');
  await T.circles(true);
  pv = await T.preview();
  const corner = pv.data.slice(0, 3);
  const mid = pv.data.slice((64 * 128 + 64) * 4, (64 * 128 + 64) * 4 + 3);
  check(corner.every((v) => v === 0), 'with circles, a corner of the light is black');
  await page.screenshot({ path: `${SHOTS}/crop-2-light-circles.png` });
  await T.circles(false);
  const pv2 = await T.preview();
  check(pv2.data.slice((64 * 128 + 64) * 4, (64 * 128 + 64) * 4 + 3).join() === mid.join(), 'the middle is the same with and without circles');
  const cornerOff = pv2.data.slice(0, 3);
  check(cornerOff.some((v) => v > 0) || corner.join() === cornerOff.join(), `without circles the corner shows the picture (${cornerOff.join(',')})`);
  await page.screenshot({ path: `${SHOTS}/crop-3-light-no-circles.png` });
  await T.circles(true);

  await T.shape('Whole sign');
  b = await T.box();
  e = await T.edit();
  pv = await T.preview();
  check(e.shape === 'sign' && near(b.width * 3, b.height, 3), `Whole sign: a 1:3 box (${b.width.toFixed(0)}x${b.height.toFixed(0)})`);
  check(pv.w === 128 && pv.h === 384, 'the preview is the whole sign at 128 x 384');
  await T.drag(await T.handle('sw'), { x: (await T.handle('sw')).x + 15, y: (await T.handle('sw')).y - 45 });
  b = await T.box();
  check(near(b.width * 3, b.height, 3), 'resizing keeps 1:3');
  await page.screenshot({ path: `${SHOTS}/crop-4-sign.png` });

  await T.shape('Free');
  const bf = await T.box();
  await T.drag(await T.handle('e'), { x: (await T.handle('e')).x + 50, y: (await T.handle('e')).y });
  const bf2 = await T.box();
  check(near(bf2.width - bf.width, 50, 3) && near(bf2.height, bf.height, 1), 'Free: an edge handle changes one side only');
  await page.screenshot({ path: `${SHOTS}/crop-5-free.png` });

  // ---------------------------------------------------------------- turning
  section('turning');
  const f0 = await T.frame();
  await page.click('[data-testid=crop-turn-right]');
  await page.waitForTimeout(200);
  const f1 = await T.frame();
  e = await T.edit();
  check(e.rotate === 90, 'turn right: 90 degrees');
  check((f0.width > f0.height) !== (f1.width > f1.height) || near(f0.width, f0.height, 2), `the picture turned (${f0.width.toFixed(0)}x${f0.height.toFixed(0)} -> ${f1.width.toFixed(0)}x${f1.height.toFixed(0)})`);
  await page.click('[data-testid=crop-turn-left]');
  await page.click('[data-testid=crop-turn-left]');
  await page.waitForTimeout(150);
  check((await T.edit()).rotate === 270, 'two turns left: 270 degrees');
  await page.click('[data-testid=crop-turn-right]');
  await page.click('[data-testid=crop-turn-right]');
  await page.waitForTimeout(150);
  check((await T.edit()).rotate === 90, 'and right again: 90');

  // ---------------------------------------------------------------- in and out
  section('in and out points');
  const dur = item.source_duration;
  const scrub = await page.locator('[data-testid=crop-scrub]').boundingBox();
  const inH = T.centre(await page.locator('[data-testid=crop-in-handle]').boundingBox());
  await T.drag(inH, { x: scrub.x + scrub.width * 0.2, y: inH.y });
  e = await T.edit();
  check(near(e.start, dur * 0.2, dur * 0.03), `dragging the in handle sets the in point (${e.start.toFixed(2)} of ${dur.toFixed(2)} s)`);
  const outV = Math.min(dur - 0.2, e.start + Math.max(1, dur * 0.4));
  await page.fill('[data-testid=crop-out]', outV.toFixed(2));
  await page.locator('[data-testid=crop-out]').press('Enter');
  e = await T.edit();
  check(near(e.end, outV, 0.011), `typing the out point (${e.end})`);
  await page.click('[data-testid=crop-play]');
  await page.waitForTimeout(Math.min(2500, (e.end - e.start) * 1000 + 600));
  const tPlay = await page.evaluate(() => document.querySelector('video.crop-source').currentTime);
  check(tPlay >= e.start - 0.05 && tPlay <= e.end + 0.05, `playback stays between in and out (at ${tPlay.toFixed(2)} s)`);
  await page.screenshot({ path: `${SHOTS}/crop-6-playing.png` });
  await page.click('[data-testid=crop-play]');

  // the final edit for the save: one light, turned 90 degrees, from the in point
  await T.shape('One light');
  await T.circles(true);
  const finalIn = Math.round((e.start + 0.25) * 100) / 100;
  await page.fill('[data-testid=crop-in]', finalIn.toFixed(2));
  await page.locator('[data-testid=crop-in]').press('Enter');
  await page.waitForFunction((t) => Math.abs(document.querySelector('video.crop-source').currentTime - t) < 0.02 && !document.querySelector('video.crop-source').seeking, finalIn);
  await page.waitForTimeout(400);
  const toolEdit = await T.edit();
  const shown = await T.preview();
  await page.screenshot({ path: `${SHOTS}/crop-7-before-save.png` });

  // ---------------------------------------------------------------- save
  section('save: the item, and the sign, show what the preview showed');
  await page.click('[data-testid=crop-save]');
  const status = page.locator('[data-testid=crop-status]');
  const sawProgress = await status.waitFor({ timeout: 3000 }).then(() => true, () => false);
  check(sawProgress, 'saving shows the conversion progress');
  await page.screenshot({ path: `${SHOTS}/crop-8-saving.png` });
  await page.locator('[data-testid=crop-tool]').waitFor({ state: 'detached', timeout: 60000 });
  const saved = await api(`/api/media/${item.id}`);
  check(saved.status === 'ready' && saved.version === 2 && !saved.error, 'the item is ready with its new version');
  check(saved.edit.rotate === toolEdit.rotate && saved.edit.shape === 'light' && near(saved.edit.start, toolEdit.start, 0.001) && near(saved.edit.end, toolEdit.end, 0.001),
    `the saved edit is the tool's (rotate ${saved.edit.rotate}, ${saved.edit.start}..${saved.edit.end} s)`);
  check(saved.edit.crop.every((v, i) => near(v, toolEdit.crop[i], 1e-5)), `... and so is the crop [${saved.edit.crop.map((v) => v.toFixed(3)).join(', ')}]`);
  check(near(saved.width, saved.height, 2) && near(saved.duration, toolEdit.end - toolEdit.start, 0.06), `the item is ${saved.width}x${saved.height}, ${saved.duration.toFixed(2)} s long`);
  await page.waitForTimeout(400);
  const note = await page.locator(`[data-media-card="${item.id}"] [data-testid=edit-note]`).textContent();
  check(note.startsWith('One light'), `the card says what was done ("${note}")`);
  await page.screenshot({ path: `${SHOTS}/crop-9-media-after.png` });

  const s1 = await makeSet('E2E crop light', { target: 'face1', type: 'video', duration: 3, params: { media: item.id, loop: true } });
  const server = await serverRows(page, s1.id, 0, 128, 256);
  const cmp = compare(shown.data, server);
  console.log(`        preview vs server frame: mean difference ${cmp.mad.toFixed(1)} / 255, correlation ${cmp.corr.toFixed(3)} (spread ${cmp.sd.toFixed(0)})`);
  await dumpPair(page, shown.data, server, 128, 128, `${SHOTS}/crop-compare-light.png`);
  check(cmp.mad < 14 && (cmp.flat || cmp.corr > 0.9), 'the sign shows what the preview showed (middle light, circles)');

  // ---------------------------------------------------------------- copy
  section('save as a copy: another crop of the same original');
  await page.locator(`[data-media-card="${item.id}"] [data-testid=crop-open]`).click();
  await T.loaded();
  check((await T.edit()).shape === 'light' && (await T.edit()).rotate === 90, 'the tool opens with the saved edit');
  await T.shape('Whole sign');
  await page.click('[data-testid=crop-reset]');         // the largest whole-sign box, upright, the whole video
  await T.circles(false);
  await page.waitForFunction(() => !document.querySelector('video.crop-source').seeking);
  await page.waitForTimeout(400);
  const copyEdit = await T.edit();
  const shownSign = await T.preview();
  await page.click('[data-testid=crop-save-copy]');
  await page.locator('[data-testid=crop-tool]').waitFor({ state: 'detached', timeout: 60000 });
  const all = await api('/api/media');
  const copy = all.find((m) => !before.has(m.id) && m.id !== item.id && m.name.includes('whole sign'));
  if (copy) made.media.add(copy.id);
  check(copy && copy.status === 'ready' && copy.edit.shape === 'sign', `a new item "${copy?.name}" holds the whole-sign crop`);
  check((await api(`/api/media/${item.id}`)).edit.shape === 'light', 'the first item keeps its one-light crop');
  if (copy) {
    check(copy.edit.crop.every((v, i) => near(v, copyEdit.crop[i], 1e-5)), 'the copy has the crop drawn in the tool');
    const s2 = await makeSet('E2E crop sign', { target: 'all', type: 'video', duration: 3, params: { media: copy.id, loop: true } });
    const srv = await serverRows(page, s2.id, 0, 0, 384);
    const c2 = compare(shownSign.data, srv);
    console.log(`        preview vs server frame: mean difference ${c2.mad.toFixed(1)} / 255, correlation ${c2.corr.toFixed(3)} (spread ${c2.sd.toFixed(0)})`);
    await dumpPair(page, shownSign.data, srv, 128, 384, `${SHOTS}/crop-compare-sign.png`);
    check(c2.mad < 14 && (c2.flat || c2.corr > 0.9), 'the whole sign shows what the preview showed (no circles)');
  }

  // ---------------------------------------------------------------- import from a link
  section('import from a link (the server\'s own /source: no internet needed)');
  await page.goto(`${BASE}/media`);
  const link = `${BASE}/api/media/${item.id}/source`;
  await page.fill('[data-testid=import-url]', link);
  await page.click('[data-testid=import-go]');
  let imported = null;
  for (let i = 0; i < 100 && !imported; i++) {
    imported = (await api('/api/media')).find((m) => m.source_url === link);
    if (!imported) await page.waitForTimeout(100);
  }
  check(!!imported, 'the import is an item at once');
  if (imported) {
    made.media.add(imported.id);
    const prog = await page.locator(`[data-media-card="${imported.id}"] [data-testid=media-progress]`).waitFor({ timeout: 5000 }).then(() => true, () => false);
    check(prog, 'its card shows progress while it downloads and converts');
    await page.locator(`[data-media-card="${imported.id}"] .fresh-offer`).waitFor({ timeout: 60000 }).catch(() => undefined);
    const im = await api(`/api/media/${imported.id}`);
    check(im.status === 'ready' && im.has_audio === item.has_audio && near(im.source_duration, item.source_duration, 0.1), `the imported item is ready (${im.status}${im.error ? ': ' + im.error : ''})`);
    check(await page.locator(`[data-media-card="${imported.id}"] .fresh-offer`).isVisible(), 'and the crop tool is offered');
    await page.screenshot({ path: `${SHOTS}/crop-10-imported.png` });
  }
  // a link that cannot be fetched ends as a readable error on its card, with a way to try again
  const dead = 'http://127.0.0.1:9/nothing.mp4';
  await page.fill('[data-testid=import-url]', dead);
  await page.click('[data-testid=import-go]');
  let failed = null;
  for (let i = 0; i < 150 && !(failed && failed.status === 'error'); i++) {
    failed = (await api('/api/media')).find((m) => m.source_url === dead);
    if (!(failed && failed.status === 'error')) await page.waitForTimeout(100);
  }
  if (failed) made.media.add(failed.id);
  check(failed?.status === 'error' && /could not be reached|could not be fetched/.test(failed.error), `a dead link ends as an error: "${failed?.error}"`);
  if (failed) {
    const card = page.locator(`[data-media-card="${failed.id}"]`);
    await card.locator('span.err').waitFor({ timeout: 5000 });
    check(await card.locator('button', { hasText: 'Try again' }).isVisible(), 'its card shows the error and "Try again"');
    await page.screenshot({ path: `${SHOTS}/crop-10b-import-error.png` });
    await card.locator('button', { hasText: 'Try again' }).click();
    const again = await waitItem(failed.id, (m) => m.status === 'processing', 3000);
    const end = await waitItem(failed.id, (m) => m.status !== 'processing', 30000);
    check(again.status === 'processing' && end.status === 'error' && end.source_url === dead, '"Try again" fetches the same link again (still dead: an error again)');
  }

  // ---------------------------------------------------------------- the editor's entry point
  section('timeline editor: crop the clip\'s media from the inspector');
  const cur = await api(`/api/media/${item.id}`);
  const s3 = await makeSet('E2E crop editor', { target: 'face0', type: 'video', duration: Math.round(cur.duration * 1000) / 1000, params: { media: item.id } });
  const clipId = s3.tracks.find((t) => t.target === 'face0').clips[0].id;
  // with Live off, opening the editor must not take the sign over, not even for a moment
  await page.evaluate(() => localStorage.setItem('trafficlight.editor.live', '0'));
  await page.goto(`${BASE}/editor/${s3.id}`);
  let tookOver = false;
  for (let i = 0; i < 15; i++) {
    if ((await api('/api/state')).now.source === 'editor') tookOver = true;
    await page.waitForTimeout(100);
  }
  check(!tookOver, 'with Live off the editor never takes the LEDs over (/ws/editor?live=0)');
  await page.evaluate(() => localStorage.setItem('trafficlight.editor.live', '1'));
  await page.reload();
  await page.waitForSelector(`[data-clip="${clipId}"]`);
  await page.locator(`[data-clip="${clipId}"]`).click();
  await page.locator('[data-testid=insp-crop]').click();
  await T.loaded();
  check(true, 'the inspector opens the crop tool for the clip\'s video');
  const newIn = Math.round((cur.edit.start + 0.5) * 100) / 100;
  await page.fill('[data-testid=crop-in]', newIn.toFixed(2));
  await page.locator('[data-testid=crop-in]').press('Enter');
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${SHOTS}/crop-11-from-editor.png` });
  await page.click('[data-testid=crop-save]');
  await page.locator('[data-testid=crop-tool]').waitFor({ state: 'detached', timeout: 60000 });
  const after = await api(`/api/media/${item.id}`);
  await page.waitForTimeout(500);
  const clipW = await page.evaluate((id) => {
    const pps = Number(document.querySelector('.tl-inner').dataset.pps);
    return parseFloat(document.querySelector(`[data-clip="${id}"]`).style.width) / pps;
  }, clipId);
  check(near(clipW, after.duration, 0.02), `the clip follows the new length (${clipW.toFixed(2)} s = ${after.duration.toFixed(2)} s)`);
  await page.screenshot({ path: `${SHOTS}/crop-12-editor-after.png` });
  await ctx.close();

  // ---------------------------------------------------------------- phone, touch
  section('phone: touch');
  const pctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
  const p = await pctx.newPage();
  p.on('pageerror', (er) => {
    failures++;
    console.log('  PAGE ERROR', er.message);
  });
  const P = tool(p);
  await p.goto(`${BASE}/media`);
  await p.locator(`[data-media-card="${item.id}"] [data-testid=crop-open]`).tap();
  await P.loaded();
  await p.waitForTimeout(300);
  await p.screenshot({ path: `${SHOTS}/crop-13-phone-open.png` });
  const cdp = await pctx.newCDPSession(p);
  const touch = (type, pts) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: pts.map(([x, y], i) => ({ x, y, id: i })) });
  const swipe = async (from, to) => {
    await touch('touchStart', [[from.x, from.y]]);
    for (let i = 1; i <= 10; i++) await touch('touchMove', [[from.x + ((to.x - from.x) * i) / 10, from.y + ((to.y - from.y) * i) / 10]]);
    await touch('touchEnd', []);
    await p.waitForTimeout(150);
  };
  await p.locator('[data-testid=crop-reset]').tap();
  await p.waitForTimeout(200);
  // smaller first (a corner, by touch), then move it
  const pb0 = await P.box();
  await swipe(await P.handle('se'), { x: (await P.handle('se')).x - 40, y: (await P.handle('se')).y - 40 });
  const pb1 = await P.box();
  check(pb1.width < pb0.width - 10 && near(pb1.width, pb1.height, 2), `touch on a corner resizes, still 1:1 (${pb0.width.toFixed(0)} -> ${pb1.width.toFixed(0)})`);
  await swipe(await P.handle('se'), { x: (await P.handle('se')).x - 400, y: (await P.handle('se')).y - 400 });
  const pbMin = await P.box();
  check(pbMin.width >= 39 && pbMin.width < 60, `the box cannot shrink below a finger's width (${pbMin.width.toFixed(0)} px)`);
  await swipe(await P.handle('se'), { x: (await P.handle('se')).x + 60, y: (await P.handle('se')).y + 60 });
  const pb1b = await P.box();
  const pc = P.centre(pb1b);
  await swipe(pc, { x: pc.x + 20, y: pc.y + 30 });
  const pb2 = await P.box();
  check(Math.abs(pb2.x - pb1b.x) > 5 || Math.abs(pb2.y - pb1b.y) > 5, 'touch drags the box');
  await p.locator('.crop-circles [role=switch]').tap();
  await p.waitForTimeout(150);
  check((await p.locator('.crop-circles [role=switch]').getAttribute('aria-checked')) === 'false', 'the circles switch works by touch');
  await p.screenshot({ path: `${SHOTS}/crop-14-phone-touched.png` });
  await p.locator('.crop-circles [role=switch]').tap();
  const sq = await P.box();
  await p.locator('.crop-side .segmented button', { hasText: 'Whole sign' }).first().tap();
  await p.waitForTimeout(250);
  const tall = await P.box();
  check(near(tall.height, tall.width * 3, 3) && tall.width >= 39 && tall.height >= sq.height - 3,
    `switching to the whole sign keeps the box's height, at least a finger wide: ${sq.width.toFixed(0)}x${sq.height.toFixed(0)} -> ${tall.width.toFixed(0)}x${tall.height.toFixed(0)}`);
  await p.screenshot({ path: `${SHOTS}/crop-15-phone-sign.png` });
  await p.evaluate(() => document.querySelector('.crop-tool').scrollTo(0, 10000));
  await p.waitForTimeout(150);
  await p.screenshot({ path: `${SHOTS}/crop-16-phone-bottom.png` });
  await p.locator('.crop-actions button', { hasText: 'Cancel' }).tap();
  check((await api(`/api/media/${item.id}`)).edit.shape === 'light', 'Cancel changes nothing');
  await pctx.close();
} finally {
  await browser.close();
  for (const id of made.sets) await api(`/api/sets/${id}`, { method: 'DELETE' }).catch(() => undefined);
  if (!process.env.TL_KEEP) for (const id of made.media) await api(`/api/media/${id}?force=1`, { method: 'DELETE' }).catch(() => undefined);
}

console.log(failures ? `\n${failures} check(s) FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
