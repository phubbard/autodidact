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
const SRC = fs.readFileSync(path.join(here, '../src/background.js'), 'utf8');
const VECTORS = JSON.parse(fs.readFileSync(path.join(here, '../../server/tests/policy_vectors.json'), 'utf8'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function worker({ store = {}, respond }) {
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
  const ctx = vm.createContext({ chrome, console: { ...console, warn() {}, info() {} }, URL, setTimeout, fetch });
  ctx.globalThis = ctx;
  vm.runInContext(SRC, ctx);
  const send = (msg) => new Promise((res) => L.message[0](msg, { tab: {} }, res));
  return { ctx, store, requests, send, alarm: () => L.alarm.forEach((f) => f({ name: 'autodidact-flush' })) };
}

const SETTINGS = { serverUrl: 'https://autodidact.example', token: 't' };
const POLICY = { version: 'v1', skip_front_pages: true, rules: ['*youtube.com/shorts/', 'axiom.phfactor.net'] };
const page = (url, id) => ({ url, visit_id: id, text: 'x'.repeat(300) });
let passed = 0;
const test = async (name, fn) => { await fn(); passed++; console.log('ok -', name); };

await test('policy matcher agrees with the server vectors', () => {
  const { ctx } = worker({ respond: () => [200, {}] });
  for (const [url, expected] of VECTORS.cases) {
    assert.equal(ctx.policyMatch(url, VECTORS.rules, VECTORS.skip_front_pages, VECTORS.allow), expected, url);
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

await test('an item the server keeps failing on is dropped after MAX_SERVER_ERRORS; the queue moves on', async () => {
  const w = worker({
    store: { ...SETTINGS, serverPolicy: { version: 'v', skipFrontPages: false, rules: [], fetchedAt: Date.now() } },
    respond: (p, opt) => (p === '/ingest' && JSON.parse(opt.body).visit_id === 'poison' ? [500, {}] : [200, {}]),
  });
  await w.send({ type: 'capture', payload: page('https://example.com/poison', 'poison') });
  await w.send({ type: 'capture', payload: page('https://example.com/good', 'good') });
  const max = vm.runInContext('MAX_SERVER_ERRORS', w.ctx);  // top-level const: not a property of the global
  assert.equal(max, 20);
  for (let i = 0; i < max + 2; i++) { w.alarm(); await sleep(5); }
  assert.equal((w.store.queue || []).length, 0, 'queue drained');
  assert.equal(w.requests.filter((p) => p === '/ingest').length, max + 1, 'poison tried max times, then the good one once');
});

await test('a network failure is never given up on', async () => {
  const w = worker({ store: { ...SETTINGS, serverPolicy: { version: 'v', skipFrontPages: false, rules: [], fetchedAt: Date.now() } },
                     respond: () => { throw new TypeError('Load failed'); } });
  await w.send({ type: 'capture', payload: page('https://example.com/a', 'a') });
  for (let i = 0; i < 30; i++) { w.alarm(); await sleep(2); }
  assert.equal(w.store.queue.length, 1);
  assert.equal(w.store.queue[0].serverErrors, undefined);
});

console.log(`${passed} passed`);
