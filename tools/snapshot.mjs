// Builds data/snapshot.json, the precomputed file that lets the site draw instantly.
//
// Opens the served site in a headless browser with ?snapshot=1 (so it computes everything fresh
// instead of reading an older snapshot), waits for the page's own buildSnapshot() to finish the
// tier lists, meta builds and roster data, and writes what it returns.
//
// Usage:
//   node tools/snapshot.mjs <site url> <output file>
//   e.g. node tools/snapshot.mjs http://localhost:8765/ data/snapshot.json
//
// Needs `npm install playwright-core` and an installed Chrome (SNAPSHOT_BROWSER=msedge for Edge).
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { chromium } from 'playwright-core';

const [site, out] = process.argv.slice(2);
if (!site || !out) {
  console.error('Usage: node tools/snapshot.mjs <site url> <output file>');
  process.exit(2);
}

// CI runners can't use Chrome's sandbox; the page only ever loads our own site and its data API.
const browser = await chromium.launch({ channel: process.env.SNAPSHOT_BROWSER || 'chrome', args: process.env.CI ? ['--no-sandbox'] : [] });
try {
  const page = await browser.newPage();
  page.setDefaultTimeout(5 * 60 * 1000);
  page.on('pageerror', err => console.error('Page error:', err.message));
  const url = new URL(site);
  url.searchParams.set('snapshot', '1');
  const started = Date.now();
  await page.goto(url.href, { waitUntil: 'load' });
  const json = await page.evaluate(() => window.buildSnapshot());
  const snap = JSON.parse(json);
  const tiers = Object.entries(snap.tiers).map(([rank, t]) => `${rank}: ${t ? `${t.stats.length} heroes` : 'not available'}`).join(', ');
  if (!snap.tiers.all) throw new Error('the tier list did not come from the match model, so there is nothing worth precomputing');
  await mkdir(dirname(out), { recursive: true });
  await writeFile(out, json);
  console.log(`Snapshot written in ${Math.round((Date.now() - started) / 1000)}s: ${(json.length / 1024).toFixed(0)} KB, `
    + `${snap.heroes.length} heroes, ${snap.items.length} items, tiers ${tiers}, ${Object.keys(snap.topBuilds?.heroes ?? {}).length} meta builds.`);
} finally {
  await browser.close();
}
