// Report mobile check (#927) — works on ANY built report.
// usage: node mobile-check.cjs <report.html> [--out <dir>] [--playwright <path>]
// Prints JSON {width: {scrollWidth, overflowing, sample, screenshot}} and
// exits 1 when any width scrolls sideways. `takkub report check` wraps this.
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const args = process.argv.slice(2);
const opt = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
};
const html = args.find((a, i) => !a.startsWith('--') && !(i > 0 && args[i - 1].startsWith('--')));
if (!html) {
  console.error('usage: node mobile-check.cjs <report.html> [--out <dir>] [--playwright <path>]');
  process.exit(2);
}
const out = opt('--out') || process.env.TAKKUB_ARTIFACTS_DIR || path.dirname(path.resolve(html));

function loadPlaywright() {
  const searchPaths = [process.cwd(), ...(process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean)];
  for (const name of [opt('--playwright'), 'playwright', '@playwright/test'].filter(Boolean)) {
    try {
      return require(require.resolve(name, { paths: searchPaths }));
    } catch (e) {
      // try the next candidate
    }
  }
  console.error('playwright not found — pass --playwright <path to node_modules/playwright>');
  process.exit(3);
}

const { chromium } = loadPlaywright();
(async () => {
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch();
  const url = pathToFileURL(path.resolve(html)).href;
  const result = {};
  for (const w of [360, 390, 768, 1440]) {
    const page = await browser.newPage({ viewport: { width: w, height: 900 } });
    await page.goto(url, { waitUntil: 'load' });
    await page.waitForTimeout(300);
    result[w] = await page.evaluate((vw) => {
      const bad = [];
      for (const el of document.body.querySelectorAll('*')) {
        if (el.closest('#lb')) continue;
        if (el.closest('.table-wrap') && !el.classList.contains('table-wrap')) continue;
        const r = el.getBoundingClientRect();
        if (r.width && r.right > vw + 1) {
          const cls = typeof el.className === 'string' && el.className ? '.' + el.className.split(' ')[0] : '';
          bad.push(el.tagName.toLowerCase() + cls + ' right=' + Math.round(r.right));
        }
      }
      return { scrollWidth: document.documentElement.scrollWidth, overflowing: bad.length, sample: bad.slice(0, 10) };
    }, w);
    const shot = path.join(out, `report-${w}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    result[w].screenshot = shot;
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify(result, null, 1));
  process.exit(Object.entries(result).some(([w, r]) => r.scrollWidth > Number(w)) ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
