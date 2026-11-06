// End-to-end test of the panel wiring wizard (Settings page) against a running server, in Microsoft Edge
// (playwright-core, channel "msedge"), at desktop and phone widths.
//
//   TL_URL=http://127.0.0.1:8091 node e2e/wiring.e2e.mjs
//
// It changes the server's output settings (order, flips, rotate180) and puts them back at the end.
// Screenshots go to TL_SHOTS (default e2e/shots). The expected settings are the ones
// tests/test_output_wiring.py derives for the same observations.
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
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

async function api(path, init = {}) {
  const r = await fetch(BASE + path, { headers: { 'Content-Type': 'application/json' }, ...init });
  if (!r.ok) throw new Error(`${init.method ?? 'GET'} ${path}: ${r.status} ${await r.text()}`);
  return r.json();
}
const patch = (body) => api('/api/settings', { method: 'PATCH', body: JSON.stringify(body) });
const until = async (cond, ms = 3000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await cond()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
};
const showing = async () => (await api('/api/state')).now;

const NAMES = ['Top light, upper panel', 'Top light, lower panel', 'Middle light, upper panel',
  'Middle light, lower panel', 'Bottom light, upper panel', 'Bottom light, lower panel'];

async function setRow(page, i, number, down) {
  const row = page.locator('.wiring-row', { hasText: NAMES[i] });
  await row.getByLabel(`Number on ${NAMES[i]}`).selectOption(number === null ? '' : String(number));
  if (number !== null) await row.getByRole('button', { name: down ? '↓ Down' : '↑ Up' }).click();
}
const readRows = (page) => page.evaluate(() => [...document.querySelectorAll('.wiring-row')].map((r) => {
  const v = r.querySelector('select').value;
  const active = r.querySelector('.segmented button.active')?.textContent ?? '';
  return v === '' ? null : [Number(v), active.includes('Down')];
}));

async function run(browser, label, viewport) {
  console.log(`\n${label} (${viewport.width}x${viewport.height})`);
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1, isMobile: viewport.width < 600, hasTouch: viewport.width < 600 });
  page.on('pageerror', (e) => check(false, `page error: ${e.message}`));

  // A sign that was set up with rotate180: the wizard must pre-fill what the panels really show.
  await patch({ output: { rotate180: true, panel_order: [7, 2, 0, 5, 1, 4], panel_flip: [true, false, false, true, false, false] } });
  await page.goto(`${BASE}/settings`);
  await page.locator('.wiring').waitFor();
  check(same(await readRows(page), [[5, true], [2, true], [6, false], [1, true], [3, true], [8, false]]),
    'pre-filled from the saved settings, rotate180 folded in');
  const overflow = await page.evaluate(() => document.scrollingElement.scrollWidth - window.innerWidth);
  check(overflow <= 0, `no sideways scrolling (overflow ${overflow}px)`);

  await page.getByRole('button', { name: 'Show connector numbers' }).click();
  check(await until(async () => (await showing()).set_name === 'wiring'), 'Show connector numbers puts the wiring picture up');
  await page.locator('.wiring').screenshot({ path: `${SHOTS}/wiring-${label}-1-prefilled.png` });

  // What the user reads off the sign, top to bottom: 3 up, 8 down, 2 up, 1 down, 5 up, 4 down.
  const seen = [[3, false], [8, true], [2, false], [1, true], [5, false], [4, true]];
  for (const [i, [n, d]] of seen.entries()) await setRow(page, i, n, d);
  check(same(await readRows(page), seen), 'the form holds what was entered');

  // Duplicate: the top panel set to 8 as well.
  await setRow(page, 0, 8, false);
  const problem = page.locator('.wiring-problem');
  check(await problem.count() === 1 && (await problem.textContent()).includes(`${NAMES[0]} and ${NAMES[1]} both show 8`),
    'a duplicate is refused with the two panels named');
  check(await page.getByRole('button', { name: 'Apply' }).isDisabled(), 'Apply is disabled while there is a duplicate');
  check(await page.locator('.wiring-row.bad').count() === 2, 'both rows are marked');
  await page.locator('.wiring').screenshot({ path: `${SHOTS}/wiring-${label}-2-duplicate.png` });
  await setRow(page, 0, 3, false);
  check(await problem.count() === 0, 'fixing it clears the message');

  await page.getByRole('button', { name: 'Apply' }).click();
  check(await until(async () => {
    const o = (await api('/api/settings')).output;
    return same(o.panel_order, [2, 7, 1, 0, 4, 3]) && same(o.panel_flip, [false, true, false, true, false, true]) && o.rotate180 === false;
  }), 'saved: panel_order [2,7,1,0,4,3], panel_flip [F,T,F,T,F,T], rotate180 off');
  await page.locator('.wiring-done').waitFor();
  check((await page.locator('.wiring-done').textContent()).includes('1 to 6 from the top, all upright'), 'offers the check');
  check(same(await readRows(page), seen), 'the form now follows the saved settings');
  await page.locator('.wiring').screenshot({ path: `${SHOTS}/wiring-${label}-3-applied.png` });
  await page.getByRole('button', { name: 'Show panels 1–6' }).click();
  check(await until(async () => (await showing()).set_name === 'panels'), 'the check pattern is up');

  // A panel that shows nothing: the rest is saved, it gets the lowest free connector.
  await setRow(page, 1, null);
  check(await page.locator('.wiring-row', { hasText: NAMES[1] }).getByRole('button', { name: '↓ Down' }).count() === 0,
    'a dark panel has no arrow choice');
  check((await page.locator('.wiring').textContent()).includes('free connector J6'), 'says which connector the dark panel gets');
  await page.locator('.wiring').screenshot({ path: `${SHOTS}/wiring-${label}-4-dark.png` });
  await page.getByRole('button', { name: 'Apply' }).click();
  check(await until(async () => {
    const o = (await api('/api/settings')).output;
    return same(o.panel_order, [2, 5, 1, 0, 4, 3]) && same(o.panel_flip, [false, false, false, true, false, true]);
  }), 'dark panel saved on J6 (band 5), the others unchanged');

  // The dashboard preview while the wiring picture is up: the sign as now mapped.
  await page.getByRole('button', { name: 'Show connector numbers' }).click();
  await until(async () => (await showing()).set_name === 'wiring');
  await page.goto(`${BASE}/`);
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${SHOTS}/wiring-${label}-5-dashboard.png`, fullPage: true });
  await api('/api/output/test', { method: 'POST', body: JSON.stringify({ pattern: 'off' }) });
  await page.close();
}

const before = (await api('/api/settings')).output;
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  await run(browser, 'desktop', { width: 1280, height: 1000 });
  await run(browser, 'phone', { width: 390, height: 844 });
} finally {
  await browser.close();
  await patch({ output: { rotate180: before.rotate180, panel_order: before.panel_order, panel_flip: before.panel_flip } });
  await api('/api/output/test', { method: 'POST', body: JSON.stringify({ pattern: 'off' }) });
}
console.log(failures ? `\n${failures} failed` : '\nall passed');
process.exit(failures ? 1 : 0);
