// End-to-end: load the built Chromium extension in headless Chromium, configure it
// through its own options page, visit a local article, and check the server got it.
import { chromium } from 'playwright';
import http from 'node:http';

const EXT = '/home/claude/autodidact/extension/dist/chromium';
const SERVER = 'http://127.0.0.1:8765';
const TOKEN = 'e2e-token';

// A local "article" page, plus an SPA-ish second route.
const article = (n) => `<!doctype html><html><head><title>Article ${n}: Raspberry Pi WAL tuning</title>
<meta name="description" content="Notes on SQLite WAL mode on a Pi ${n}"></head><body>
<nav>Home | About | Contact — this nav text must not be captured</nav>
<main><article><h1>Article ${n}: Raspberry Pi WAL tuning</h1>
${'<p>SQLite write-ahead logging on an SD card behaves differently from NVMe. Checkpoint interval, synchronous=NORMAL, and page size all matter for a Raspberry Pi ' + n + '. Readers do not block writers.</p>'.repeat(6)}
<button onclick="history.pushState({}, '', '/article/2'); document.title='Article 2: Raspberry Pi WAL tuning'; document.querySelector('h1').textContent='Article 2 rewritten'; document.querySelector('article').insertAdjacentHTML('beforeend','<p>Part two covers mmap and the busy timeout in far more depth than part one ever did, with numbers from a Pi 5.</p>'.repeat(4))">next</button>
</article></main>
<footer>Footer boilerplate copyright text must not be captured either</footer>
</body></html>`;
const site = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.end(article(req.url.includes('/2') ? 2 : 1));
}).listen(8091);

const ctx = await chromium.launchPersistentContext('/tmp/e2e-profile-' + Date.now(), {
  headless: true,
  // executablePath: '/path/to/chrome', // set if Playwright's bundled Chromium is not installed
  args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
});
let [sw] = ctx.serviceWorkers();
if (!sw) sw = await ctx.waitForEvent('serviceworker');
const extId = new URL(sw.url()).host;
console.log('extension id', extId);

// Configure via the options page.
const opt = await ctx.newPage();
await opt.goto(`chrome-extension://${extId}/options.html`);
await opt.fill('#serverUrl', SERVER);
await opt.fill('#token', TOKEN);
await opt.fill('#dwellSeconds', '3');
// The default blocklist includes 127.0.0.1, which is where the test site lives.
await opt.fill('#blocklist', 'mail.google.com\n192.168.*');
await opt.click('#test');
await opt.waitForFunction(() => /Connected|Failed/.test(document.querySelector('#status').textContent));
console.log('options:', await opt.textContent('#status'));

// Visit the article and dwell.
const page = await ctx.newPage();
await page.goto('http://127.0.0.1:8091/article/1');
await page.bringToFront();
await page.waitForTimeout(5000);

// SPA navigation, dwell again.
await page.click('button');
await page.waitForTimeout(5000);
await page.goto('about:blank'); // triggers pagehide → dwell flush
await page.waitForTimeout(2000);

const search = async (q) => (await (await fetch(`${SERVER}/search?q=${encodeURIComponent(q)}`)).json());
const stats = await (await fetch(`${SERVER}/stats`)).json();
console.log('stats', stats);
const r1 = await search('checkpoint interval');
console.log('hits for "checkpoint interval":', r1.results.map((h) => [h.url, h.title, h.visit_count, h.total_dwell_s]));
const r2 = await search('busy timeout');
console.log('hits for "busy timeout":', r2.results.map((h) => [h.url, h.title]));
const full = await (await fetch(`${SERVER}/page/${r1.results[0].id}`)).json();
console.log('nav captured?', full.text.includes('must not be captured'));
console.log('text head:', JSON.stringify(full.text.slice(0, 120)));

await ctx.close();
site.close();
const ok = stats.pages === 2 && r1.results.length >= 1 && r1.results[0].total_dwell_s >= 4 && r2.results.length === 1 && !full.text.includes('must not be captured');
console.log(ok ? 'E2E PASS' : 'E2E FAIL');
process.exit(ok ? 0 : 1);
