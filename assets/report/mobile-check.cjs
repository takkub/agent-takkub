const { pathToFileURL } = require('url');
const path = require('path');
const { chromium } = require(process.argv[2]);
(async () => {
  const b = await chromium.launch();
  const out = {};
  const url = pathToFileURL(process.argv[3]).href;
  for (const w of [360, 390, 768]) {
    const p = await b.newPage({ viewport: { width: w, height: 844 }, deviceScaleFactor: 2 });
    await p.goto(url);
    await p.waitForTimeout(800);
    out[w] = await p.evaluate((vw) => {
      const bad = [];
      for (const el of document.querySelectorAll('section.sys, section.sys *')) {
        if (el.closest('.table-wrap') && !el.classList.contains('table-wrap')) continue;
        const r = el.getBoundingClientRect();
        if (r.width && r.right > vw + 1) bad.push(el.tagName + '.' + el.className + ' right=' + Math.round(r.right));
      }
      const tw = [...document.querySelectorAll('.table-wrap')].filter(t => t.scrollWidth > t.clientWidth + 1).length;
      return { scrollW: document.documentElement.scrollWidth, badCount: bad.length, overflowing: bad.slice(0, 15), scrollingTables: tw };
    }, w);
    if (w === 390) await p.locator('#retest').screenshot({ path: path.join(process.argv[4], 'mobile-390-retest.png') });
    await p.close();
  }
  console.log(JSON.stringify(out, null, 1));
  await b.close();
})();
