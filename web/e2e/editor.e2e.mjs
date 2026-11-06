// End-to-end test of the timeline editor against a running server, in Microsoft Edge
// (playwright-core, channel "msedge": no browser download).
//
//   node e2e/editor.e2e.mjs            (server at TL_URL, default http://localhost:8091)
//
// The server needs at least one ready video and one ready audio item in its media
// library for the media flows (a copy of the video is cropped for the whole sign and
// deleted again). Screenshots go to TL_SHOTS (default e2e/shots).
import { mkdirSync, writeFileSync } from 'node:fs';
import { chromium } from 'playwright-core';

const BASE = process.env.TL_URL ?? 'http://localhost:8091';
const SHOTS = process.env.TL_SHOTS ?? 'e2e/shots';
mkdirSync(SHOTS, { recursive: true });

let failures = 0;
const check = (cond, msg) => {
  if (cond) console.log('  ok   ', msg);
  else {
    failures++;
    console.log('  FAIL ', msg);
  }
};
const near = (a, b, tol = 0.02) => Math.abs(a - b) <= tol;
const section = (s) => console.log(`\n${s}`);

async function api(path, init = {}) {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...init });
  if (!r.ok) throw new Error(`${init.method ?? 'GET'} ${path}: ${r.status} ${await r.text()}`);
  return r.json();
}

// ---------------------------------------------------------------- page helpers
function helpers(page) {
  const geom = () => page.evaluate(() => {
    const inner = document.querySelector('.tl-inner');
    const lanes = [...document.querySelectorAll('.tl-lane')].map((l) => {
      const r = l.getBoundingClientRect();
      return { x: r.left, y: r.top, h: r.height };
    });
    const ruler = document.querySelector('.tl-ruler').getBoundingClientRect();
    const sc = document.querySelector('.tl-scroll');
    return { pps: Number(inner.dataset.pps), lanes, ruler: { x: ruler.left, y: ruler.top, h: ruler.height }, scrollLeft: sc.scrollLeft };
  });
  const at = async (row, t) => {
    const g = await geom();
    return { x: g.lanes[row].x + t * g.pps, y: g.lanes[row].y + g.lanes[row].h / 2 };
  };
  const rulerAt = async (t) => {
    const g = await geom();
    return { x: g.ruler.x + t * g.pps - g.scrollLeft, y: g.ruler.y + g.ruler.h / 2 };
  };
  /** Clips as drawn: `row` is the row index, or "whole" for a block over the light rows. */
  const clips = () => page.evaluate(() => {
    const pps = Number(document.querySelector('.tl-inner').dataset.pps);
    const head = document.querySelector('.tl-head')?.getBoundingClientRect().width ?? 0;
    const info = (c, row, x0) => ({
      id: c.dataset.clip, type: c.dataset.type, row,
      start: (parseFloat(c.style.left) - x0) / pps, dur: parseFloat(c.style.width) / pps,
      sel: c.classList.contains('sel'), bad: c.classList.contains('bad'), dim: c.classList.contains('dim'), text: c.textContent,
      wave: !!c.querySelector('.clip-wave path'), thumb: !!c.querySelector('.clip-thumb'),
    });
    return [
      ...[...document.querySelectorAll('.tl-row')].flatMap((row, i) => [...row.querySelectorAll('[data-clip]')].map((c) => info(c, i, 0))),
      ...[...document.querySelectorAll('.tl-inner > [data-whole][data-clip]')].map((c) => info(c, 'whole', head)),
    ];
  });
  /** Bounding boxes of the rows' lanes (client coords). */
  const lanes = () => page.evaluate(() => [...document.querySelectorAll('.tl-lane')].map((l) => {
    const r = l.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left };
  }));
  const clipCenter = async (id, frac = 0.5) => {
    const b = await page.locator(`[data-clip="${id}"]`).boundingBox();
    return { x: b.x + b.width * frac, y: b.y + b.height / 2 };
  };
  const clipEdge = async (id, edge) => {
    const b = await page.locator(`[data-clip="${id}"]`).boundingBox();
    return { x: edge === 'start' ? b.x + 2 : b.x + b.width - 2, y: b.y + b.height / 2 };
  };
  /** Centre of a palette item, scrolled into view first. */
  const item = async (sel) => {
    const loc = page.locator(sel).first();
    await loc.scrollIntoViewIfNeeded();
    const b = await loc.boundingBox();
    return { x: b.x + Math.min(30, b.width / 2), y: b.y + b.height / 2 };
  };
  const drag = async (from, to, { steps = 14, shift = false, shiftFirst = false } = {}) => {
    await page.mouse.move(from.x, from.y);
    if (shiftFirst) await page.keyboard.down('Shift');
    await page.mouse.down();
    if (shift && !shiftFirst) await page.keyboard.down('Shift');
    await page.mouse.move(to.x, to.y, { steps });
    await page.mouse.up();
    if (shift || shiftFirst) await page.keyboard.up('Shift');
    await page.waitForTimeout(60);
  };
  const time = async () => parseFloat(await page.locator('[data-testid=time-seconds]').textContent());
  /** Mean RGB of the preview canvas rows [y0, y1) (canvas pixels, 128 x 384). */
  const mean = (y0, y1) => page.evaluate(([a, b]) => {
    const c = document.querySelector('.ed-preview canvas');
    const d = c.getContext('2d').getImageData(0, a, c.width, b - a).data;
    let r = 0, g = 0, bl = 0;
    for (let i = 0; i < d.length; i += 4) { r += d[i]; g += d[i + 1]; bl += d[i + 2]; }
    const n = d.length / 4;
    return [r / n, g / n, bl / n];
  }, [y0, y1]);
  const frameHash = () => page.evaluate(() => {
    const c = document.querySelector('.ed-preview canvas');
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    let h = 0;
    for (let i = 0; i < d.length; i += 4 * 3) h = (h * 31 + d[i] + d[i + 1] * 3 + d[i + 2] * 7) >>> 0;
    return h;
  });
  const waitFor = async (fn, ms = 3000, every = 50) => {
    const t0 = Date.now();
    for (;;) {
      const v = await fn();
      if (v) return v;
      if (Date.now() - t0 > ms) return v;
      await page.waitForTimeout(every);
    }
  };
  return { item, geom, at, rulerAt, clips, lanes, clipCenter, clipEdge, drag, time, mean, frameHash, waitFor };
}

/** One frame of a stored set as RGBA rows y0..y1 (rendered by the server). */
async function serverRows(page, setId, t, y0, y1) {
  return page.evaluate(async ([id, tt, a, b]) => {
    const bmp = await createImageBitmap(await (await fetch(`/api/render.png?set=${id}&t=${tt}&nocache=${Math.random()}`)).blob());
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
  const sd = Math.sqrt(va / xa.length) / 3;
  return { mad: sum / n, corr: va && vb ? cov / Math.sqrt(va * vb) : 1, flat: sd < 8, sd };
}

/** Two RGBA pictures side by side, for looking at a comparison. */
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

// ---------------------------------------------------------------- setup
const media = await api('/api/media');
// the shortest video: its clip stays well inside the 20 s set
const video = media.filter((m) => m.kind === 'video' && m.status === 'ready' && m.duration > 2).sort((a, b) => a.duration - b.duration)[0];
const sound = media.find((m) => m.kind === 'audio' && m.status === 'ready');
console.log(`media: video=${video?.name ?? '-'} audio=${sound?.name ?? '-'}`);

/** A copy of `video` cropped to the whole sign (1:3 from the middle), ready. */
async function makeSignVideo(v) {
  const copy = await api(`/api/media/${v.id}/duplicate`, { method: 'POST', body: JSON.stringify({ name: 'E2E whole sign' }) });
  const W = v.source_width;
  const H = v.source_height;
  const crop = W / H > 1 / 3 ? [(1 - H / 3 / W) / 2, 0, H / 3 / W, 1] : [0, (1 - (3 * W) / H) / 2, 1, (3 * W) / H];
  await api(`/api/media/${copy.id}`, { method: 'PATCH', body: JSON.stringify({ edit: { ...v.edit, crop, shape: 'sign' } }) });
  for (let i = 0; i < 300; i++) {
    const m = await api(`/api/media/${copy.id}`);
    if (m.status === 'ready' && m.edit.shape === 'sign' && m.version > v.version) return m;
    if (m.status === 'error') throw new Error(`whole-sign crop failed: ${m.error}`);
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error('whole-sign crop did not finish');
}
const signVideo = video ? await makeSignVideo(video) : null;
if (signVideo) console.log(`whole-sign copy ${signVideo.id}: ${signVideo.width}x${signVideo.height}, ${signVideo.duration.toFixed(2)} s`);
const set = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E editor', bpm: 120, duration: 20 }) });
console.log(`set ${set.id}`);

const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await ctx.newPage();
  page.on('dialog', (d) => d.accept());
  page.on('pageerror', (e) => {
    failures++;
    console.log('  PAGE ERROR', e.message);
  });
  const h = helpers(page);
  const ROW = { top: 0, mid: 1, bottom: 2, audio: 3 };

  section('open');
  await page.goto(`${BASE}/editor/${set.id}`);
  await page.waitForSelector('.tl-row');
  check((await page.locator('.tl-row').count()) === 4, 'four rows: the three lights and audio (the whole-sign track has none)');
  check(!(await page.locator('.tl-name', { hasText: 'Whole sign' }).count()), 'no "Whole sign" row header');
  await page.click('.tl-add');
  check(!(await page.locator('.popover button', { hasText: 'Whole sign' }).count()), 'the add-track menu offers no whole-sign track');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  await page.locator('[data-testid=snap]').getAttribute('aria-pressed').then((v) => v === 'false' && page.click('[data-testid=snap]'));

  section('palette drops');
  await h.drag(await h.item('.pal-chip[data-type="text"]'), await h.at(ROW.top, 1.05));
  let cs = await h.clips();
  const text1 = cs.find((c) => c.type === 'text');
  check(text1 && text1.row === ROW.top, 'text dropped on the top light');
  check(text1 && near(text1.start, 1) && near(text1.dur, 4), `text snapped to beat 1.0 s with its default 4 s (got ${text1?.start.toFixed(3)}, ${text1?.dur})`);
  check((await page.locator('.insp-title').textContent()) === 'Text', 'inspector shows the new text clip');

  await h.drag(await h.item('.pal-chip[data-type="fx:pulse"]'), await h.at(ROW.mid, 2.02));
  cs = await h.clips();
  const pulse = cs.find((c) => c.type === 'fx:pulse');
  check(pulse && pulse.row === ROW.mid && near(pulse.start, 2), 'pulse dropped on the middle light at 2 s');

  // A text cannot go on the audio track: the row is marked and the drop does nothing.
  {
    const chip = await h.item('.pal-chip[data-type="text"]');
    await page.mouse.move(chip.x, chip.y);
    await page.mouse.down();
    const p = await h.at(ROW.audio, 12);
    await page.mouse.move(p.x, p.y, { steps: 10 });
    const rowCls = await page.locator('.tl-row').nth(ROW.audio).getAttribute('class');
    const topCls = await page.locator('.tl-row').nth(ROW.top).getAttribute('class');
    await page.mouse.up();
    check(rowCls.includes('drop-no') && topCls.includes('drop-ok'), 'during a text drag the audio row is not a target, the lights are');
    check((await h.clips()).length === 2, 'drop on an incompatible track adds nothing');
  }

  if (sound) {
    await h.drag(await h.item(`[data-media="${sound.id}"]`), await h.at(ROW.audio, 0));
    await page.waitForTimeout(600);
    cs = await h.clips();
    const a = cs.find((c) => c.type === 'audio');
    check(a && a.row === ROW.audio && near(a.dur, sound.duration, 0.01), 'song on the audio track gets its duration');
    check(a?.wave, 'audio clip draws a waveform from /peaks');
  } else console.log('  skip  no ready audio in the library');
  await page.screenshot({ path: `${SHOTS}/desktop-1-drops.png` });

  section('move, snap, overlap');
  await h.drag(await h.clipCenter(text1.id), await h.clipCenter(text1.id).then(async (p) => ({ x: p.x + (await h.geom()).pps * 1.27, y: p.y })));
  let t1 = (await h.clips()).find((c) => c.id === text1.id);
  check(near(t1.start, 2.5), `move snaps to the beat grid (1 + 1.27 -> 2.5, got ${t1.start.toFixed(3)})`);
  await h.drag(await h.clipCenter(text1.id), await h.clipCenter(text1.id).then(async (p) => ({ x: p.x + (await h.geom()).pps * 0.37, y: p.y })), { shift: true });
  t1 = (await h.clips()).find((c) => c.id === text1.id);
  check(near(t1.start, 2.87, 0.03), `Shift suspends snapping (2.5 + 0.37, got ${t1.start.toFixed(3)})`);
  await page.click('[data-testid=snap]');
  await h.drag(await h.clipCenter(text1.id), await h.clipCenter(text1.id).then(async (p) => ({ x: p.x - (await h.geom()).pps * 0.61, y: p.y })));
  t1 = (await h.clips()).find((c) => c.id === text1.id);
  check(near(t1.start, 2.26, 0.03), `snapping toggled off (2.87 - 0.61, got ${t1.start.toFixed(3)})`);
  await page.click('[data-testid=snap]');
  // back onto the grid at 1 s
  await h.drag(await h.clipCenter(text1.id), await h.clipCenter(text1.id).then(async (p) => ({ x: p.x - (await h.geom()).pps * 1.26, y: p.y })));
  t1 = (await h.clips()).find((c) => c.id === text1.id);
  check(near(t1.start, 1), `back to 1 s (got ${t1.start.toFixed(3)})`);

  // second text after the first, then try to drag it over the first
  await page.mouse.click(...Object.values(await h.at(ROW.top, 7.02)));
  check(await page.locator('.tl-row').nth(ROW.top).locator('.tl-insert').count() === 1, 'clicking an empty spot sets the insertion point');
  await page.click('.pal-chip[data-type="text"]');
  cs = await h.clips();
  const text2 = cs.find((c) => c.type === 'text' && c.id !== text1.id);
  check(text2 && near(text2.start, 7) && text2.row === ROW.top, 'click on a library item adds it at the insertion point');
  await h.drag(await h.clipCenter(text2.id, 0.2), await h.at(ROW.top, 3.5));
  cs = await h.clips();
  const a1 = cs.find((c) => c.id === text1.id);
  const a2 = cs.find((c) => c.id === text2.id);
  check(a2.start >= a1.start + a1.dur - 1e-6 || a2.start + a2.dur <= a1.start + 1e-6, `dragging onto a clip never overlaps (got ${a2.start.toFixed(2)}..${(a2.start + a2.dur).toFixed(2)} vs ${a1.start}..${a1.start + a1.dur})`);
  check(near(a2.start, 5), `it butts up against the neighbour (5 s, got ${a2.start.toFixed(3)})`);

  // move to another compatible track: text to the bottom light
  await h.drag(await h.clipCenter(text2.id), await h.at(ROW.bottom, 6.6));
  cs = await h.clips();
  check(cs.find((c) => c.id === text2.id).row === ROW.bottom, 'clip moved to another light');
  await h.drag(await h.clipCenter(text2.id), await h.at(ROW.audio, 6.6));
  check((await h.clips()).find((c) => c.id === text2.id).row === ROW.bottom, 'a text clip cannot be moved onto the audio track');
  await page.screenshot({ path: `${SHOTS}/desktop-2-moves.png` });

  section('trim');
  await h.drag(await h.clipEdge(text1.id, 'end'), await h.clipEdge(text1.id, 'end').then(async (p) => ({ x: p.x + (await h.geom()).pps * 1.02, y: p.y })));
  t1 = (await h.clips()).find((c) => c.id === text1.id);
  check(near(t1.start, 1) && near(t1.dur, 5), `end trim +1 s (dur ${t1.dur.toFixed(3)})`);
  await h.drag(await h.clipEdge(text1.id, 'start'), await h.clipEdge(text1.id, 'start').then(async (p) => ({ x: p.x + (await h.geom()).pps * 0.5, y: p.y })));
  t1 = (await h.clips()).find((c) => c.id === text1.id);
  check(near(t1.start, 1.5) && near(t1.dur, 4.5), `start trim keeps the end (start ${t1.start.toFixed(3)}, dur ${t1.dur.toFixed(3)})`);
  await h.drag(await h.clipEdge(text1.id, 'start'), await h.at(ROW.top, 0));
  t1 = (await h.clips()).find((c) => c.id === text1.id);
  check(near(t1.start, 0) && near(t1.dur, 6), 'start trim to 0');

  section('split, delete, undo, redo');
  await h.drag(await h.rulerAt(4.0), await h.rulerAt(4.0));
  check(near(await h.time(), 4, 0.03), 'click on the ruler moves the playhead');
  await page.mouse.click(...Object.values(await h.clipCenter(pulse.id)));
  await page.keyboard.press('s');
  cs = await h.clips();
  const mids = cs.filter((c) => c.row === ROW.mid).sort((a, b) => a.start - b.start);
  check(mids.length === 2 && near(mids[0].dur, 2) && near(mids[1].start, 4), 'S splits the selected clip at the playhead');
  await page.mouse.click(...Object.values(await h.clipCenter(mids[1].id)));
  await page.keyboard.press('Delete');
  check((await h.clips()).filter((c) => c.row === ROW.mid).length === 1, 'Delete removes the selected clip');
  await page.keyboard.press('Control+z');
  check((await h.clips()).filter((c) => c.row === ROW.mid).length === 2, 'Ctrl+Z undoes the delete');
  await page.keyboard.press('Control+z');
  check((await h.clips()).filter((c) => c.row === ROW.mid).length === 1, 'Ctrl+Z undoes the split');
  await page.keyboard.press('Control+Shift+z');
  await page.keyboard.press('Control+Shift+z');
  check((await h.clips()).filter((c) => c.row === ROW.mid).length === 1, 'Ctrl+Shift+Z redoes split and delete');
  await page.click('button[aria-label=Undo]');
  check((await h.clips()).filter((c) => c.row === ROW.mid).length === 2, 'the Undo button works');
  await page.click('button[aria-label=Redo]');

  section('copy, paste, duplicate, multi-select');
  await page.mouse.click(...Object.values(await h.clipCenter(text2.id)));
  await page.keyboard.press('Control+d');
  cs = await h.clips();
  const bottoms = cs.filter((c) => c.row === ROW.bottom).sort((a, b) => a.start - b.start);
  check(bottoms.length === 2 && near(bottoms[1].start, bottoms[0].start + bottoms[0].dur), 'Ctrl+D duplicates right after the clip');
  await page.keyboard.press('Control+c');
  await h.drag(await h.rulerAt(15), await h.rulerAt(15));
  await page.keyboard.press('Control+v');
  cs = await h.clips();
  check(cs.filter((c) => c.row === ROW.bottom).length === 3 && cs.some((c) => c.row === ROW.bottom && near(c.start, 15)), 'Ctrl+V pastes at the playhead');
  // marquee over the bottom row from 0 to 20 s
  {
    const p0 = await h.at(ROW.bottom, 0.2);
    const p1 = await h.at(ROW.bottom, 19.5);
    await h.drag({ x: p0.x, y: p0.y - 20 }, { x: p1.x, y: p1.y + 10 });
    check((await page.locator('.insp-title').textContent()).startsWith('3 clips'), 'a box drag selects the clips it touches');
    await page.keyboard.press('Delete');
    check((await h.clips()).filter((c) => c.row === ROW.bottom).length === 0, 'Delete removes all selected');
    await page.keyboard.press('Control+z');
  }

  section('inspector edits change the preview');
  await page.keyboard.press('Escape');
  await h.drag(await h.rulerAt(2.2), await h.rulerAt(2.2));
  await page.waitForTimeout(400);
  {
    const h0 = await h.frameHash();
    await page.locator('.tl-row').nth(ROW.mid).locator('.tl-mute').click();
    check(await h.waitFor(async () => (await h.frameHash()) !== h0), 'muting the middle track changes the preview');
    await page.locator('.tl-row').nth(ROW.mid).locator('.tl-mute').click();
  }
  await page.mouse.click(...Object.values(await h.clipCenter(text1.id)));
  await page.waitForTimeout(500);
  const before = await h.frameHash();
  const ta = page.locator('.insp textarea');
  await ta.fill('HI');
  check(await h.waitFor(async () => (await h.clips()).find((c) => c.id === text1.id).text.includes('HI')), 'the clip shows its new text');
  check(await h.waitFor(async () => (await h.frameHash()) !== before), 'typing text changes the preview frame');
  // background colour of the text: pick blue, the top light turns blue
  const bgPicker = page.locator('.insp-row', { hasText: 'Background' }).locator('input[type=color]').first();
  if (await bgPicker.count()) {
    await bgPicker.fill('#0000ff');
    const blue = await h.waitFor(async () => {
      const [r, g, b] = await h.mean(20, 108);
      return b > 60 && b > r * 3 ? [r, g, b] : null;
    });
    check(!!blue, `picking a blue background turns the top light blue (${blue?.map((x) => x.toFixed(0))})`);
    await page.locator('.insp-row', { hasText: 'Background' }).locator('button', { hasText: 'Light' }).click();
    const red = await h.waitFor(async () => {
      const [r, , b] = await h.mean(20, 108);
      return r > 60 && r > b * 3;
    });
    check(red, '"Light" colour makes it the light\'s own colour (red) again');
  } else check(false, 'text clip has a background colour control');
  const font = page.locator('.insp-row', { hasText: 'Font' }).locator('select');
  const nOpts = await font.locator('option').count();
  const h0 = await h.frameHash();
  await font.selectOption({ index: nOpts - 1 });
  check(await h.waitFor(async () => (await h.frameHash()) !== h0), 'choosing another font in a select changes the preview');

  section('keys in fields do not trigger shortcuts');
  await ta.click();
  const nClips = (await h.clips()).length;
  await page.keyboard.press('End');
  await page.keyboard.type(' X');
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  check((await h.clips()).length === nClips, 'Delete/Backspace while typing do not delete clips');
  check((await page.locator('[data-testid=play]').getAttribute('aria-label')) === 'Play', 'Space while typing does not start playback');
  check((await ta.inputValue()) === 'HI ', `the field got the keys ("${await ta.inputValue()}")`);

  section('problem badge');
  await ta.fill('THIS TEXT IS MUCH TOO LONG TO FIT INSIDE ONE ROUND LIGHT, EVEN AT THE VERY SMALLEST FONT SIZE THAT THE SIGN HAS AVAILABLE FOR TEXT');
  check(await h.waitFor(async () => (await h.clips()).find((c) => c.id === text1.id).bad), 'the clip gets a warning badge');
  check(await h.waitFor(async () => (await page.locator('[data-testid=clip-problems]').count()) > 0), 'the inspector shows the message');
  console.log('        message:', (await page.locator('[data-testid=clip-problems]').textContent()).slice(0, 90));
  check(await page.locator('[data-testid=problems]').isVisible(), 'the toolbar shows a problems count');
  await page.screenshot({ path: `${SHOTS}/desktop-3-problem.png` });
  await ta.fill('GO');
  check(await h.waitFor(async () => !(await h.clips()).find((c) => c.id === text1.id).bad), 'the badge goes away when fixed');
  await page.keyboard.press('Escape');
  await page.mouse.click(...Object.values(await h.at(ROW.audio, 19)));

  section('play, loop, scrub');
  await h.drag(await h.rulerAt(0), await h.rulerAt(0));
  await page.keyboard.press('Space');
  await page.waitForTimeout(1500);
  const tPlay = await h.time();
  check(tPlay > 1.0 && tPlay < 2.2, `Space plays: time advances (${tPlay})`);
  await page.keyboard.press('Space');
  const tPaused = await h.time();
  await page.waitForTimeout(500);
  check(near(await h.time(), tPaused, 0.05), 'Space pauses');
  // shift-drag on the ruler: loop 1..2.5 s
  await h.drag(await h.rulerAt(1), await h.rulerAt(2.5), { shiftFirst: true });
  check((await page.locator('.tp-range').textContent()).includes('1.00–2.50'), 'shift-drag on the ruler sets a loop range');
  await page.click('[data-testid=play]');
  const seen = [];
  for (let i = 0; i < 40; i++) {
    seen.push(await h.time());
    await page.waitForTimeout(80);
  }
  await page.click('[data-testid=play]');
  const wrapped = seen.some((t, i) => i > 0 && t < seen[i - 1] - 0.5);
  const inRange = seen.slice(4).every((t) => t >= 0.95 && t <= 2.55);
  check(wrapped && inRange, `the loop wraps inside its range (min ${Math.min(...seen.slice(4)).toFixed(2)}, max ${Math.max(...seen).toFixed(2)})`);
  await page.click('button[aria-label="Clear loop range"]');
  // scrub: drag along the ruler; the time follows at once, the frame follows
  {
    const p0 = await h.rulerAt(0.5);
    const p1 = await h.rulerAt(5.5);
    await page.mouse.move(p0.x, p0.y);
    await page.mouse.down();
    const hA = await h.frameHash();
    await page.mouse.move(p1.x, p1.y, { steps: 20 });
    const tScrub = await h.time();
    check(near(tScrub, 5.5, 0.05), `scrubbing moves the playhead immediately (${tScrub})`);
    const changed = await h.waitFor(async () => (await h.frameHash()) !== hA, 1500, 20);
    await page.mouse.up();
    check(changed, 'the preview follows the scrub');
  }

  section('tracks');
  await page.click('.tl-add');
  await page.click('.popover button:has-text("Middle light")');
  check((await page.locator('.tl-row').count()) === 5, 'a middle-light track is added');
  const newRow = page.locator('.tl-row').nth(2);
  check((await newRow.locator('.tl-name').textContent()) === 'Middle 2', 'it goes after the middle track, named "Middle 2"');
  await newRow.locator('.tl-name').dblclick();
  await page.keyboard.press('Control+a');
  await page.keyboard.type('Sparkles');
  await page.keyboard.press('Enter');
  check((await newRow.locator('.tl-name').textContent()) === 'Sparkles', 'double-click renames');
  await newRow.locator('.tl-mute').click();
  check((await newRow.getAttribute('class')).includes('muted'), 'M mutes the track');
  await newRow.locator('.tl-more').click();
  await page.click('.popover button:has-text("Move up")');
  check((await page.locator('.tl-row').nth(1).locator('.tl-name').textContent()) === 'Sparkles', 'Move up reorders');
  // the audio row cannot be dragged above the lights (whole-sign blocks need the lights together)
  {
    const grip = await page.locator('.tl-row').nth(4).locator('.tl-grip').boundingBox();
    const top = await page.locator('.tl-row').nth(0).boundingBox();
    await h.drag({ x: grip.x + 5, y: grip.y + grip.height / 2 }, { x: grip.x + 5, y: top.y + 10 });
    check((await page.locator('.tl-row').nth(4).locator('.tl-name').textContent()) === 'Audio', 'the audio row stays below the lights');
  }
  // drag the grip of "Sparkles" to the top row
  {
    const grip = await page.locator('.tl-row').nth(1).locator('.tl-grip').boundingBox();
    const top = await page.locator('.tl-row').nth(0).boundingBox();
    await h.drag({ x: grip.x + 5, y: grip.y + grip.height / 2 }, { x: grip.x + 5, y: top.y + 10 });
    check((await page.locator('.tl-row').nth(0).locator('.tl-name').textContent()) === 'Sparkles', 'dragging the grip reorders (top = top layer)');
  }
  await page.locator('.tl-row').nth(0).locator('.tl-more').click();
  await page.click('.popover button:has-text("Delete track")');
  check((await page.locator('.tl-row').count()) === 4, 'an empty track is deleted');

  section('zoom');
  {
    const p0 = (await h.geom()).pps;
    await page.click('button[aria-label="Zoom in"]');
    const p1 = (await h.geom()).pps;
    const lane = await h.at(ROW.top, 3);
    await page.mouse.move(lane.x, lane.y);
    await page.keyboard.down('Control');
    await page.mouse.wheel(0, -300);
    await page.keyboard.up('Control');
    await page.waitForTimeout(100);
    const p2 = (await h.geom()).pps;
    const lane2 = await h.at(ROW.top, 3);
    check(p1 > p0 * 1.4 && p2 > p1 * 1.5, `zoom button and ctrl+wheel zoom in (${p0.toFixed(1)} -> ${p1.toFixed(1)} -> ${p2.toFixed(1)} px/s)`);
    check(Math.abs(lane2.x - lane.x) < 3, 'ctrl+wheel keeps the time under the pointer still');
    await page.keyboard.press('0');
    await page.waitForTimeout(100);
    check((await h.geom()).pps < p1, '0 fits the set');
  }

  section('live toggle');
  {
    const st1 = await api('/api/state');
    await page.click('.tp-live .toggle');
    await page.waitForTimeout(400);
    const st2 = await api('/api/state');
    await page.click('.tp-live .toggle');
    await page.waitForTimeout(400);
    const st3 = await api('/api/state');
    check(st1.now.source === 'editor' && st2.now.source !== 'editor' && st3.now.source === 'editor', `live on/off hands the sign to the editor and back (${st1.now.source} -> ${st2.now.source} -> ${st3.now.source})`);
  }

  section('library search, tap tempo, media param');
  await page.fill('.pal-search', 'puls');
  check((await page.locator('.pal-chip').count()) === 1 && (await page.locator('.pal-chip').first().getAttribute('data-type')) === 'fx:pulse', 'search filters the clip types');
  if (video) {
    await page.fill('.pal-search', video.name.slice(0, 6));
    check((await page.locator('.pal-media-item').count()) >= 1 && (await page.locator('.pal-chip').count()) === 0, 'and the media library');
  }
  await page.fill('.pal-search', '');
  await page.keyboard.press('Escape');
  await page.mouse.click(...Object.values(await h.at(ROW.audio, 19.5)));
  for (let i = 0; i < 6; i++) {
    await page.click('.tap-btn');
    await page.waitForTimeout(380);
  }
  const tapped = Number(await page.locator('#s-bpm').inputValue());
  check(tapped > 125 && tapped < 170, `tap tempo sets the bpm (${tapped})`);
  await page.fill('#s-bpm', '120');
  check(Number(await page.locator('#s-bpm').inputValue()) === 120, 'typing a bpm');

  section('focus and keys');
  await page.click('[data-testid=play]');
  await page.waitForTimeout(300);
  await page.keyboard.press('Space');
  await page.waitForTimeout(200);
  check((await page.locator('[data-testid=play]').getAttribute('aria-label')) === 'Play', 'Space with the play button focused pauses (no double toggle)');

  {
    const tx = (await h.clips()).find((c) => c.type === 'text' && c.row === ROW.top);
    await page.mouse.click(...Object.values(await h.clipCenter(tx.id)));
    await page.keyboard.press('ArrowRight');
    let n = (await h.clips()).find((c) => c.id === tx.id);
    check(near(n.start, tx.start + 0.5), `ArrowRight nudges by a beat (${tx.start} -> ${n.start})`);
    await page.keyboard.press('ArrowDown');
    n = (await h.clips()).find((c) => c.id === tx.id);
    check(n.row === ROW.mid, 'ArrowDown moves it to the next light');
    await page.keyboard.press('Control+z');
    await page.keyboard.press('Control+z');
    await page.fill('#c-start', '0.25');
    await page.fill('#c-dur', '3');
    await page.fill('#c-fi', '1');
    n = (await h.clips()).find((c) => c.id === tx.id);
    check(near(n.start, 0.25) && near(n.dur, 3), `typing start and length in the inspector (${n.start}, ${n.dur})`);
    check((await page.locator(`[data-clip="${tx.id}"] .clip-fade.in`).count()) === 1, 'a fade-in is drawn on the clip');
    await page.locator('.insp-row', { hasText: 'Mask' }).locator('button', { hasText: 'None' }).click();
    check((await page.locator('.insp-row', { hasText: 'Mask' }).locator('button.active').textContent()) === 'None', 'mask choice');
    await page.locator('#c-start').blur();
  }

  section('second editor window');
  {
    const p2 = await ctx.newPage();
    p2.on('dialog', (d) => d.accept());
    await p2.goto(`${BASE}/editor/${set.id}`);
    await p2.waitForSelector('.tl-row');
    check(await h.waitFor(async () => (await page.locator('.ed-banner').count()) > 0, 4000), 'the first window is told another editor took over');
    await page.waitForTimeout(1500);
    check((await page.locator('.ed-banner').count()) > 0, 'and does not grab the session back by itself');
    await p2.close();
    await page.click('.ed-banner button');
    check(await h.waitFor(async () => (await page.locator('.ed-banner').count()) === 0, 4000), '"Take over again" reconnects');
    check(await h.waitFor(async () => (await api('/api/state')).now.set_id === set.id, 3000), 'and the draft is live again');
  }

  section('save, reload');
  check(await page.locator('[data-testid=dirty]').isVisible(), 'unsaved marker is shown');
  const draftClips = await h.clips();
  await page.keyboard.press('Control+s');
  check(await h.waitFor(async () => !(await page.locator('[data-testid=dirty]').count())), 'Ctrl+S saves: the marker goes');
  const saved = await api(`/api/sets/${set.id}`);
  const savedClips = saved.tracks.flatMap((t) => t.clips);
  check(savedClips.length === draftClips.length, `the server has all ${draftClips.length} clips`);
  check(savedClips.some((c) => c.type === 'text' && c.params.text === 'GO'), 'with the edited text');
  await page.reload();
  await page.waitForSelector('.tl-row');
  await page.waitForTimeout(300);
  const reloaded = await h.clips();
  check(reloaded.length === draftClips.length && reloaded.every((c) => draftClips.some((d) => d.id === c.id && d.row === c.row)), 'after a reload everything is where it was');
  check(!(await page.locator('[data-testid=dirty]').count()), 'and it is clean');

  section('unsaved draft recovery');
  {
    const victim = reloaded.find((c) => c.type === 'fx:pulse');
    await page.mouse.click(...Object.values(await h.clipCenter(victim.id)));
    await page.keyboard.press('Delete');
    await page.waitForTimeout(700); // backup is written debounced
    await page.reload();
    await page.waitForSelector('[data-testid=recover]', { timeout: 5000 }).catch(() => null);
    const dlg = await page.locator('[data-testid=recover]').count();
    check(dlg === 1, 'reopening offers to recover the unsaved draft');
    if (dlg) {
      await page.screenshot({ path: `${SHOTS}/desktop-4-recover.png` });
      await page.click('[data-testid=recover]');
      await page.waitForTimeout(200);
      const after = await h.clips();
      check(!after.some((c) => c.id === victim.id) && after.length === reloaded.length - 1, 'recover restores the draft');
      check(await page.locator('[data-testid=dirty]').isVisible(), 'recovered draft is marked unsaved');
      await page.keyboard.press('Control+z');
      check((await h.clips()).some((c) => c.id === victim.id), 'undo goes back to the saved version');
      check(!(await page.locator('[data-testid=dirty]').count()), 'which is clean');
      await page.keyboard.press('Control+Shift+z');
    }
    // leaving through the app's nav asks first (accepted by the dialog handler: our confirm is in-page)
    await page.click('.topbar nav a:has-text("Sets")');
    check((await page.locator('.backdrop .dialog h2').textContent().catch(() => '')) === 'Leave without saving?', 'leaving with unsaved changes asks');
    await page.click('.backdrop button:has-text("Cancel")');
    check(page.url().includes('/editor/'), 'Cancel stays in the editor');
  }

  section('desktop screenshot');
  await page.keyboard.press('Control+s');
  await page.waitForTimeout(300);
  await page.mouse.click(...Object.values(await h.clipCenter(reloaded.find((c) => c.type === 'text').id)));
  await h.drag(await h.rulerAt(3.2), await h.rulerAt(3.2));
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOTS}/desktop-5-final.png` });
  await ctx.close();

  // ---------------------------------------------------------------- whole sign
  if (signVideo) {
    section('whole sign: one block over the three lights');
    const wset = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E whole sign', bpm: 120, duration: 30 }) });
    const c = await browser.newContext({ viewport: { width: 1600, height: 900 } });
    const p = await c.newPage();
    p.on('dialog', (d) => d.accept());
    p.on('pageerror', (e) => {
      failures++;
      console.log('  PAGE ERROR', e.message);
    });
    const hh = helpers(p);
    await p.goto(`${BASE}/editor/${wset.id}`);
    await p.waitForSelector('.tl-row');
    await p.waitForTimeout(500);
    const D = signVideo.duration;
    const blocks = async () => (await hh.clips()).filter((x) => x.row === 'whole').sort((a, b) => a.start - b.start);
    const overlaps = (a, b) => a.start < b.start + b.dur - 1e-3 && b.start < a.start + a.dur - 1e-3;
    /** Light clips that overlap a whole-sign block (should be none after any edit). */
    const clashes = async () => {
      const cs = await hh.clips();
      const ws = cs.filter((x) => x.row === 'whole');
      return cs.filter((x) => x.row !== 'whole' && x.row !== 3 && ws.some((w) => overlaps(w, x))).length
        + ws.filter((w, i) => ws.some((v, j) => j !== i && overlaps(w, v))).length;
    };
    /** Does the block's box cover exactly the three light rows? */
    const spansLights = async (id, sel = `[data-clip="${id}"]`) => {
      const b = await p.locator(sel).first().boundingBox();
      const L = await hh.lanes();
      return Math.abs(b.y - (L[0].top + 4)) < 1.5 && Math.abs(b.y + b.height - (L[2].bottom - 4)) < 1.5;
    };

    // drag the whole-sign video over the middle light: the ghost covers all three lights
    {
      const from = await hh.item(`[data-media="${signVideo.id}"]`);
      check((await p.locator(`[data-media="${signVideo.id}"]`).textContent()).includes('whole sign'), 'the library says the item is for the whole sign');
      await p.mouse.move(from.x, from.y);
      await p.mouse.down();
      const to = await hh.at(1, 1.02);
      await p.mouse.move(to.x, to.y, { steps: 12 });
      await p.waitForTimeout(80);
      const ghostSpans = await spansLights(null, '.clip.ghost');
      const cls = await Promise.all([0, 1, 2, 3].map((i) => p.locator('.tl-row').nth(i).getAttribute('class')));
      await p.screenshot({ path: `${SHOTS}/whole-1-ghost.png` });
      await p.mouse.up();
      await p.waitForTimeout(100);
      check(ghostSpans, 'while dragging, the ghost spans the three light rows');
      check(cls.slice(0, 3).every((k) => k.includes('drop-ok')) && cls[3].includes('drop-no'), 'the lights take it, the audio row does not');
    }
    let ws = await blocks();
    const w1 = ws[0];
    check(ws.length === 1 && near(w1.start, 1) && near(w1.dur, D, 0.01), `dropped: one whole-sign block at 1 s, ${D.toFixed(2)} s long (got ${JSON.stringify(ws.map((x) => [x.start, x.dur]))})`);
    check(w1 && (await spansLights(w1.id)), 'the block covers the three light rows exactly');
    check(w1?.thumb, 'it shows a thumbnail strip');
    check((await p.locator('.insp-title').textContent()) === 'Video' && (await p.locator('.insp-track').textContent()).includes('Whole sign'), 'the inspector shows it as a whole-sign video');

    // a light clip dropped into its time goes beside it
    await hh.drag(await hh.item('.pal-chip[data-type="text"]'), await hh.at(0, 3));
    let cs = await hh.clips();
    const txt = cs.find((x) => x.type === 'text');
    check(txt && txt.row === 0 && near(txt.start, w1.start + w1.dur, 0.01), `a text dropped inside the block's time goes right after it (${txt?.start.toFixed(2)})`);
    check((await clashes()) === 0, 'no light clip overlaps a whole-sign block');

    // tap an empty spot on the bottom light, then the item: a block there
    await p.mouse.click(...Object.values(await hh.at(2, 16.02)));
    await p.click(`[data-media="${signVideo.id}"]`);
    ws = await blocks();
    const w2 = ws.find((x) => x.id !== w1.id);
    check(ws.length === 2 && w2 && near(w2.start, 16) && (await spansLights(w2.id)), `click a row, click the item: a second block at 16 s (${w2?.start})`);
    await p.screenshot({ path: `${SHOTS}/whole-2-blocks.png` });

    // moving the second block onto the text stops at the text
    await hh.drag(await hh.clipCenter(w2.id), { x: (await hh.clipCenter(w2.id)).x - 5.5 * (await hh.geom()).pps, y: (await hh.clipCenter(w2.id)).y });
    let w2m = (await blocks()).find((x) => x.id === w2.id);
    check(near(w2m.start, txt.start + txt.dur, 0.01), `a block dragged onto a light clip butts up against it (${w2m.start.toFixed(2)})`);
    // dragging it up and down does not change it
    await hh.drag(await hh.clipCenter(w2.id), { x: (await hh.clipCenter(w2.id)).x, y: (await hh.at(3, 0)).y });
    check(near((await blocks()).find((x) => x.id === w2.id).start, w2m.start) && (await spansLights(w2.id)), 'dragging it down stays a whole-sign block');

    // trims stop at the neighbours
    await hh.drag(await hh.clipEdge(w1.id, 'end'), await hh.at(1, w1.start + w1.dur + 2));
    check(near((await blocks()).find((x) => x.id === w1.id).dur, w1.dur, 0.01), 'trimming the block\'s end into the text stops at the text');
    await hh.drag(await hh.clipEdge(txt.id, 'start'), await hh.at(0, 4));
    cs = await hh.clips();
    check(near(cs.find((x) => x.id === txt.id).start, txt.start, 0.01), 'trimming the text\'s start into the block stops at the block');
    await hh.drag(await hh.clipCenter(txt.id), await hh.at(2, 3));
    cs = await hh.clips();
    check((await clashes()) === 0 && cs.find((x) => x.id === txt.id).row === 2, 'moving the text to the bottom light inside the block\'s time puts it outside that time');
    await p.keyboard.press('Control+z');
    check(near((await hh.clips()).find((x) => x.id === txt.id).start, txt.start, 0.01) && (await hh.clips()).find((x) => x.id === txt.id).row === 0, 'undo puts it back');
    await p.keyboard.press('Control+Shift+z');
    await p.keyboard.press('Control+z');

    // the inspector switch: a light clip becomes a whole-sign clip and back
    await hh.drag(await hh.item('.pal-chip[data-type="light"]'), await hh.at(1, 26));
    cs = await hh.clips();
    const lc = cs.find((x) => x.type === 'light');
    check(lc && lc.row === 1, 'a light clip on the middle light');
    await p.click('[data-testid=shows-on] button:has-text("Whole sign")');
    let lw = (await hh.clips()).find((x) => x.id === lc.id);
    check(lw.row === 'whole' && near(lw.start, lc.start) && near(lw.dur, lc.dur) && (await spansLights(lc.id)), '"Shows on: Whole sign" turns it into a block, same start and length');
    await p.click('[data-testid=shows-on] button:has-text("One light")');
    lw = (await hh.clips()).find((x) => x.id === lc.id);
    check(lw.row === 1 && near(lw.start, lc.start), '"One light" puts it back on the light it came from');
    await p.keyboard.press('Control+z');
    check((await hh.clips()).find((x) => x.id === lc.id).row === 'whole', 'undo: whole sign again');
    await p.keyboard.press('Control+Shift+z');
    // refused: another light clip in the way
    await hh.drag(await hh.item('.pal-chip[data-type="text"]'), await hh.at(2, lc.start + 0.5));
    await p.mouse.click(...Object.values(await hh.clipCenter(lc.id)));
    await p.click('[data-testid=shows-on] button:has-text("Whole sign")');
    const toast = await hh.waitFor(async () => (await p.locator('.toast').allTextContents()).join(' | '), 1500);
    check((await hh.clips()).find((x) => x.id === lc.id).row === 1 && /in the way|move them/.test(toast), `refused while another light clip plays then, and says why ("${toast}")`);
    await p.screenshot({ path: `${SHOTS}/whole-3-refused.png` });
    await p.keyboard.press('Control+z'); // the second text goes

    // group: the first block and the text, moved together; the text changes row, the block stays a block
    await p.mouse.click(...Object.values(await hh.clipCenter(w1.id)));
    await p.keyboard.down('Shift');
    await p.mouse.click(...Object.values(await hh.clipCenter(txt.id)));
    await p.keyboard.up('Shift');
    const g0 = (await hh.clips()).filter((x) => x.id === w1.id || x.id === txt.id);
    {
      const from = await hh.clipCenter(w1.id);
      const L = await hh.lanes();
      const pps = (await hh.geom()).pps;
      // pointer goes down on the middle row and ends one row lower, half a second earlier
      await hh.drag({ x: from.x, y: (L[1].top + L[1].bottom) / 2 }, { x: from.x - 0.52 * pps, y: (L[2].top + L[2].bottom) / 2 });
    }
    const g1 = (await hh.clips()).filter((x) => x.id === w1.id || x.id === txt.id);
    const dW = g1.find((x) => x.id === w1.id).start - g0.find((x) => x.id === w1.id).start;
    const dT = g1.find((x) => x.id === txt.id).start - g0.find((x) => x.id === txt.id).start;
    check(near(dW, -0.5, 0.01) && near(dT, dW, 0.001), `the group moves as one (${dW.toFixed(2)} s, ${dT.toFixed(2)} s)`);
    check(g1.find((x) => x.id === w1.id).row === 'whole' && g1.find((x) => x.id === txt.id).row === 1, 'the text went one row down, the block stays over the lights');
    await p.screenshot({ path: `${SHOTS}/whole-4-group.png` });
    await p.keyboard.press('Control+z');
    const g2 = (await hh.clips()).filter((x) => x.id === w1.id || x.id === txt.id);
    check(g2.every((x) => { const o = g0.find((y) => y.id === x.id); return near(x.start, o.start) && x.row === o.row; }), 'undo restores both');
    await p.keyboard.press('Control+Shift+z');
    const g3 = (await hh.clips()).filter((x) => x.id === w1.id || x.id === txt.id);
    check(g3.every((x) => { const o = g1.find((y) => y.id === x.id); return near(x.start, o.start) && x.row === o.row; }), 'redo moves them again');
    await p.keyboard.press('Escape');

    // split, copy, paste, delete
    {
      const b = (await blocks()).find((x) => x.id === w1.id);
      await p.mouse.click(...Object.values(await hh.clipCenter(w1.id)));
      await hh.drag(await hh.rulerAt(b.start + 2), await hh.rulerAt(b.start + 2));
      await p.keyboard.press('s');
      const parts = (await blocks()).filter((x) => x.start >= b.start - 1e-3 && x.start < b.start + b.dur);
      check(parts.length === 2 && near(parts[1].start, b.start + 2, 0.02) && (await spansLights(parts[1].id)), 'S splits the block in two blocks');
      await p.mouse.click(...Object.values(await hh.clipCenter(parts[1].id)));
      await p.keyboard.press('Control+c');
      await p.keyboard.press('Control+v');
      const after = await blocks();
      check(after.length === 4 && (await clashes()) === 0, `Ctrl+V pastes a block where the lights are free (${after.map((x) => x.start.toFixed(2)).join(', ')})`);
      await p.keyboard.press('Control+d');
      check((await blocks()).length === 5 && (await clashes()) === 0, 'Ctrl+D duplicates it, also without overlap');
      await p.keyboard.press('Delete');
      check((await blocks()).length === 4, 'Delete removes a block');
    }
    await p.keyboard.press('Escape');
    await p.screenshot({ path: `${SHOTS}/whole-5-edited.png` });

    // save, reload: the same picture; the server keeps whole-sign clips on its "all" track
    const before = (await hh.clips()).map((x) => `${x.id}:${x.row}:${x.start.toFixed(3)}:${x.dur.toFixed(3)}`).sort();
    await p.keyboard.press('Control+s');
    check(await hh.waitFor(async () => !(await p.locator('[data-testid=dirty]').count())), 'saved');
    const saved = await api(`/api/sets/${wset.id}`);
    const allTrack = saved.tracks.find((t) => t.target === 'all');
    check(allTrack && allTrack.clips.length === (await blocks()).length && saved.tracks.indexOf(allTrack) === 0, 'the server has the blocks on the whole-sign track, the top layer');
    await p.reload();
    await p.waitForSelector('[data-whole]');
    await p.waitForTimeout(300);
    const afterLoad = (await hh.clips()).map((x) => `${x.id}:${x.row}:${x.start.toFixed(3)}:${x.dur.toFixed(3)}`).sort();
    check(JSON.stringify(before) === JSON.stringify(afterLoad), 'after a reload every clip is where it was');
    await p.screenshot({ path: `${SHOTS}/whole-6-reloaded.png` });

    // the server's picture: the video on all three lights
    {
      const b = (await blocks())[0];
      const t = b.start + 1;
      const thirds = await p.evaluate(async ([id, tt]) => {
        const bmp = await createImageBitmap(await (await fetch(`/api/render.png?set=${id}&t=${tt}`)).blob());
        const cv = document.createElement('canvas');
        cv.width = bmp.width;
        cv.height = bmp.height;
        const x = cv.getContext('2d');
        x.drawImage(bmp, 0, 0);
        const third = bmp.height / 3;
        return [0, 1, 2].map((k) => {
          const d = x.getImageData(0, k * third, bmp.width, third).data;
          let s = 0;
          for (let i = 0; i < d.length; i += 4) s += d[i] + d[i + 1] + d[i + 2];
          return s / (d.length / 4) / 3;
        });
      }, [wset.id, t]);
      check(thirds.every((m) => m > 12), `/api/render.png shows the whole-sign video on all three lights (mean ${thirds.map((m) => m.toFixed(0)).join(' / ')})`);
    }

    // the inspector's picture is what the sign shows: whole sign (three circles), then one light
    {
      const b = (await blocks())[0];
      await p.mouse.click(...Object.values(await hh.clipCenter(b.id)));
      await p.waitForSelector('[data-testid=clip-picture][data-loaded="1"]');
      const shown = await p.evaluate(() => {
        const cv = document.querySelector('[data-testid=clip-picture]');
        return { w: cv.width, h: cv.height, data: Array.from(cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data) };
      });
      check(shown.w * 3 === shown.h, `the inspector shows the whole sign's 1:3 picture (${shown.w}x${shown.h})`);
      // a one-clip set with the same video seen through the circles, at the thumbnail's moment
      const thumbAt = Math.min(1, signVideo.duration / 3);
      // (a whole-sign clip's mask defaults to the three circles, as on a light; the one at 10 s has it off)
      const ref = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E picture', duration: 20, tracks: [
        { name: 'W', target: 'all', clips: [
          { type: 'video', start: 0, duration: 5, params: { media: signVideo.id, loop: false } },
          { type: 'video', start: 10, duration: 5, mask: false, params: { media: signVideo.id, loop: false } }] },
        { name: 'M', target: 'face1', clips: [{ type: 'video', start: 5, duration: 5, params: { media: video.id, loop: false } }] },
      ] }) });
      const server = await serverRows(p, ref.id, thumbAt, 0, shown.h);
      const cmp = compare(shown.data, server);
      console.log(`        inspector vs server, whole sign: mean difference ${cmp.mad.toFixed(1)} / 255, correlation ${cmp.corr.toFixed(3)}`);
      await dumpPair(p, shown.data, server, shown.w, shown.h, `${SHOTS}/whole-7-picture-compare.png`);
      check(cmp.mad < 14 && (cmp.flat || cmp.corr > 0.9), 'the inspector picture matches the sign (whole sign, circles)');
      await p.screenshot({ path: `${SHOTS}/whole-8-inspector-whole.png` });
      // mask off: the whole box
      await p.locator('[data-testid=mask] button', { hasText: 'None' }).click();
      await p.waitForTimeout(300);
      await p.waitForSelector('[data-testid=clip-picture][data-loaded="1"]');
      const open = await p.evaluate(() => {
        const cv = document.querySelector('[data-testid=clip-picture]');
        return Array.from(cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data);
      });
      const cmpOpen = compare(open, await serverRows(p, ref.id, 10 + thumbAt, 0, shown.h));
      console.log(`        inspector vs server, whole sign, mask off: mean difference ${cmpOpen.mad.toFixed(1)} / 255, correlation ${cmpOpen.corr.toFixed(3)}`);
      check(cmpOpen.mad < 14 && (cmpOpen.flat || cmpOpen.corr > 0.9) && compare(open, shown.data).mad > 0.5, 'with the mask off the picture fills the whole sign, as the sign shows it');
      await p.keyboard.press('Control+z');
      // a light video clip on the middle light
      await hh.drag(await hh.item(`[data-media="${video.id}"]`), await hh.at(1, 27));
      const lv = (await hh.clips()).find((x) => x.type === 'video' && x.row === 1);
      check(!!lv, 'a plain (landscape) video goes on one light');
      await p.waitForSelector('[data-testid=clip-picture][data-loaded="1"]');
      const shownL = await p.evaluate(() => {
        const cv = document.querySelector('[data-testid=clip-picture]');
        return { w: cv.width, h: cv.height, data: Array.from(cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data) };
      });
      const thumbL = Math.min(1, video.duration / 3);
      const serverL = await serverRows(p, ref.id, 5 + thumbL, shownL.h, shownL.h * 2);
      const cmpL = compare(shownL.data, serverL);
      console.log(`        inspector vs server, one light: mean difference ${cmpL.mad.toFixed(1)} / 255, correlation ${cmpL.corr.toFixed(3)}`);
      await dumpPair(p, shownL.data, serverL, shownL.w, shownL.h, `${SHOTS}/whole-9-picture-compare-light.png`);
      check(shownL.w === shownL.h && cmpL.mad < 14 && (cmpL.flat || cmpL.corr > 0.9), 'the inspector picture matches the sign (one light, circle)');
      await p.screenshot({ path: `${SHOTS}/whole-10-inspector-light.png` });
      // switching the clip to the whole sign changes the picture
      await p.click('[data-testid=shows-on] button:has-text("Whole sign")');
      check(await hh.waitFor(async () => p.evaluate(() => {
        const cv = document.querySelector('[data-testid=clip-picture][data-loaded="1"]');
        return !!cv && cv.height === cv.width * 3;
      })), 'switching it to the whole sign shows the 1:3 picture');
      await p.keyboard.press('Control+z');
      await api(`/api/sets/${ref.id}`, { method: 'DELETE' });
    }
    await c.close();
    await api(`/api/sets/${wset.id}`, { method: 'DELETE' });

    section('a hand-made set that breaks the rule opens and plays');
    {
      const bad = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E overlap', bpm: 120, duration: 10, tracks: [
        { name: 'Whole sign', target: 'all', muted: true, clips: [{ type: 'video', start: 0, duration: 6, params: { media: signVideo.id } }] },
        { name: 'Top', target: 'face0', clips: [{ type: 'text', start: 1, duration: 2, params: { text: 'A' } }] },
        { name: 'Audio', target: 'audio', clips: [] },
        { name: 'Middle', target: 'face1', clips: [{ type: 'fx:pulse', start: 2, duration: 3 }] },
        { name: 'Bottom', target: 'face2', clips: [{ type: 'light', start: 0, duration: 7 }] },
      ] }) });
      const c2 = await browser.newContext({ viewport: { width: 1600, height: 900 } });
      const p2 = await c2.newPage();
      p2.on('pageerror', (e) => {
        failures++;
        console.log('  PAGE ERROR', e.message);
      });
      const h2 = helpers(p2);
      await p2.goto(`${BASE}/editor/${bad.id}`);
      await p2.waitForSelector('[data-whole]');
      await p2.waitForTimeout(400);
      const names = await p2.locator('.tl-name').allTextContents();
      check(JSON.stringify(names) === JSON.stringify(['Top', 'Middle', 'Bottom', 'Audio']), `the lights are shown above the audio row (${names.join(', ')})`);
      let cs2 = await h2.clips();
      const W = cs2.find((x) => x.row === 'whole');
      check(cs2.length === 4 && W && W.dim, 'every clip is drawn; the block of the muted whole-sign track is dimmed');
      const onTop = await p2.evaluate(() => {
        const w = document.querySelector('[data-whole]');
        const r = w.getBoundingClientRect();
        return document.elementFromPoint(r.left + r.width * 0.3, r.top + r.height * 0.15)?.closest('[data-clip]') === w;
      });
      check(onTop, 'the whole-sign block is drawn on top of the light clips under it');
      check(!(await p2.locator('[data-testid=dirty]').count()), 'opening it changes nothing');
      await p2.screenshot({ path: `${SHOTS}/whole-11-overlap.png` });
      const f0 = await h2.frameHash();
      await p2.click('[data-testid=play]');
      await p2.waitForTimeout(1200);
      await p2.click('[data-testid=play]');
      const tp = await h2.time();
      check(tp > 0.7 && (await h2.frameHash()) !== f0, `it plays (${tp.toFixed(2)} s)`);
      // nowhere to put it on one light: refused
      await p2.mouse.click(...Object.values(await h2.clipCenter(W.id, 0.9)));
      await p2.click('[data-testid=shows-on] button:has-text("One light")');
      const toast = await h2.waitFor(async () => (await p2.locator('.toast').allTextContents()).join(' | '), 1500);
      check((await h2.clips()).find((x) => x.id === W.id).row === 'whole' && /No light is free/.test(toast), `turning it into a light clip is refused when no light is free ("${toast}")`);
      // the first edit: the moved clip must fit, the mute of the hidden track goes
      const T = cs2.find((x) => x.type === 'text');
      await p2.keyboard.press('Escape');
      await p2.evaluate(() => document.querySelector('[data-whole]').style.pointerEvents = 'none');
      await h2.drag(await h2.clipCenter(T.id), await h2.clipCenter(T.id).then((q) => ({ x: q.x + 10, y: q.y })));
      await p2.evaluate(() => document.querySelector('[data-whole]').style.pointerEvents = '');
      cs2 = await h2.clips();
      const T2 = cs2.find((x) => x.id === T.id);
      check(T2.start >= 6 - 1e-3 && !cs2.find((x) => x.id === W.id).dim, `the next move puts the text outside the block (${T2.start.toFixed(2)}) and the block plays again`);
      check(cs2.find((x) => x.type === 'fx:pulse').start === 2, 'what was not moved stays where it was');
      await c2.close();
      await api(`/api/sets/${bad.id}`, { method: 'DELETE' });
    }
  }

  // ---------------------------------------------------------------- video speed: presets, and splits that keep the picture
  if (video) {
    section('video speed');
    const sset = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E speed', bpm: 120, duration: 8, tracks: [
      { name: 'Top', target: 'face0', clips: [{ type: 'video', start: 0, duration: 6, params: { media: video.id, loop: true } }] },
      { name: 'Middle', target: 'face1', clips: [{ type: 'video', start: 0, duration: 6, params: { media: video.id, loop: true } }] },
    ] }) });
    const c4 = await browser.newContext({ viewport: { width: 1600, height: 900 } });
    const p4 = await c4.newPage();
    p4.on('pageerror', (e) => {
      failures++;
      console.log('  PAGE ERROR', e.message);
    });
    const h4 = helpers(p4);
    await p4.goto(`${BASE}/editor/${sset.id}`);
    await p4.waitForSelector('[data-clip]');
    await p4.waitForTimeout(400);
    const [top, mid] = (await h4.clips()).sort((a, b) => a.row - b.row);
    const speedRow = p4.locator('.insp-row', { hasText: 'Speed' });
    await p4.mouse.click(...Object.values(await h4.clipCenter(mid.id)));
    check(await speedRow.locator('[data-testid=speed-presets]').isVisible(), 'the speed has quick choices');
    check((await speedRow.textContent()).includes('only at 1×'), `the note about the sound is next to it ("${(await speedRow.locator('.insp-help').textContent())}")`);
    await speedRow.locator('button', { hasText: /^2×$/ }).click();
    await p4.screenshot({ path: `${SHOTS}/speed-1-inspector.png` });
    await p4.mouse.click(...Object.values(await h4.clipCenter(top.id)));
    await speedRow.locator('button', { hasText: /^0.5×$/ }).click();
    const typed = await h4.waitFor(async () => (await speedRow.locator('#p-speed').inputValue()) === '0.5' || null, 1000);
    check(typed, `a choice sets the number (${await speedRow.locator('#p-speed').inputValue()})`);
    await p4.keyboard.press('Control+s');
    await h4.waitFor(async () => !(await p4.locator('[data-testid=dirty]').count()));
    let saved = await api(`/api/sets/${sset.id}`);
    check(saved.tracks[0].clips[0].params.speed === 0.5 && saved.tracks[1].clips[0].params.speed === 2, 'saved at 0.5× and 2×');
    const times = [1.2, 1.29, 1.31, 2.4, 4.7];
    const frame = (t) => serverRows(p4, sset.id, t, 0, 256);
    const before = [];
    for (const t of times) before.push(await frame(t));
    // split both at 1.3 s (nothing selected: every clip under the playhead), save, render again
    await p4.keyboard.press('Escape');
    await h4.drag(await h4.rulerAt(1.3), await h4.rulerAt(1.3));
    await p4.keyboard.press('s');
    await p4.keyboard.press('Control+s');
    await h4.waitFor(async () => !(await p4.locator('[data-testid=dirty]').count()));
    saved = await api(`/api/sets/${sset.id}`);
    const offs = saved.tracks.map((t) => t.clips.map((c) => `${c.start}+${c.params.offset}`).join(' '));
    check(saved.tracks.every((t) => t.clips.length === 2), `both clips are split (${offs.join(' | ')})`);
    const diffs = [];
    for (let i = 0; i < times.length; i++) diffs.push(compare(before[i], await frame(times[i])).mad);
    console.log(`        before vs after the split, mean difference at ${times.join(', ')} s: ${diffs.map((d) => d.toFixed(2)).join(', ')}`);
    check(diffs.every((d) => d < 1), 'at 0.5× and 2× the picture is the same before and after the split, on both sides of the cut');
    const across = compare(await frame(1.29), await frame(1.31)).mad;
    console.log(`        1.29 s vs 1.31 s (across the cut): mean difference ${across.toFixed(1)}`);
    await c4.close();
    await api(`/api/sets/${sset.id}`, { method: 'DELETE' });
  }

  // ---------------------------------------------------------------- snapping to beats or time
  section('snap grid: beats or time');
  {
    const gset = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E snap grid', bpm: 120, duration: 10, tracks: [
      { name: 'Top', target: 'face0', clips: [{ type: 'text', start: 1, duration: 3, params: { text: 'A' } }] },
      { name: 'Middle', target: 'face1', clips: [] },
    ] }) });
    const c3 = await browser.newContext({ viewport: { width: 1600, height: 900 } });
    const p3 = await c3.newPage();
    const h3 = helpers(p3);
    await p3.goto(`${BASE}/editor/${gset.id}`);
    await p3.waitForSelector('[data-clip]');
    await p3.waitForTimeout(500);
    const id = (await h3.clips())[0].id;
    const startOf = async () => (await h3.clips()).find((x) => x.id === id).start;
    const move = async (dt) => {
      const q = await h3.clipCenter(id);
      await h3.drag(q, { x: q.x + dt * (await h3.geom()).pps, y: q.y });
    };
    check((await p3.locator('[data-testid=snap-unit]').inputValue()) === 'beats' && (await p3.locator('[data-testid=snap-step]').inputValue()) === '0', 'the default is beats, following the zoom');
    await move(0.37);
    check(near(await startOf(), 1.5, 0.001), `beats: a move snaps to the half-second beat (${await startOf()})`);
    await p3.keyboard.press('Control+z');
    await p3.selectOption('[data-testid=snap-unit]', 'time');
    check((await p3.locator('[data-testid=snap-step]').inputValue()) === '0.25', 'time starts at 0.25 s');
    await move(0.37);
    check(near(await startOf(), 1.25, 0.001), `time: the same move snaps to 1.25 s (${await startOf()})`);
    // trim to the grid
    {
      const e = await h3.clipEdge(id, 'end');
      await h3.drag(e, { x: e.x + 0.4 * (await h3.geom()).pps, y: e.y });
      const cl = (await h3.clips()).find((x) => x.id === id);
      check(near(cl.start + cl.dur, 4.75, 0.001), `time: a trim snaps to 0.25 s (end ${(cl.start + cl.dur).toFixed(3)})`);
    }
    // a new clip from the library
    await h3.drag(await h3.item('.pal-chip[data-type="fx:pulse"]'), await h3.at(1, 6.13));
    const pl = (await h3.clips()).find((x) => x.type === 'fx:pulse');
    check(pl && near(pl.start, 6.25, 0.001), `time: a new clip snaps to 0.25 s (${pl?.start})`);
    // arrow keys move by one line
    await p3.mouse.click(...Object.values(await h3.clipCenter(id)));
    const s0 = await startOf();
    await p3.keyboard.press('ArrowRight');
    check(near(await startOf(), s0 + 0.25, 0.001), 'ArrowRight moves by 0.25 s');
    // the lines behind the tracks are the grid
    const lines = async () => p3.evaluate(() => {
      const cv = document.querySelector('.tl-grid');
      const pps = Number(document.querySelector('.tl-inner').dataset.pps);
      const sl = document.querySelector('.tl-scroll').scrollLeft;
      const d = cv.getContext('2d').getImageData(0, 4, cv.width, 1).data;
      const dpr = cv.width / cv.clientWidth;
      const xs = [];
      for (let x = 0; x < cv.width; x++) if (d[x * 4 + 3] > 0) xs.push((x / dpr + sl) / pps);
      return xs.filter((t) => t < 9.9);
    });
    const onGrid = (xs, step, tol) => xs.length > 3 && xs.every((t) => Math.abs(t / step - Math.round(t / step)) * step < tol);
    let xs = await lines();
    const tolPx = 1.5 / (await h3.geom()).pps;
    check(onGrid(xs, 0.25, tolPx) && xs.length >= 30, `the lines drawn are 0.25 s apart (${xs.length} lines)`);
    await p3.selectOption('[data-testid=snap-step]', '1');
    xs = await lines();
    check(onGrid(xs, 1, tolPx) && xs.length >= 8 && xs.length <= 11, `step 1 s: lines every second (${xs.length})`);
    await move(0.4);
    check(Number.isInteger(Math.round((await startOf()) * 1000) / 1000), `step 1 s: moves snap to whole seconds (${await startOf()})`);
    await p3.screenshot({ path: `${SHOTS}/snap-1-time.png` });
    await p3.reload();
    await p3.waitForSelector('[data-clip]');
    check((await p3.locator('[data-testid=snap-unit]').inputValue()) === 'time' && (await p3.locator('[data-testid=snap-step]').inputValue()) === '1', 'the choice survives a reload');
    await p3.selectOption('[data-testid=snap-unit]', 'beats');
    check((await p3.locator('[data-testid=snap-step]').inputValue()) === '0', 'beats keep their own step (Auto)');
    await p3.selectOption('[data-testid=snap-unit]', 'time');
    check((await p3.locator('[data-testid=snap-step]').inputValue()) === '1', 'and time keeps 1 s');
    await c3.close();
    await api(`/api/sets/${gset.id}`, { method: 'DELETE' });
  }

  // ---------------------------------------------------------------- tablet and phone
  for (const dev of [
    { name: 'tablet', viewport: { width: 1024, height: 768 } },
    { name: 'tablet-portrait', viewport: { width: 768, height: 1024 } },
    { name: 'phone', viewport: { width: 390, height: 844 } },
  ]) {
    section(dev.name);
    const c = await browser.newContext({ viewport: dev.viewport, hasTouch: true, isMobile: dev.name === 'phone', deviceScaleFactor: dev.name === 'phone' ? 2 : 1 });
    const p = await c.newPage();
    p.on('dialog', (d) => d.accept());
    p.on('pageerror', (e) => {
      failures++;
      console.log('  PAGE ERROR', e.message);
    });
    const hh = helpers(p);
    await p.goto(`${BASE}/editor/${set.id}`);
    await p.waitForSelector('.tl-row');
    await p.waitForTimeout(800);
    await p.screenshot({ path: `${SHOTS}/${dev.name}-1.png` });
    if (dev.name === 'phone') {
      // tap a spot on the middle light, then a library item: a clip appears there
      const n0 = (await hh.clips()).length;
      const spot = await hh.at(1, 12); // the middle light is empty from 6 s on
      // Where the tap lands with snapping on: a clip edge (or the set's end) within 8 px pulls it,
      // else the beat grid at this zoom (Auto) quantizes it.
      const expected = await (async () => {
        const { pps } = await hh.geom();
        const edges = [0, 20, ...(await hh.clips()).flatMap((x) => [x.start, x.start + x.dur])];
        const pull = edges.filter((e) => Math.abs(e - 12) <= 8 / pps).sort((a, b) => Math.abs(a - 12) - Math.abs(b - 12))[0];
        if (pull !== undefined) return pull;
        const beat = 0.5;
        const step = [1, 4, 16, 64].map((b) => b * beat).find((st) => st * pps >= 10);
        return Math.round(12 / step) * step;
      })();
      await p.touchscreen.tap(spot.x, spot.y);
      await p.locator('.pal-chip[data-type="figure"]').first().tap();
      const cs2 = await hh.clips();
      const fig = cs2.find((x) => x.type === 'figure');
      check(cs2.length === n0 + 1 && fig && fig.row === 1 && near(fig.start, expected, 0.01),
        `tap a track, tap an item: the clip is added there, snapped as when dragging (expected ${expected.toFixed(2)}, got ${fig?.start}, ${cs2.length - n0} new)`);
      // tap the selected clip again: the inspector opens
      const cc = await hh.clipCenter(fig.id);
      await p.touchscreen.tap(cc.x, cc.y);
      await p.waitForTimeout(150);
      check((await p.locator('.insp-title').textContent().catch(() => '')) === 'Figure', 'tapping the selected clip opens its inspector');
      await p.screenshot({ path: `${SHOTS}/${dev.name}-2-inspector.png` });
      const cdp = await c.newCDPSession(p);
      const touch = (type, pts) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: pts.map(([x, y], i) => ({ x, y, id: i })) });
      // one finger on the selected clip drags it
      {
        const from = await hh.clipCenter(fig.id);
        await touch('touchStart', [[from.x, from.y]]);
        for (let i = 1; i <= 10; i++) await touch('touchMove', [[from.x + i * 4, from.y]]);
        await touch('touchEnd', []);
        await p.waitForTimeout(100);
        const moved = (await hh.clips()).find((x) => x.id === fig.id);
        check(moved.start > fig.start + 0.5, `a one-finger drag moves the selected clip (${fig.start} -> ${moved.start.toFixed(2)})`);
      }
      // two fingers pinch-zoom the timeline
      {
        const pps0 = (await hh.geom()).pps;
        const mid = await hh.at(2, 3);
        await touch('touchStart', [[mid.x - 30, mid.y], [mid.x + 30, mid.y]]);
        for (let i = 1; i <= 10; i++) await touch('touchMove', [[mid.x - 30 - i * 6, mid.y], [mid.x + 30 + i * 6, mid.y]]);
        await touch('touchEnd', []);
        await p.waitForTimeout(100);
        const pps1 = (await hh.geom()).pps;
        check(pps1 > pps0 * 1.5, `pinch zooms the timeline (${pps0.toFixed(1)} -> ${pps1.toFixed(1)} px/s)`);
        await p.screenshot({ path: `${SHOTS}/${dev.name}-3-zoomed.png` });
      }
      await p.locator('button[aria-label=Undo]').tap();
      await p.locator('button[aria-label=Undo]').tap();
      // the toolbar fits the phone: nothing scrolls sideways, two lines at most, every control on screen
      const bar = await p.evaluate(() => {
        const el = document.querySelector('.transport-bar');
        const r = el.getBoundingClientRect();
        const outside = [...el.querySelectorAll('button, select, label')].filter((b) => {
          const q = b.getBoundingClientRect();
          return q.width > 0 && (q.left < -0.5 || q.right > window.innerWidth + 0.5);
        }).length;
        return { overflow: el.scrollWidth - el.clientWidth, h: r.height, outside };
      });
      check(bar.overflow <= 0 && bar.outside === 0 && bar.h < 100, `the transport bar fits the phone (overflow ${bar.overflow}px, ${bar.h.toFixed(0)}px tall, ${bar.outside} controls off screen)`);
      // tap a light row, then the whole-sign video: one block over the three lights, clear of light clips
      if (signVideo) {
        await p.locator('.ed-tabbar button:has-text("Library")').tap();
        await p.locator('button[aria-label="Zoom to fit"]').tap();
        await p.waitForTimeout(150);
        const spot2 = await hh.at(0, 13);
        await p.touchscreen.tap(spot2.x, spot2.y);
        await p.locator(`[data-media="${signVideo.id}"]`).tap();
        await p.waitForTimeout(150);
        const all = await hh.clips();
        const blk = all.find((x) => x.row === 'whole');
        const L = await hh.lanes();
        const bb = blk && (await p.locator(`[data-clip="${blk.id}"]`).boundingBox());
        const free = blk && !all.some((x) => x.row !== 'whole' && x.row !== 3 && x.start < blk.start + blk.dur - 1e-3 && blk.start < x.start + x.dur - 1e-3);
        // 13 s is in a gap of the lights shorter than the video: the block fills that gap
        check(!!blk && free && blk.start <= 13 && blk.start + blk.dur >= 13 && Math.abs(bb.y - (L[0].top + 4)) < 1.5 && Math.abs(bb.y + bb.height - (L[2].bottom - 4)) < 1.5,
          `phone: tap a light, tap the whole-sign video: one block over the three lights (${blk?.start.toFixed(2)}..${blk ? (blk.start + blk.dur).toFixed(2) : ''})`);
        await p.screenshot({ path: `${SHOTS}/${dev.name}-4-whole.png` });
        await p.locator('button[aria-label=Undo]').tap();
      }
    }
    await c.close();
  }

  // ---------------------------------------------------------------- many clips: drag stays smooth
  section('200 clips');
  {
    const tracks = [];
    const targets = ['all', 'face0', 'face1', 'face2'];
    for (let k = 0; k < 4; k++) {
      const clips = [];
      for (let i = 0; i < 50; i++) {
        // whole-sign clips after the light clips: they take all three lights over
        clips.push({ type: k === 0 ? 'light' : i % 2 ? 'text' : 'fx:pulse', start: (k === 0 ? 100 : 0) + i * 2, duration: 1.5, params: i % 2 && k ? { text: `N${i}` } : {} });
      }
      tracks.push({ name: `T${k}`, target: targets[k], clips });
    }
    const big = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E 200 clips', bpm: 120, duration: 200, tracks }) });
    const c = await browser.newContext({ viewport: { width: 1600, height: 900 } });
    const p = await c.newPage();
    const hh = helpers(p);
    await p.goto(`${BASE}/editor/${big.id}`);
    await p.waitForSelector('[data-clip]');
    await p.waitForTimeout(600);
    check((await p.locator('[data-clip]').count()) === 200, '200 clips rendered');
    await p.click('button[aria-label="Zoom in"]');
    await p.click('button[aria-label="Zoom in"]');
    await p.waitForTimeout(300);
    const cs3 = await hh.clips();
    const target = cs3.find((x) => x.row === 0 && near(x.start, 4));
    await p.evaluate(() => {
      window.__frames = [];
      let last = performance.now();
      const tick = (t) => {
        window.__frames.push(t - last);
        last = t;
        if (window.__frames.length < 2000) window.__raf = requestAnimationFrame(tick);
      };
      window.__raf = requestAnimationFrame(tick);
    });
    const from = await hh.clipCenter(target.id);
    const t0 = Date.now();
    await p.mouse.move(from.x, from.y);
    await p.mouse.down();
    for (let i = 1; i <= 120; i++) await p.mouse.move(from.x + i * 3, from.y + (i > 60 ? 56 : 0));
    await p.mouse.up();
    const elapsed = Date.now() - t0;
    const frames = await p.evaluate(() => {
      cancelAnimationFrame(window.__raf);
      return window.__frames.slice(1);
    });
    const sorted = frames.slice().sort((a, b) => a - b);
    const p95 = sorted[Math.floor(sorted.length * 0.95)] ?? 0;
    console.log(`        120 pointer moves in ${elapsed} ms, ${frames.length} frames, p95 frame ${p95.toFixed(1)} ms, max ${sorted[sorted.length - 1]?.toFixed(1)} ms`);
    check(p95 < 40, 'dragging among 200 clips keeps frames short (p95 < 40 ms)');
    // scrub smoothness
    const r0 = await hh.rulerAt(1);
    await p.mouse.move(r0.x, r0.y);
    await p.mouse.down();
    const s0 = Date.now();
    for (let i = 1; i <= 100; i++) await p.mouse.move(r0.x + i * 6, r0.y);
    await p.mouse.up();
    console.log(`        100 scrub moves in ${Date.now() - s0} ms`);
    await p.waitForTimeout(300);
    await p.screenshot({ path: `${SHOTS}/desktop-6-200clips.png` });
    await c.close();
    await api(`/api/sets/${big.id}`, { method: 'DELETE' });
  }
} finally {
  await browser.close();
  await api(`/api/sets/${set.id}`, { method: 'DELETE' }).catch(() => undefined);
  if (signVideo) await api(`/api/media/${signVideo.id}?force=1`, { method: 'DELETE' }).catch(() => undefined);
}

console.log(failures ? `\n${failures} check(s) FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
