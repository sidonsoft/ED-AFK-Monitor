// Optional documentation tool; requires Playwright, not used by the monitor.
const { chromium } = require('playwright');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const fs = require('node:fs/promises');
(async () => {
  const root = path.resolve(__dirname, '..');
  const input = path.resolve(process.argv[2] || path.join(root, 'work/readme-previews'));
  const output = path.resolve(process.argv[3] || path.join(root, 'images'));
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({
    ...(process.env.PREVIEW_CHROME ? { executablePath: process.env.PREVIEW_CHROME } : {})
  });
  try {
    for (const [name, width] of [['terminal', 1480], ['discord', 820]]) {
      const page = await browser.newPage({ viewport: { width, height: 1000 }, deviceScaleFactor: 1 });
      await page.goto(pathToFileURL(path.join(input, `${name}.html`)).href);
      await page.evaluate(() => document.fonts.ready);
      await page.locator(".frame").screenshot({ path: path.join(output, `wing-stacks-${name}.png`) });
      await page.close();
    }
  } finally { await browser.close(); }
})();
