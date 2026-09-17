// Autodidact content script.
//
// Runs in the top frame of every http(s) page. Counts seconds the tab is both
// visible and focused; once the dwell threshold is reached it extracts the
// page's main text and hands it to the background worker. Re-arms itself on
// SPA navigations (pushState / replaceState / popstate) and re-captures the
// same URL only if the extracted text changed.
//
// It deliberately knows nothing about the server, the blocklist, or pausing;
// the background worker enforces all of that.

(() => {
  if (window.top !== window) return;
  if (window.__autodidactLoaded) return;
  window.__autodidactLoaded = true;

  const api = globalThis.browser ?? globalThis.chrome;

  const DEFAULT_DWELL_S = 8;
  const MIN_TEXT_CHARS = 200;
  const MAX_TEXT_CHARS = 200_000;
  const TICK_MS = 1000;
  const DWELL_HEARTBEAT_S = 15;

  const STRIP_TAGS = new Set([
    'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME', 'SVG', 'CANVAS',
    'NAV', 'HEADER', 'FOOTER', 'ASIDE', 'FORM', 'BUTTON', 'SELECT',
    'TEXTAREA', 'INPUT', 'VIDEO', 'AUDIO', 'OBJECT', 'EMBED',
  ]);
  const BLOCK_TAGS = new Set([
    'P', 'DIV', 'SECTION', 'ARTICLE', 'MAIN', 'LI', 'TR', 'TD', 'TH', 'DT', 'DD',
    'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE', 'BR', 'HR',
    'UL', 'OL', 'TABLE', 'FIGCAPTION', 'DETAILS', 'SUMMARY',
  ]);
  const CONTENT_ROOTS = [
    'article', 'main', '[role="main"]', '#content', '#main', '.post', '.entry-content',
    '.article-body', '.post-content', '.markdown-body', '.content',
  ];

  let dwellS = 0;
  let captured = false;
  let lastHash = null;
  let visitId = newId();
  let dwellThresholdS = DEFAULT_DWELL_S;
  let timer = null;

  api.storage.local.get({ dwellSeconds: DEFAULT_DWELL_S }).then((v) => {
    dwellThresholdS = Number(v.dwellSeconds) || DEFAULT_DWELL_S;
  }).catch(() => {});

  function newId() {
    return (crypto.randomUUID ? crypto.randomUUID()
      : Date.now().toString(36) + Math.random().toString(36).slice(2));
  }

  // FNV-1a over the string, two lanes, hex. Only used to detect "changed since
  // last capture" on the client; the server computes the real sha256.
  function cheapHash(s) {
    let h1 = 0x811c9dc5, h2 = 0x01000193;
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
      h2 = Math.imul(h2 ^ c, 0x811c9dc5) >>> 0;
    }
    return h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0');
  }

  function isHidden(el) {
    if (el.hidden || el.getAttribute('aria-hidden') === 'true') return true;
    const st = el.style;
    return st && (st.display === 'none' || st.visibility === 'hidden');
  }

  function pickRoot() {
    let best = document.body;
    let bestLen = 0;
    for (const sel of CONTENT_ROOTS) {
      let els;
      try { els = document.querySelectorAll(sel); } catch { continue; }
      for (const el of els) {
        const len = (el.innerText || '').length;
        if (len > bestLen) { best = el; bestLen = len; }
      }
    }
    // A too-small "content" root is probably a sidebar widget; use body.
    const bodyLen = (document.body?.innerText || '').length;
    if (bestLen < bodyLen * 0.2) best = document.body;
    return best || document.documentElement;
  }

  function extractText(root) {
    const out = [];
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        out.push(node.nodeValue);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (STRIP_TAGS.has(node.tagName) || isHidden(node)) return;
      const block = BLOCK_TAGS.has(node.tagName);
      if (block) out.push('\n');
      for (const child of node.childNodes) walk(child);
      if (block) out.push('\n');
    };
    walk(root);
    return out.join('')
      .replace(/[ \t ]+/g, ' ')
      .replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim()
      .slice(0, MAX_TEXT_CHARS);
  }

  function meta(name) {
    const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
    return el ? (el.getAttribute('content') || '').trim() : '';
  }

  function canonicalUrl() {
    const link = document.querySelector('link[rel="canonical"]');
    const href = link?.getAttribute('href');
    if (href) {
      try { return new URL(href, location.href).href; } catch { /* fall through */ }
    }
    return location.href;
  }

  function buildPayload(text) {
    return {
      visit_id: visitId,
      url: location.href,
      canonical_url: canonicalUrl(),
      title: (document.title || '').trim(),
      description: meta('description') || meta('og:description'),
      text,
      lang: document.documentElement.lang || '',
      dwell_s: dwellS,
      captured_at: Math.floor(Date.now() / 1000),
      browser: navigator.userAgent,
    };
  }

  function send(type, payload) {
    try {
      const p = api.runtime.sendMessage({ type, payload });
      if (p && p.catch) p.catch(() => {});
    } catch { /* extension reloaded; ignore */ }
  }

  function capture() {
    const text = extractText(pickRoot());
    if (text.length < MIN_TEXT_CHARS) return;
    const h = cheapHash(text);
    if (h === lastHash) return;
    lastHash = h;
    captured = true;
    send('capture', buildPayload(text));
  }

  function tick() {
    // SPA navigation: wrapping history.pushState from a content script does not
    // intercept the page's own calls (isolated world), so poll the URL instead.
    if (location.href !== lastHref) onNav();
    if (document.visibilityState !== 'visible' || !document.hasFocus()) return;
    dwellS += 1;
    if (!captured && dwellS >= dwellThresholdS) capture();
    // Long reads: re-check every 60 s in case the content changed (infinite scroll, SPA in-place updates).
    else if (captured && dwellS % 60 === 0) capture();
    // Dwell heartbeat. Messages sent from pagehide/unload are often dropped, so
    // the server's view of dwell is kept current while the page is open instead.
    if (captured && dwellS % DWELL_HEARTBEAT_S === 0) flushDwell();
  }

  function reset() {
    dwellS = 0;
    captured = false;
    lastHash = null;
    visitId = newId();
  }

  function flushDwell() {
    if (!captured) return;
    send('dwell', { visit_id: visitId, url: location.href, dwell_s: dwellS });
  }

  // SPA navigation: popstate/hashchange fire in the isolated world; pushState
  // is caught by the URL poll in tick().
  let lastHref = location.href;
  const onNav = () => {
    if (location.href === lastHref) return;
    flushDwell();
    lastHref = location.href;
    reset();
  };
  window.addEventListener('popstate', () => setTimeout(onNav, 0));
  window.addEventListener('hashchange', () => setTimeout(onNav, 0));

  // Best effort only; see the heartbeat in tick() for the reliable path.
  window.addEventListener('pagehide', flushDwell);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushDwell();
  });

  timer = setInterval(tick, TICK_MS);
})();
