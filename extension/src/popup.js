const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);

let host = '';

async function init() {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  try { host = new URL(tab.url).hostname; } catch { host = ''; }
  $('domain').textContent = host || '(not a web page)';
  $('block').disabled = !host;

  const s = await api.runtime.sendMessage({ type: 'settings' });
  const paused = s.pausedUntil > Date.now();
  $('pause').textContent = paused ? 'Resume recording' : 'Pause for 1 hour';
  if (host && s.blocklist.includes(host)) {
    $('block').textContent = 'Domain is blocked';
    $('block').disabled = true;
  }
  const q = await api.runtime.sendMessage({ type: 'queueLength' });
  $('queue').textContent = q.length ? `${q.length} page(s) waiting to upload` : 'Queue empty';
}

$('pause').addEventListener('click', async () => {
  const s = await api.runtime.sendMessage({ type: 'settings' });
  const paused = s.pausedUntil > Date.now();
  await api.storage.local.set({ pausedUntil: paused ? 0 : Date.now() + 3600_000 });
  window.close();
});

$('block').addEventListener('click', async () => {
  const s = await api.runtime.sendMessage({ type: 'settings' });
  if (!s.blocklist.includes(host)) s.blocklist.push(host);
  await api.storage.local.set({ blocklist: s.blocklist });
  window.close();
});

$('search').addEventListener('click', async () => {
  const s = await api.runtime.sendMessage({ type: 'settings' });
  api.tabs.create({ url: s.serverUrl + '/' });
  window.close();
});

$('options').addEventListener('click', () => {
  api.runtime.openOptionsPage();
  window.close();
});

init();
