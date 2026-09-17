// Autodidact background worker (MV3 service worker on Chromium, event page on Firefox).
//
// Receives captures from content scripts, applies the blocklist / pause /
// incognito rules, queues them in storage.local, and POSTs them to the server
// with retry. Everything here is stateless across restarts except the queue.

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = {
  serverUrl: 'http://localhost:8765',
  token: '',
  dwellSeconds: 8,
  pausedUntil: 0,
  blocklist: [
    'mail.google.com',
    'outlook.live.com',
    'outlook.office.com',
    '*.bank*',
    '*chase.com',
    '*wellsfargo.com',
    '*schwab.com',
    '*fidelity.com',
    '*paypal.com',
    '1password.com',
    '*.1password.com',
    'bitwarden.com',
    'vault.bitwarden.com',
    'localhost',
    '127.0.0.1',
    '192.168.*',
    '10.*',
    'accounts.google.com',
    'login.*',
    'auth.*',
    'sso.*',
  ],
};
const QUEUE_KEY = 'queue';
const QUEUE_MAX = 500;
const ALARM = 'autodidact-flush';

// ---------- settings ----------

async function getSettings() {
  const v = await api.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...v };
}

function globToRegex(glob) {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp('^' + esc + '$', 'i');
}

function isBlocked(url, blocklist) {
  let u;
  try { u = new URL(url); } catch { return true; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return true;
  const host = u.hostname;
  for (const raw of blocklist) {
    const pat = raw.trim();
    if (!pat || pat.startsWith('#')) continue;
    if (pat.includes('/')) {
      // URL prefix pattern, e.g. https://github.com/settings
      if (url.startsWith(pat)) return true;
      continue;
    }
    if (globToRegex(pat).test(host)) return true;
  }
  return false;
}

// ---------- queue ----------

async function enqueue(item) {
  let { [QUEUE_KEY]: q = [] } = await api.storage.local.get(QUEUE_KEY);
  if (item.path === '/dwell') {
    // Heartbeats for the same visit supersede each other; keep only the latest.
    q = q.filter((i) => !(i.path === '/dwell' && i.body.visit_id === item.body.visit_id));
  }
  q.push(item);
  while (q.length > QUEUE_MAX) q.shift();
  await api.storage.local.set({ [QUEUE_KEY]: q });
  await updateBadge();
}

let flushing = false;
async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    const settings = await getSettings();
    let { [QUEUE_KEY]: q = [] } = await api.storage.local.get(QUEUE_KEY);
    while (q.length) {
      const item = q[0];
      const res = await post(settings, item);
      if (res === 'ok' || res === 'drop') {
        q = q.slice(1);
        await api.storage.local.set({ [QUEUE_KEY]: q });
      } else {
        break; // network down; leave the queue for the alarm
      }
    }
  } finally {
    flushing = false;
    await updateBadge();
  }
}

async function post(settings, item) {
  const base = settings.serverUrl.replace(/\/+$/, '');
  try {
    const r = await fetch(base + item.path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + settings.token,
      },
      body: JSON.stringify(item.body),
    });
    if (r.ok) return 'ok';
    // 4xx means the server rejected this item; retrying will not help.
    if (r.status >= 400 && r.status < 500 && r.status !== 401 && r.status !== 429) {
      console.warn('autodidact: server rejected item', r.status, item.path);
      return 'drop';
    }
    return 'retry';
  } catch (e) {
    return 'retry';
  }
}

// ---------- badge ----------

async function updateBadge() {
  try {
    const settings = await getSettings();
    const { [QUEUE_KEY]: q = [] } = await api.storage.local.get(QUEUE_KEY);
    const paused = settings.pausedUntil > Date.now();
    let text = '';
    let color = '#3b82f6';
    if (paused) { text = 'II'; color = '#9ca3af'; }
    else if (q.length) { text = String(q.length); color = '#f59e0b'; }
    await api.action.setBadgeText({ text });
    await api.action.setBadgeBackgroundColor({ color });
  } catch { /* action API may be unavailable in some contexts */ }
}

// ---------- message handling ----------

async function handleCapture(payload, sender) {
  const settings = await getSettings();
  if (settings.pausedUntil > Date.now()) return { skipped: 'paused' };
  if (sender.tab?.incognito) return { skipped: 'incognito' };
  if (isBlocked(payload.url, settings.blocklist)) return { skipped: 'blocked' };
  if (payload.canonical_url && isBlocked(payload.canonical_url, settings.blocklist)) return { skipped: 'blocked' };
  await enqueue({ path: '/ingest', body: payload });
  flush();
  return { queued: true };
}

async function handleDwell(payload, sender) {
  const settings = await getSettings();
  if (sender.tab?.incognito) return { skipped: 'incognito' };
  if (isBlocked(payload.url, settings.blocklist)) return { skipped: 'blocked' };
  await enqueue({ path: '/dwell', body: { visit_id: payload.visit_id, dwell_s: payload.dwell_s } });
  flush();
  return { queued: true };
}

api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  let p;
  switch (msg?.type) {
    case 'capture': p = handleCapture(msg.payload, sender); break;
    case 'dwell': p = handleDwell(msg.payload, sender); break;
    case 'flush': p = flush().then(() => ({ ok: true })); break;
    case 'settings': p = getSettings(); break;
    case 'queueLength':
      p = api.storage.local.get(QUEUE_KEY).then((v) => ({ length: (v[QUEUE_KEY] || []).length }));
      break;
    case 'testConnection': p = testConnection(); break;
    default: return false;
  }
  p.then(sendResponse, (e) => sendResponse({ error: String(e) }));
  return true; // async response
});

async function testConnection() {
  const settings = await getSettings();
  const base = settings.serverUrl.replace(/\/+$/, '');
  try {
    const r = await fetch(base + '/stats', {
      headers: { 'Authorization': 'Bearer ' + settings.token },
    });
    if (!r.ok) return { ok: false, error: 'HTTP ' + r.status };
    return { ok: true, stats: await r.json() };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// ---------- lifecycle ----------

api.runtime.onInstalled.addListener(async () => {
  const current = await api.storage.local.get(null);
  const fill = {};
  for (const [k, v] of Object.entries(DEFAULTS)) if (!(k in current)) fill[k] = v;
  if (Object.keys(fill).length) await api.storage.local.set(fill);
  api.alarms.create(ALARM, { periodInMinutes: 1 });
  updateBadge();
});
api.runtime.onStartup?.addListener(() => {
  api.alarms.create(ALARM, { periodInMinutes: 1 });
  flush();
});
api.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) flush(); });
api.storage.onChanged.addListener(() => updateBadge());
