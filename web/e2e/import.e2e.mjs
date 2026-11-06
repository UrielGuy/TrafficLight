// The link import's "are you sure?" for long videos and the download queue, in Microsoft Edge
// (playwright-core, channel "msedge"), against a running server with an empty library and internet access:
//
//   node e2e/import.e2e.mjs        (server at TL_URL, default http://localhost:8091)
//
// A link longer than config.IMPORT_WARN_SECONDS asks first: Cancel imports nothing, a Max length under the limit
// skips the question. A short link imports straight away. Deletes what it made.
import { chromium } from 'playwright-core';

const BASE = process.env.TL_URL ?? 'http://localhost:8091';
const LONG = 'https://www.youtube.com/watch?v=9bZkp7q19f0';    // 4:12
const SHORT = 'https://www.youtube.com/watch?v=jNQXAC9IVRw';   // 0:19
let failed = 0;
const check = (ok, what) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}`); if (!ok) failed++; };
const media = async () => (await fetch(BASE + '/api/media')).json();
async function untilMedia(count, ms = 30000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if ((await media()).length >= count) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

const browser = await chromium.launch({ channel: 'msedge' });
const page = await browser.newPage();
try {
  await page.goto(BASE + '/media');
  const before = (await media()).length;
  check(await page.inputValue('[data-testid=import-quality]') === '480', 'quality defaults to 480p');

  await page.fill('[data-testid=import-url]', LONG);
  await page.click('[data-testid=import-go]');
  const dialog = page.getByRole('dialog');
  await dialog.waitFor({ timeout: 30000 });
  const text = await dialog.innerText();
  check(/4:12 long/.test(text) && /Download anyway/.test(text), `long video asks first: ${JSON.stringify(text.slice(0, 120))}`);
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await page.waitForTimeout(500);
  check((await media()).length === before, 'Cancel imports nothing');
  check(await page.inputValue('[data-testid=import-url]') === LONG, 'the link stays in the field after Cancel');

  await page.fill('[data-testid=import-length]', '1:00');
  await page.click('[data-testid=import-go]');
  check(await untilMedia(before + 1), 'with a Max length of 1:00 it imports');
  check(!(await dialog.isVisible()), 'with a Max length of 1:00 there is no question');
  await page.waitForSelector('[data-testid=import-go]:has-text("Import")');

  await page.fill('[data-testid=import-length]', '');
  await page.fill('[data-testid=import-url]', SHORT);
  await page.click('[data-testid=import-go]');
  check(await untilMedia(before + 2), 'a 19 s video imports');
  check(!(await dialog.isVisible()), 'a 19 s video imports without a question');
  const items = await media();
  const stages = items.filter((m) => m.status === 'processing').map((m) => m.stage);
  check(stages.filter((s) => s === 'downloading').length <= 1, `at most one download at a time: ${stages.join(', ')}`);
  await page.screenshot({ path: 'e2e/shots/import-queue.png' });
  for (const m of items) await fetch(`${BASE}/api/media/${m.id}?force=1`, { method: 'DELETE' });
} finally {
  await browser.close();
}
console.log(failed ? `${failed} FAILED` : 'all passed');
process.exit(failed ? 1 : 0);
