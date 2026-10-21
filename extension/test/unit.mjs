// Unit tests for background.js, run with plain node (no browser, no dependencies):
//   node extension/test/unit.mjs
// Loads the worker into a vm with a stubbed WebExtension API. Covers the policy matcher (against the
// same vectors as server/tests/test_policy.py), policy fetch/enforcement, and queue behaviour.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(process.env.BACKGROUND_JS || path.join(here, '../src/background.js'), 'utf8');  // override to test an older build
const VECTORS = JSON.parse(fs.readFileSync(path.join(here, '../../server/tests/policy_vectors.json'), 'utf8'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function worker({ store = {}, respond, clock = { now: Date.now() } }) {
  const L = {};
  const ev = (n) => ({ addListener: (f) => { (L[n] ||= []).push(f); } });
  const requests = [];
  const local = {
    get: async (k) => {
      if (k === null) return structuredClone(store);
      if (typeof k === 'string') k = [k];
      if (Array.isArray(k)) return Object.fromEntries(k.filter((x) => x in store).map((x) => [x, structuredClone(store[x])]));
      return Object.fromEntries(Object.entries(k).map(([x, d]) => [x, x in store ? structuredClone(store[x]) : d]));
    },
    set: async (o) => { for (const [x, v] of Object.entries(o)) store[x] = structuredClone(v); },
    remove: async (x) => { delete store[x]; },
  };
  const chrome = {
    storage: { local, onChanged: ev('changed') },
    runtime: { onMessage: ev('message'), onInstalled: ev('installed'), onStartup: ev('startup') },
    alarms: { create() {}, onAlarm: ev('alarm') },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  };
  const fetch = async (url, opt = {}) => {
    const p = new URL(url).pathname;
    requests.push(p);
    const [status, body] = respond(p, opt);
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  class FakeDate extends Date { static now() { return clock.now; } }
  const ctx = vm.createContext({ chrome, console: { ...console, warn() {}, info() {} }, URL, setTimeout, fetch, Date: FakeDate });
  ctx.globalThis = ctx;
  vm.runInContext(SRC, ctx);
  const send = (msg) => new Promise((res) => L.message[0](msg, { tab: {} }, res));
  return { ctx, store, requests, send, clock, alarm: () => L.alarm.forEach((f) => f({ name: 'autodidact-flush' })) };
}

const SETTINGS = { serverUrl: 'https://autodidact.example', token: 't' };
const POLICY = { version: 'v1', skip_front_pages: true, rules: ['*youtube.com/shorts/', 'axiom.phfactor.net'] };
const page = (url, id) => ({ url, visit_id: id, text: 'x'.repeat(300) });
let passed = 0;
const ONLY = process.env.ONLY;  // run only tests whose name contains this
const test = async (name, fn) => { if (ONLY && !name.includes(ONLY)) return; await fn(); passed++; console.log('ok -', name); };

await test('policy matcher agrees with the server vectors', () => {
  const { ctx } = worker({ respond: () => [200, {}] });
  for (const [url, expected, page] of VECTORS.cases) {
    assert.equal(ctx.policyMatch(url, VECTORS.rules, VECTORS.skip_front_pages, VECTORS.allow,
                                 VECTORS.allow_platforms, page ?? null), expected, url);
  }
});

await test('local blocklist keeps its old meaning', () => {
  const { ctx } = worker({ respond: () => [200, {}] });
  assert.equal(ctx.isBlocked('https://mail.google.com/x', ['mail.google.com']), true);
  assert.equal(ctx.isBlocked('https://github.com/settings/keys', ['https://github.com/settings']), true);
  assert.equal(ctx.isBlocked('https://github.com/x', ['https://github.com/settings']), false);
  assert.equal(ctx.isBlocked('https://192.168.1.5/', ['192.168.*']), true);
});

await test('server policy is fetched, cached and applied to captures and dwell', async () => {
  const w = worker({ store: { ...SETTINGS }, respond: (p) => (p === '/policy' ? [200, POLICY] : [200, { id: 1 }]) });
  assert.deepEqual({ ...(await w.send({ type: 'capture', payload: page('https://news.ycombinator.com/', 'a') })) },
                   { skipped: 'policy', rule: 'front page' });
  assert.equal((await w.send({ type: 'capture', payload: page('https://www.youtube.com/shorts/z', 'b') })).rule, '*youtube.com/shorts/');
  assert.equal((await w.send({ type: 'dwell', payload: { url: 'https://axiom.phfactor.net/x', visit_id: 'c', dwell_s: 15 } })).skipped, 'policy');
  assert.equal((await w.send({ type: 'capture', payload: page('https://www.youtube.com/watch?v=1', 'd') })).queued, true);
  await sleep(20);
  assert.equal(w.requests.filter((p) => p === '/policy').length, 1, 'policy fetched once and cached');
  assert.deepEqual(w.requests.filter((p) => p === '/ingest'), ['/ingest']);
});

await test('unreachable policy server: last cached copy still applies, nothing else blocked', async () => {
  const cached = { version: 'old', skipFrontPages: true, rules: [], fetchedAt: 0 };
  const w = worker({ store: { ...SETTINGS, serverPolicy: cached }, respond: (p) => (p === '/policy' ? [503, {}] : [200, {}]) });
  assert.equal((await w.send({ type: 'capture', payload: page('https://example.com/', 'a') })).rule, 'front page');
  const fresh = worker({ store: { ...SETTINGS }, respond: (p) => (p === '/policy' ? [503, {}] : [200, {}]) });
  assert.equal((await fresh.send({ type: 'capture', payload: page('https://example.com/', 'a') })).queued, true);
});

const POLICY_NONE = { version: 'v', skipFrontPages: false, rules: [], allow: [], allowPlatforms: [], fetchedAt: 8.64e15 };
const MIN = 60 * 1000;
async function drain(w, rounds = 1) { for (let i = 0; i < rounds; i++) { w.alarm(); await sleep(5); } }
const posted = (w) => w.requests.filter((p) => p === '/ingest' || p === '/dwell');

await test('a failing item backs off on its own; the items behind it go ahead', async () => {
  const w = worker({
    store: { ...SETTINGS, serverPolicy: POLICY_NONE },
    respond: (p, opt) => (p === '/ingest' && JSON.parse(opt.body).visit_id === 'poison' ? [500, {}] : [200, {}]),
  });
  await w.send({ type: 'capture', payload: page('https://example.com/poison', 'poison') });
  await w.send({ type: 'capture', payload: page('https://example.com/good', 'good') });
  await sleep(20);
  assert.deepEqual(w.store.queue.map((i) => i.body.visit_id), ['poison'], 'good one delivered past the failing one');
  const n = posted(w).length;
  await drain(w, 3);
  assert.equal(posted(w).length, n, 'not retried before its backoff expires');
  w.clock.now += 3 * MIN;
  await drain(w);
  assert.equal(posted(w).length, n + 1, 'retried once the backoff passed');
  assert.equal(w.store.queue[0].attempts, 2);
});

await test('backoff grows to an hour; the item is dropped after MAX_SERVER_ERRORS', async () => {
  const w = worker({ store: { ...SETTINGS, serverPolicy: POLICY_NONE }, respond: (p) => (p === '/ingest' ? [500, {}] : [200, {}]) });
  await w.send({ type: 'capture', payload: page('https://example.com/a', 'a') });
  await sleep(10);
  const max = vm.runInContext('MAX_SERVER_ERRORS', w.ctx);
  const waits = [];
  for (let i = 1; i < max && w.store.queue.length; i++) {
    waits.push(Math.round((w.store.queue[0].notBefore - w.clock.now) / MIN));
    w.clock.now = w.store.queue[0].notBefore + 1;
    await drain(w);
  }
  assert.ok(waits[0] >= 0.8 && waits[0] <= 1.2 && waits[3] >= 6 && waits[3] <= 10, `grows: ${waits}`);
  assert.ok(Math.max(...waits) <= 72, `capped near an hour: ${waits}`);
  assert.equal((w.store.queue || []).length, 0, 'dropped');
  assert.equal(posted(w).length, max);
});

await test('unreachable server: the pass stops, nothing backs off or is dropped', async () => {
  const w = worker({ store: { ...SETTINGS, serverPolicy: POLICY_NONE }, respond: () => { throw new TypeError('Load failed'); } });
  await w.send({ type: 'capture', payload: page('https://example.com/a', 'a') });
  await w.send({ type: 'capture', payload: page('https://example.com/b', 'b') });
  await drain(w, 30);
  assert.equal(w.store.queue.length, 2);
  assert.ok(w.store.queue.every((i) => i.attempts === undefined && i.notBefore === undefined));
  assert.ok(w.requests.filter((p) => p === '/ingest').length <= 33, 'one attempt per pass, not one per item');
});

await test('a dwell update waits for its own capture', async () => {
  let fail = true;
  const w = worker({ store: { ...SETTINGS, serverPolicy: POLICY_NONE }, respond: (p) => (p === '/ingest' && fail ? [503, {}] : [200, {}]) });
  await w.send({ type: 'capture', payload: page('https://example.com/a', 'v1') });
  await w.send({ type: 'dwell', payload: { url: 'https://example.com/a', visit_id: 'v1', dwell_s: 30 } });
  await sleep(20);
  assert.equal(w.requests.filter((p) => p === '/dwell').length, 0);
  fail = false;
  w.clock.now += 3 * MIN;
  await drain(w);
  assert.deepEqual(posted(w).slice(-2), ['/ingest', '/dwell']);
  assert.equal(w.store.queue.length, 0);
});

await test('a capture that arrives during a flush is not lost', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const w = worker({
    store: { ...SETTINGS, serverPolicy: POLICY_NONE },
    respond: (p, opt) => [200, {}],
  });
  // Slow the first POST so a second capture is enqueued while flush() is mid-flight.
  const realFetch = w.ctx.fetch;
  let first = true;
  w.ctx.fetch = async (...args) => { if (first) { first = false; await gate; } return realFetch(...args); };
  await w.send({ type: 'capture', payload: page('https://example.com/a', 'a') });
  await w.send({ type: 'capture', payload: page('https://example.com/b', 'b') });
  release();
  await sleep(20);
  await drain(w);
  assert.deepEqual(w.requests.filter((p) => p === '/ingest').length, 2);
  assert.equal(w.store.queue.length, 0);
});

await test('items older than two weeks are dropped; old-format items get ids', async () => {
  const w = worker({ store: { ...SETTINGS, serverPolicy: POLICY_NONE,
    queue: [{ path: '/ingest', body: page('https://example.com/legacy', 'L') }] }, respond: () => [503, {}] });
  await drain(w);
  assert.ok(w.store.queue[0].qid && w.store.queue[0].enqueuedAt, 'legacy item adopted');
  w.clock.now += 15 * 24 * 60 * MIN;
  await drain(w);
  assert.equal(w.store.queue.length, 0);
});

await test('blog front pages are kept when the policy allows the platform', async () => {
  const pol = { version: 'v', skip_front_pages: true, rules: [], allow: [], allow_platforms: ['wordpress'] };
  const w = worker({ store: { ...SETTINGS }, respond: (p) => (p === '/policy' ? [200, pol] : [200, {}]) });
  const blog = { ...page('https://www.someblog.net/', 'b'), platform: 'wordpress', blog_front: true };
  const news = { ...page('https://www.bignews.com/', 'n'), platform: 'wordpress', blog_front: false };
  assert.equal((await w.send({ type: 'capture', payload: blog })).queued, true);
  assert.equal((await w.send({ type: 'capture', payload: news })).rule, 'front page');
});

console.log(`${passed} passed`);
