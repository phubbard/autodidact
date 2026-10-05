const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);

// Settings come from the background worker (which merges defaults and, on
// Safari, restores a lost configuration from the app's backup). If that fails
// the form stays locked: saving a form that never loaded would write blanks
// over the real server URL and token.
let loaded = false;

async function load() {
  for (let attempt = 0; attempt < 4 && !loaded; attempt++) {
    try {
      const s = await api.runtime.sendMessage({ type: 'settings' });
      if (!s || typeof s !== 'object' || !('serverUrl' in s)) throw new Error('no settings in reply');
      $('serverUrl').value = s.serverUrl || '';
      $('token').value = s.token || '';
      $('deviceName').value = s.deviceName || '';
      $('dwellSeconds').value = s.dwellSeconds ?? 8;
      $('blocklist').value = (s.blocklist || []).join('\n');
      loaded = true;
    } catch (e) {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  $('save').disabled = $('test').disabled = !loaded;
  if (!loaded) $('status').textContent = 'Could not read the current settings. Close this page and open it again.';
}

async function save() {
  if (!loaded) return false;
  const serverUrl = $('serverUrl').value.trim().replace(/\/+$/, '');
  const token = $('token').value.trim();
  if (!/^https?:\/\/\S+$/.test(serverUrl) || !token) {
    $('status').textContent = 'Server URL (http:// or https://) and token are both required; nothing was saved.';
    return false;
  }
  const blocklist = $('blocklist').value.split('\n').map((l) => l.trim()).filter(Boolean);
  await api.storage.local.set({
    serverUrl,
    token,
    deviceName: $('deviceName').value.trim().slice(0, 64),
    dwellSeconds: Math.max(2, Number($('dwellSeconds').value) || 8),
    blocklist,
  });
  $('status').textContent = 'Saved.';
  setTimeout(() => ($('status').textContent = ''), 2000);
  return true;
}

async function test() {
  if (!(await save())) return;
  $('status').textContent = 'Testing…';
  const r = await api.runtime.sendMessage({ type: 'testConnection' });
  if (r.ok) {
    $('status').textContent = `Connected: ${r.stats.pages} pages, ${r.stats.visits} visits.`;
  } else {
    $('status').textContent = 'Failed: ' + r.error;
  }
}

$('save').addEventListener('click', save);
$('test').addEventListener('click', test);
load();
