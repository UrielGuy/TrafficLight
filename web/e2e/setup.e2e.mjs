// The setup checks in the UI against a running server, in Microsoft Edge (playwright-core), at desktop and
// phone widths: Settings lists every check of /api/state, the Dashboard only what needs attention, and the
// Settings tab is marked when a check is an error.
//
//   node e2e/setup.e2e.mjs <server url> <label>     (screenshots: TL_SHOTS, default e2e/shots/setup-<label>-*.png)
import { mkdirSync } from 'node:fs';
import { chromium } from 'playwright-core';

const [BASE = 'http://localhost:8091', LABEL = 'run'] = process.argv.slice(2);
const SHOTS = process.env.TL_SHOTS ?? 'e2e/shots';
mkdirSync(SHOTS, { recursive: true });

let failures = 0;
const check = (cond, msg) => {
  console.log(cond ? '  ok   ' : '  FAIL ', msg);
  if (!cond) failures++;
};

const state = await (await fetch(`${BASE}/api/state`)).json();
const checks = state.checks;
const open = checks.filter((c) => c.level === 'error' || c.level === 'warning');
const hasError = checks.some((c) => c.level === 'error');
console.log(`${LABEL}: ${checks.map((c) => `${c.id}=${c.level}`).join(' ')}`);

const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  for (const [name, viewport] of [['desktop', { width: 1280, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport, isMobile: name === 'phone', hasTouch: name === 'phone' });
    page.on('pageerror', (e) => check(false, `page error: ${e.message}`));
    await page.goto(`${BASE}/settings`);
    await page.locator('.setup .checks').waitFor();
    const rows = await page.locator('.setup .check').evaluateAll((els) => els.map((e) => [e.dataset.check, e.className]));
    check(rows.length === checks.length && rows.every(([id, cls], i) => id === checks[i].id && cls.includes(checks[i].level)),
      `${name}: Settings lists all ${checks.length} checks in order, with their levels`);
    for (const c of open) {
      const text = await page.locator(`.setup .check[data-check="${c.id}"]`).textContent();
      check(text.includes(c.title) && (!c.fix || text.includes(c.fix.replaceAll('`', ''))), `${name}: ${c.id} shows its title and fix`);
    }
    check((await page.locator('.nav-alert').count()) === (hasError ? 1 : 0), `${name}: Settings tab marked ${hasError ? '' : 'not '}`);
    const overflow = await page.evaluate(() => document.scrollingElement.scrollWidth - window.innerWidth);
    check(overflow <= 0, `${name}: no sideways scrolling (${overflow}px)`);
    await page.locator('.setup').screenshot({ path: `${SHOTS}/setup-${LABEL}-${name}-settings.png` });

    await page.goto(`${BASE}/`);
    await page.waitForTimeout(800);
    const shown = await page.locator('.attention .check').evaluateAll((els) => els.map((e) => e.dataset.check));
    check(JSON.stringify(shown) === JSON.stringify(open.map((c) => c.id)), `${name}: Dashboard shows only ${open.length} to fix`);
    if (open.length) check(await page.locator('.attention a[href="/settings"]').count() === 1, `${name}: with a link to Settings`);
    await page.screenshot({ path: `${SHOTS}/setup-${LABEL}-${name}-dashboard.png` });
    await page.close();
  }
} finally {
  await browser.close();
}
console.log(failures ? `${failures} failed` : 'all passed');
process.exit(failures ? 1 : 0);
