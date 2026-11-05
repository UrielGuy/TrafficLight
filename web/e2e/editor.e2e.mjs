// End-to-end test of the timeline editor against a running server, in Microsoft Edge
// (playwright-core, channel "msedge": no browser download).
//
//   node e2e/editor.e2e.mjs            (server at TL_URL, default http://localhost:8091)
//
// The server needs at least one ready video and one ready audio item in its media
// library for the media flows. Screenshots go to TL_SHOTS (default e2e/shots).
import { mkdirSync } from 'node:fs';
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
  const clips = () => page.evaluate(() => {
    const pps = Number(document.querySelector('.tl-inner').dataset.pps);
    return [...document.querySelectorAll('.tl-row')].flatMap((row, i) => [...row.querySelectorAll('[data-clip]')].map((c) => ({
      id: c.dataset.clip, type: c.dataset.type, row: i,
      start: parseFloat(c.style.left) / pps, dur: parseFloat(c.style.width) / pps,
      sel: c.classList.contains('sel'), bad: c.classList.contains('bad'), text: c.textContent,
      wave: !!c.querySelector('.clip-wave path'), thumb: !!c.querySelector('.clip-thumb'),
    })));
  });
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
  return { item, geom, at, rulerAt, clips, clipCenter, clipEdge, drag, time, mean, frameHash, waitFor };
}

// ---------------------------------------------------------------- setup
const media = await api('/api/media');
// the shortest video: its clip stays well inside the 20 s set
const video = media.filter((m) => m.kind === 'video' && m.status === 'ready' && m.duration > 2).sort((a, b) => a.duration - b.duration)[0];
const sound = media.find((m) => m.kind === 'audio' && m.status === 'ready');
console.log(`media: video=${video?.name ?? '-'} audio=${sound?.name ?? '-'}`);
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
  const ROW = { all: 0, top: 1, mid: 2, bottom: 3, audio: 4 };

  section('open');
  await page.goto(`${BASE}/editor/${set.id}`);
  await page.waitForSelector('.tl-row');
  check((await page.locator('.tl-row').count()) === 5, 'five default tracks');
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

  // A text cannot go on the whole sign: the row is marked and the drop does nothing.
  {
    const chip = await h.item('.pal-chip[data-type="text"]');
    await page.mouse.move(chip.x, chip.y);
    await page.mouse.down();
    const p = await h.at(ROW.all, 8);
    await page.mouse.move(p.x, p.y, { steps: 10 });
    const rowCls = await page.locator('.tl-row').nth(ROW.all).getAttribute('class');
    const topCls = await page.locator('.tl-row').nth(ROW.top).getAttribute('class');
    await page.mouse.up();
    check(rowCls.includes('drop-no') && topCls.includes('drop-ok'), 'during a text drag the whole-sign row is not a target, the lights are');
    check((await h.clips()).length === 2, 'drop on an incompatible track adds nothing');
  }

  if (video) {
    await h.drag(await h.item(`[data-media="${video.id}"]`), await h.at(ROW.all, 0.1));
    cs = await h.clips();
    const v = cs.find((c) => c.type === 'video');
    check(v && v.row === ROW.all && near(v.start, 0) && near(v.dur, video.duration, 0.01), `video on the whole sign gets the media's duration ${video.duration.toFixed(2)} s (got ${v?.row} ${v?.start} ${v?.dur})`);
    check(v?.thumb, 'video clip shows its thumbnail');
  } else console.log('  skip  no ready video in the library');
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
    // the whole-sign video is the top layer and hides the lights: muting it shows them
    const h0 = await h.frameHash();
    await page.locator('.tl-row').nth(ROW.all).locator('.tl-mute').click();
    check(await h.waitFor(async () => (await h.frameHash()) !== h0), 'muting the whole-sign track changes the preview');
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
  await page.locator('.tl-row').nth(ROW.all).locator('.tl-mute').click();
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
  check((await page.locator('.tl-row').count()) === 6, 'a middle-light track is added');
  const newRow = page.locator('.tl-row').nth(3);
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
  check((await page.locator('.tl-row').nth(2).locator('.tl-name').textContent()) === 'Sparkles', 'Move up reorders');
  // drag the grip of "Sparkles" to the top row
  {
    const grip = await page.locator('.tl-row').nth(2).locator('.tl-grip').boundingBox();
    const top = await page.locator('.tl-row').nth(0).boundingBox();
    await h.drag({ x: grip.x + 5, y: grip.y + grip.height / 2 }, { x: grip.x + 5, y: top.y + 10 });
    check((await page.locator('.tl-row').nth(0).locator('.tl-name').textContent()) === 'Sparkles', 'dragging the grip reorders (top = top layer)');
  }
  await page.locator('.tl-row').nth(0).locator('.tl-more').click();
  await page.click('.popover button:has-text("Delete track")');
  check((await page.locator('.tl-row').count()) === 5, 'an empty track is deleted');

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
  const vclip = (await h.clips()).find((c) => c.type === 'video');
  const other = media.find((m) => m.kind === 'video' && m.status === 'ready' && m.id !== video?.id);
  if (vclip && other) {
    await page.mouse.click(...Object.values(await h.clipCenter(vclip.id)));
    await page.locator('.mediactl select').selectOption(other.id);
    check(await h.waitFor(async () => (await h.clips()).find((c) => c.id === vclip.id).text.includes(other.name)), 'choosing another video in the media param updates the clip');
    await page.keyboard.press('Control+z');
  }

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
      // tap a spot on the bottom light, then a library item: a clip appears there
      const n0 = (await hh.clips()).length;
      const spot = await hh.at(2, 12); // the middle light is empty
      await p.touchscreen.tap(spot.x, spot.y);
      await p.locator('.pal-chip[data-type="figure"]').tap();
      const cs2 = await hh.clips();
      const fig = cs2.find((x) => x.type === 'figure');
      check(cs2.length === n0 + 1 && fig && fig.row === 2 && near(fig.start, 12, 0.3), `tap a track, tap an item: the clip is added there (${JSON.stringify(fig)}, ${cs2.length - n0} new)`);
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
        clips.push({ type: k === 0 ? 'light' : i % 2 ? 'text' : 'fx:pulse', start: i * 2, duration: 1.5, params: i % 2 && k ? { text: `N${i}` } : {} });
      }
      tracks.push({ name: `T${k}`, target: targets[k], clips });
    }
    const big = await api('/api/sets', { method: 'POST', body: JSON.stringify({ name: 'E2E 200 clips', bpm: 120, duration: 100, tracks }) });
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
    const target = cs3.find((x) => x.row === 1 && near(x.start, 4));
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
}

console.log(failures ? `\n${failures} check(s) FAILED` : '\nall checks passed');
process.exit(failures ? 1 : 0);
