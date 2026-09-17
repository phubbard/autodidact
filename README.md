# Autodidact

Records the text of web pages you dwell on, in every browser, so you can find
them weeks or months later from a vague recollection. Everything runs on your
own network: a browser extension captures, a small Flask server stores and
indexes in SQLite, and a search page finds.

Design notes and the milestone plan live in the "Autodidact Architecture" doc.
This is milestone 1: capture plus keyword search with date and site filters.

```
autodidact/
├── extension/          MV3 WebExtension (one codebase, Chromium + Firefox)
│   ├── src/            manifest, content.js, background.js, options, popup
│   ├── build.sh        → dist/chromium, dist/firefox, and a zip of each
│   └── test/e2e.mjs    headless-Chromium end-to-end test
├── server/
│   ├── app.py          Flask: /ingest, /dwell, /search, /page, /stats, UI at /
│   ├── db.py           schema (pages, visits, embeddings, FTS5), URL normalisation
│   ├── embed.py        milestone-2 enrichment job (summaries, tags, embeddings via LM Studio)
│   └── tests/          pytest
└── deploy/             systemd unit, Caddy snippet, cron line for embed.py
```

## Run the server

```sh
cd server
python3 -m venv venv && venv/bin/pip install -r requirements.txt
AUTODIDACT_TOKEN=$(openssl rand -hex 24) ; echo $AUTODIDACT_TOKEN   # keep this
AUTODIDACT_DB=./autodidact.db AUTODIDACT_TOKEN=$AUTODIDACT_TOKEN venv/bin/python app.py
# → http://127.0.0.1:8765
venv/bin/pytest -q tests
```

For the Pi: `deploy/autodidact.service` runs it under gunicorn bound to
localhost; `deploy/Caddyfile.snippet` fronts it as `autodidact.phfactor.net`
and refuses anything not from the LAN. Put `AUTODIDACT_TOKEN=…` in
`/srv/autodidact/env`.

Endpoints: `POST /ingest` and `POST /dwell` (bearer token) take what the
extension sends; `GET /search?q=&after=&before=&domain=&limit=&offset=` returns
JSON hits with highlighted snippets, or the most recent pages when `q` is
empty; `GET /page/<id>` returns the stored text (JSON, or HTML with
`?format=html`); `DELETE /page/<id>` and `DELETE /domain/<d>` (bearer token)
remove mistakes; `GET /stats` for counts.

## Install the extension

```sh
cd extension && ./build.sh
```

Chromium family (Chrome, Brave, Edge, Arc, Vivaldi): open `chrome://extensions`,
enable Developer mode, "Load unpacked", pick `extension/dist/chromium`.

Firefox: `about:debugging#/runtime/this-firefox` → "Load Temporary Add-on" →
pick `extension/dist/firefox/manifest.json`. Temporary add-ons vanish on
restart; for a permanent install, submit `dist/autodidact-firefox.zip` to AMO
as unlisted (free, usually automatic) and install the signed `.xpi`. Firefox
MV3 treats host permissions as optional: after installing, open the add-on's
Permissions tab and allow "Access your data for all websites", or nothing is
captured.

Then click the toolbar icon → Settings, enter the server URL and token, hit
"Test connection". The popup also has a one-hour pause and a "never record
this domain" button.

Safari is milestone 4: `xcrun safari-web-extension-converter extension/dist/chromium`
produces an Xcode project that wraps the same code.

## How capture works

The content script counts seconds a tab is visible and focused. At the dwell
threshold (default 8 s) it extracts the main content block, strips nav,
header, footer, aside, forms and hidden elements, and sends title, URL,
canonical URL, description, text (≤ 200 KB) and dwell time to the background
worker. The worker drops it if recording is paused, the window is private, or
the host matches the blocklist; otherwise it queues it in `storage.local` and
POSTs with retry every minute, so the server being down loses nothing.

SPA navigations are detected by polling the URL once a second (wrapping
`history.pushState` from a content script does not intercept the page's own
calls). Dwell is reported by a 15-second heartbeat while the page stays open,
because messages sent from `pagehide` are unreliable.

The server normalises URLs (drops fragments, `utm_*` and other tracking
parameters, `www.`, default ports, trailing slashes) and dedups on URL hash
plus content hash: the same page seen again adds a visit; the same URL with
changed text becomes a new row so the old version stays searchable.

## Search

FTS5 with the porter tokenizer over title, description, text, summary, tags
and domain; title, summary and tags weighted 3×. User input is quoted term by
term so FTS5 operators cannot break a query, the last word gets a prefix
wildcard, and if AND-ing every word finds nothing the search falls back to any
word (the UI says so). `after`/`before` filter on the page's seen window;
`domain` matches the site and its subdomains.

## Milestone 2 (optional now)

`server/embed.py` fills `summary` and `tags` (which flow into the FTS index
through a trigger, so keyword search improves immediately) and stores
embeddings for the later semantic layer. It talks to any OpenAI-compatible
endpoint; `deploy/embed.cron` runs it against LM Studio on Axiom every 15
minutes and simply does nothing when Axiom is asleep.

## Tests

`server/tests` covers URL normalisation, FTS query escaping, dedup, dwell,
filters and deletion (19 tests). `extension/test/e2e.mjs` loads the built
extension into headless Chromium, configures it through its own options page,
dwells on a local article, triggers an SPA navigation, and checks both pages
are searchable with the nav and footer text absent. Needs `npm i playwright`
and the server running with `AUTODIDACT_TOKEN=e2e-token`.
