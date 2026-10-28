// Standalone synthetic browser regression; no real proof, credentials or writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const staticRoot = path.resolve(__dirname, '../../static');
const photoId = '11111111-1111-4111-8111-111111111111';
const proof = {
  title: 'Synthetic wedding album', design_format: 'saal_portrait_22x30',
  photos: [{ id: photoId, name: 'Synthetic photograph' }], cover_photo_id: photoId,
  spreads: Array.from({ length: 17 }, (_, i) => ({ id: String(i), layout: 'hero', slots: [{ photo_id: photoId }] })),
  feedback: [], approved: null, offers: [],
  offer: { product: 'saal', product_label: 'Saal Professional Line', base_eur: 150,
    closed_size: '22×30 cm', cover_ratio: 193/300, spread_ratio: 438/294 },
};
let writes = 0;
let unlocked = false;
const server = http.createServer((req, res) => {
  if (req.method !== 'GET') { writes++; res.writeHead(400); res.end(); return; }
  if (req.url.endsWith('/api')) {
    if (!unlocked) { res.writeHead(401, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: 'Enter the review password' })); return; }
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(proof)); return;
  }
  const files = { '/review/synthetic': 'review.html', '/review-app.js': 'review-app.js', '/review-app.css': 'review-app.css' };
  const file = files[req.url];
  if (!file) { res.writeHead(404); res.end(); return; }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(path.join(staticRoot, file)));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/review/synthetic`);
    await page.getByRole('button', { name: 'Open album →' }).focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(errors.length, 0, 'Reader shortcuts must stay inactive before unlocking');
    unlocked = true;
    await page.reload();
    await page.getByRole('button', { name: 'Full screen ↗' }).click();
    await page.getByRole('button', { name: 'View spread 17', exact: true }).click();
    assert.equal(await page.evaluate(() => document.activeElement.dataset.index), '16');
    await page.getByRole('button', { name: 'View spread 16', exact: true }).click();
    await page.getByRole('button', { name: 'Next spread' }).click();
    assert(await page.getByRole('button', { name: 'Next spread' }).isDisabled());
    assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'toggle-reader');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.index), '16');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'toggle-reader');
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'toggle-reader');
    assert.equal(writes, 0);
    assert.equal(errors.length, 0);
    console.log('Reader thumbnails retain focus; boundary navigation and full-screen Tab/Escape stay accessible; browsing makes no writes.');
  } finally {
    await browser.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
