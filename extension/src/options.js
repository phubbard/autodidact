const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);

async function load() {
  const s = await api.runtime.sendMessage({ type: 'settings' });
  $('serverUrl').value = s.serverUrl || '';
  $('token').value = s.token || '';
  $('dwellSeconds').value = s.dwellSeconds ?? 8;
  $('blocklist').value = (s.blocklist || []).join('\n');
}

async function save() {
  const blocklist = $('blocklist').value.split('\n').map((l) => l.trim()).filter(Boolean);
  await api.storage.local.set({
    serverUrl: $('serverUrl').value.trim().replace(/\/+$/, ''),
    token: $('token').value.trim(),
    dwellSeconds: Math.max(2, Number($('dwellSeconds').value) || 8),
    blocklist,
  });
  $('status').textContent = 'Saved.';
  setTimeout(() => ($('status').textContent = ''), 2000);
}

async function test() {
  await save();
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
