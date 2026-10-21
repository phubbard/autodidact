// Autodidact background worker (MV3 service worker on Chromium, event page on Firefox).
//
// Receives captures from content scripts, applies the blocklist / pause /
// incognito rules, queues them in storage.local, and POSTs them to the server
// with retry. Everything here is stateless across restarts except the queue.

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = {
  serverUrl: 'http://localhost:8765',
  token: '',
  deviceName: '',
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
const MAX_SERVER_ERRORS = 20;
const ALARM = 'autodidact-flush';

// ---------- settings ----------

async function getSettings() {
  await syncSettingsBackup();
  const v = await api.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...v };
}

// ---------- settings backup (Safari) ----------
//
// Reinstalling the iOS app with a new build on 2026-10-05 came back with an
// empty server URL and token. On Safari the settings are therefore mirrored
// into the app extension's own UserDefaults (SafariWebExtensionHandler.swift),
// which survive app updates, and restored from there whenever storage.local
// has no token. Chromium and Firefox builds have no nativeMessaging permission
// (build.sh strips it), so sendNativeMessage is absent and all of this is a no-op.
const BACKUP_KEYS = ['serverUrl', 'token', 'deviceName', 'dwellSeconds', 'blocklist'];

async function nativeCall(message) {
  if (typeof api.runtime.sendNativeMessage !== 'function') return null;
  try {
    return await api.runtime.sendNativeMessage('net.phfactor.autodidact', message);
  } catch {
    return null;
  }
}

async function backupSettings() {
  const s = await api.storage.local.get(BACKUP_KEYS);
  // Only a complete configuration is worth keeping; never overwrite a good backup with blanks.
  if (!s.token || !s.serverUrl) return;
  await nativeCall({ type: 'saveSettings', settings: s });
}

// Once per worker lifetime: restore from the backup if storage lost the token,
// otherwise refresh the backup from storage.
let backupSynced = null;
function syncSettingsBackup() {
  backupSynced ??= (async () => {
    const { token } = await api.storage.local.get('token');
    if (token) return backupSettings();
    const saved = (await nativeCall({ type: 'loadSettings' }))?.settings;
    if (!saved?.token || !saved?.serverUrl) return;
    const restore = {};
    for (const k of BACKUP_KEYS) if (saved[k] !== undefined && saved[k] !== null) restore[k] = saved[k];
    await api.storage.local.set(restore);
    console.info('autodidact: settings restored from the app backup');
  })().catch(() => {});
  return backupSynced;
}

function globToRegex(glob) {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp('^' + esc + '$', 'i');
}

// The search UI on the server itself is never worth capturing.
function isServerPage(url, settings) {
  try { return new URL(url).origin === new URL(settings.serverUrl).origin; } catch { return false; }
}

// The rule in *rules* that matches *url*, or null. Same syntax and semantics as server/policy.py
// (both are checked against server/tests/policy_vectors.json by extension/test/unit.mjs):
//   example.com / *.bank*        host glob
//   *youtube.com/shorts/         host glob + path prefix (path and query string)
//   https://github.com/settings  full URL prefix
// With skipFrontPages, a site's front page (path "/" or empty) matches as "front page".
// Anything an *allow* rule matches (the server's "!allow" lines, e.g. *.substack.com) is never blocked.
function ruleHits(url, host, pathAndQuery, raw) {
  const rule = raw.trim();
  if (!rule || rule.startsWith('#') || rule.startsWith('!')) return false;
  if (/^https?:\/\//.test(rule)) return url.startsWith(rule);
  if (rule.includes('/')) {
    const i = rule.indexOf('/');
    return globToRegex(rule.slice(0, i)).test(host) && pathAndQuery.startsWith(rule.slice(i));
  }
  return globToRegex(rule).test(host);
}

function policyMatch(url, rules, skipFrontPages = false, allow = []) {
  let u;
  try { u = new URL(url); } catch { return 'unparseable URL'; }
  if ((u.protocol !== 'http:' && u.protocol !== 'https:') || !u.hostname) return 'not http(s)';
  const host = u.hostname;
  const pathAndQuery = (u.pathname || '/') + u.search;
  if (allow.some((a) => ruleHits(url, host, pathAndQuery, a))) return null;
  if (skipFrontPages && (u.pathname === '/' || u.pathname === '')) return 'front page';
  for (const raw of rules) {
    if (ruleHits(url, host, pathAndQuery, raw)) return raw.trim();
  }
  return null;
}

function isBlocked(url, blocklist) {
  return policyMatch(url, blocklist) !== null;
}

// ---------- server policy ----------
//
// The shared capture rules live on the Pi (data/policy.txt, served at GET /policy) so one edit
// reaches every browser. Cached in storage.local and refreshed hourly from the flush alarm; if the
// server is unreachable the last copy keeps applying. The local Settings blocklist applies on top.
const POLICY_KEY = 'serverPolicy';
const POLICY_MAX_AGE_MS = 60 * 60 * 1000;

async function refreshPolicy(force = false) {
  const { [POLICY_KEY]: cached } = await api.storage.local.get(POLICY_KEY);
  if (!force && cached && Date.now() - cached.fetchedAt < POLICY_MAX_AGE_MS) return cached;
  const settings = await getSettings();
  try {
    const r = await fetch(settings.serverUrl.replace(/\/+$/, '') + '/policy', { headers: deviceHeader(settings) });
    if (!r.ok) return cached ?? null;
    const p = await r.json();
    const fresh = { version: p.version, skipFrontPages: !!p.skip_front_pages, rules: p.rules || [],
                    allow: p.allow || [], fetchedAt: Date.now() };
    await api.storage.local.set({ [POLICY_KEY]: fresh });
    return fresh;
  } catch {
    return cached ?? null;
  }
}

async function serverPolicyMatch(url) {
  const p = await refreshPolicy();
  return p ? policyMatch(url, p.rules, p.skipFrontPages, p.allow || []) : null;
}

// ---------- queue ----------

// Safari has rejected storage.local.set() here with "Exceeded storage quota"
// while the queue held only a couple of pages, including when the write made
// the queue smaller. A sent item was then never removed and was re-posted every
// minute. The manifest asks for unlimitedStorage, which lifts the quota (the
// queue can also legitimately pass 10 MB when the server is unreachable for
// days). As a fallback, drop the key first so the write is not measured against
// the old value.
async function saveQueue(q) {
  try {
    await api.storage.local.set({ [QUEUE_KEY]: q });
  } catch (e) {
    await api.storage.local.remove(QUEUE_KEY);
    if (q.length) await api.storage.local.set({ [QUEUE_KEY]: q });
  }
}

async function enqueue(item) {
  let { [QUEUE_KEY]: q = [] } = await api.storage.local.get(QUEUE_KEY);
  if (item.path === '/dwell') {
    // Heartbeats for the same visit supersede each other; keep only the latest.
    q = q.filter((i) => !(i.path === '/dwell' && i.body.visit_id === item.body.visit_id));
  }
  q.push(item);
  while (q.length > QUEUE_MAX) q.shift();
  await saveQueue(q);
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
        await saveQueue(q);
      } else if (res === 'server-error') {
        // A 5xx for this particular item. Twice (2026-10-09, 2026-10-19) one item the server could
        // not store sat at the head of the queue for hours and everything behind it waited. Give up
        // on an item after MAX_SERVER_ERRORS; the server logs the traceback for each attempt.
        item.serverErrors = (item.serverErrors || 0) + 1;
        if (item.serverErrors >= MAX_SERVER_ERRORS) {
          console.warn('autodidact: dropping item after repeated server errors', item.path, item.body?.url);
          q = q.slice(1);
          await saveQueue(q);
          continue;
        }
        q[0] = item;
        await saveQueue(q);
        break;
      } else {
        break; // network down, or a token/rate problem: leave the queue for the alarm
      }
    }
  } finally {
    flushing = false;
    await updateBadge();
  }
}

// Sent on every request so the server's /debug page can say which machine it
// came from; reverse DNS on the LAN only gives generic DHCP names. URL-encoded
// because fetch() throws on header values outside Latin-1 (a curly apostrophe
// in "Paul’s iPhone" would otherwise wedge the queue).
function deviceHeader(settings) {
  return settings.deviceName ? { 'X-Autodidact-Device': encodeURIComponent(settings.deviceName) } : {};
}

async function post(settings, item) {
  const base = settings.serverUrl.replace(/\/+$/, '');
  try {
    const r = await fetch(base + item.path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + settings.token,
        ...deviceHeader(settings),
      },
      body: JSON.stringify(item.body),
    });
    if (r.ok) return 'ok';
    // 4xx means the server rejected this item; retrying will not help.
    if (r.status >= 400 && r.status < 500 && r.status !== 401 && r.status !== 429) {
      console.warn('autodidact: server rejected item', r.status, item.path);
      return 'drop';
    }
    if (r.status >= 500) return 'server-error';
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
  if (isServerPage(payload.url, settings)) return { skipped: 'server' };
  if (isBlocked(payload.url, settings.blocklist)) return { skipped: 'blocked' };
  if (payload.canonical_url && isBlocked(payload.canonical_url, settings.blocklist)) return { skipped: 'blocked' };
  const rule = await serverPolicyMatch(payload.url);
  if (rule) return { skipped: 'policy', rule };
  await enqueue({ path: '/ingest', body: payload });
  flush();
  return { queued: true };
}

async function handleDwell(payload, sender) {
  const settings = await getSettings();
  if (sender.tab?.incognito) return { skipped: 'incognito' };
  if (isServerPage(payload.url, settings)) return { skipped: 'server' };
  if (isBlocked(payload.url, settings.blocklist)) return { skipped: 'blocked' };
  if (await serverPolicyMatch(payload.url)) return { skipped: 'policy' };
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
      headers: { 'Authorization': 'Bearer ' + settings.token, ...deviceHeader(settings) },
    });
    if (!r.ok) return { ok: false, error: 'HTTP ' + r.status };
    await refreshPolicy(true);
    return { ok: true, stats: await r.json() };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// ---------- lifecycle ----------

api.runtime.onInstalled.addListener(async () => {
  await syncSettingsBackup(); // restore before defaults are filled in
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
api.alarms.onAlarm.addListener((a) => {
  if (a.name !== ALARM) return;
  flush();
  refreshPolicy(); // no-op unless the cached policy is over an hour old
});
api.storage.onChanged.addListener((changes, area) => {
  updateBadge();
  if (area === 'local' && BACKUP_KEYS.some((k) => k in changes)) backupSettings();
});
