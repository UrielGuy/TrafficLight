// Screenshot of a page in Edge: node e2e/shot.mjs <url> <width> <height> <out.png> [waitMs]
import { chromium } from 'playwright-core';

const [url, w, h, out, wait = '1500'] = process.argv.slice(2);
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: Number(w), height: Number(h) }, deviceScaleFactor: 1 });
  page.on('console', (m) => m.type() === 'error' && console.log('console:', m.text()));
  page.on('pageerror', (e) => console.log('pageerror:', e.message));
  await page.goto(url);
  await page.waitForTimeout(Number(wait));
  await page.screenshot({ path: out });
} finally {
  await browser.close();
}
