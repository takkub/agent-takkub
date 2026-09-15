const { pathToFileURL } = require('url');
const { chromium } = require(process.argv[2]);
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 390, height: 844 } });
  await p.goto(pathToFileURL(process.argv[3]).href);
  await p.waitForTimeout(500);
  const r = await p.evaluate(() => [...document.querySelectorAll('.table-wrap')].map(t => {
    const sec = t.closest('section');
    const widest = [...t.querySelectorAll('td,th')].map(c => ({ w: c.scrollWidth, txt: c.textContent.trim().slice(0, 40) })).sort((a, b) => b.w - a.w)[0];
    return { section: (sec && (sec.id || sec.querySelector('h2')?.textContent)) || '?', cols: t.querySelectorAll('thead th').length, client: t.clientWidth, scroll: t.scrollWidth, widest };
  }).filter(x => x.scroll > x.client + 1));
  console.log(JSON.stringify(r, null, 1));
  await b.close();
})();
