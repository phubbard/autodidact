# Autodidact

Records the text of web pages you dwell on, in every browser, so you can find
them weeks or months later from a vague recollection. Everything runs on your
own network: a browser extension captures, a small Flask server stores and
indexes in SQLite, and a search page finds.

Design notes and the milestone plan are in `docs/ARCHITECTURE.md`; current state and next steps in `docs/STATUS.md`; conventions for working on the code in `CLAUDE.md`.
This is milestone 1: capture plus keyword search with date and site filters.

```
autodidact/
├── extension/          MV3 WebExtension (one codebase: Chromium, Firefox, Safari)
│   ├── src/            manifest, content.js, background.js, options, popup
│   ├── build.sh        → dist/chromium, dist/firefox, a zip of each, and a Firefox .xpi
│   └── test/e2e.mjs    headless-Chromium end-to-end test
├── server/
│   ├── app.py          Flask: /ingest, /dwell, /search, /page, /stats, UI at /
│   ├── db.py           schema (pages, visits, embeddings, FTS5), URL normalisation
│   ├── freshrss_sync.py  second source: read/starred FreshRSS items via its Google Reader API
│   ├── embed.py        milestone-2 enrichment job (summaries, tags, embeddings via LM Studio)
│   └── tests/          pytest
├── safari/Autodidact/  Xcode project wrapping extension/src for Safari on macOS and iOS
├── docs/               ARCHITECTURE.md (design), STATUS.md (current state and session log)
└── deploy/install.sh   idempotent Pi installer/updater: venv, env, systemd, Caddy, cron
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

## Deploy to the Pi

One idempotent script installs and later updates everything:

```sh
curl -fsSL https://raw.githubusercontent.com/phubbard/autodidact/main/deploy/install.sh | sudo bash
```

It clones the repo to `/srv/autodidact/src`, builds a venv, generates a bearer
token into `/srv/autodidact/env` (chmod 600), installs a hardened systemd unit
running gunicorn on `127.0.0.1:8765`, drops a Caddy site into
`/etc/caddy/conf.d/autodidact.caddy` that proxies `autodidact.<your domain>` for
LAN and Tailscale clients only (the LAN CIDR is read off the Pi's primary
interface), adds the `import` line to the Caddyfile if it is missing, writes
`/etc/cron.d/autodidact`, and smoke-tests `/stats`. Re-running it pulls `main`,
reinstalls requirements, restarts the service, and refreshes cron. Pass
FreshRSS credentials on the first run or add them to `env` later and re-run:

```sh
FRESHRSS_URL=https://rss.example.net FRESHRSS_USER=paul FRESHRSS_API_PASSWORD=… \
AUTODIDACT_LLM_URL=http://axiom.phfactor.net:1234/v1 \
  sudo -E deploy/install.sh
```

The cron jobs are only installed for sources that are configured. Knobs:
`PREFIX`, `RUN_USER`, `SITE_HOST`, `PORT`, `LAN_CIDR`, `BRANCH`, `DRY_RUN=1`.
Two things the script cannot do for you: create the DNS record for
`autodidact.<domain>` pointing at the Pi, and enable API access in FreshRSS.

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

Zen, LibreWolf and other Firefox forks built without mandatory signing can
install the unsigned build permanently: set `xpinstall.signatures.required` to
`false` in `about:config`, then `about:addons` → gear → "Install Add-on From
File" → `extension/dist/autodidact-firefox.xpi`. That pref turns off signature
checks for every add-on in the profile. To update, install the newer `.xpi`
the same way and check that the version on the add-on's page changed.

Then click the toolbar icon → Settings, enter the server URL and token, hit
"Test connection". The popup also has a one-hour pause and a "never record
this domain" button.

To update an unpacked Chromium install after pulling new code, run `build.sh`
again and press reload on the extension's card. `build.sh` recreates `dist/`,
so the folder the browser loads from is replaced in place.

### Safari (macOS and iOS)

Safari needs the extension wrapped in an app. `safari/Autodidact/Autodidact.xcodeproj`
does that for both platforms and references `extension/src` directly, so there
is nothing to copy. You need Xcode and an Apple developer team; set yours on
all four targets (Signing & Capabilities), since the project carries the
author's team id.

macOS: choose the `Autodidact (macOS)` scheme with "My Mac" and Run, or

```sh
cd safari/Autodidact
xcodebuild -project Autodidact.xcodeproj -scheme "Autodidact (macOS)" -configuration Debug build
```

then Safari → Settings → Extensions → enable Autodidact, open its row and
choose "Always Allow on Every Website".

iOS: choose the `Autodidact (iOS)` scheme with your iPhone and Run. On the
phone, open the app once, then Settings → Apps → Safari → Extensions →
Autodidact → Allow Extension, and set All Websites to Allow. This covers
Safari only; other iOS browsers cannot run extensions. The phone reaches the
server on the home network or over Tailscale; anywhere else captures wait in
the extension's queue (up to 500) and are sent when the server is reachable.

Each scheme only runs on its own platform; Xcode's "platform doesn't match"
error means the scheme and the destination disagree. After changing signing
settings, use Product → Clean Build Folder before building again.

## How capture works

The content script counts seconds a tab is visible and focused. At the dwell
threshold (default 8 s) it extracts the main content block, strips nav,
header, footer, aside, forms and hidden elements, and sends title, URL,
canonical URL, description, text (≤ 200 KB) and dwell time to the background
worker. The worker drops it if recording is paused, the window is private, the
host matches the blocklist, or the page is on the Autodidact server itself;
otherwise it queues it in `storage.local` and POSTs with retry every minute,
so the server being down loses nothing. The manifest requests
`unlimitedStorage` for that queue: Safari otherwise rejects queue writes as
over quota, which leaves a sent page stuck at the head and re-sent forever.

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

## Second source: FreshRSS

Reading in an RSS client on the phone never touches a browser extension, but
the client syncs read and starred state back to FreshRSS, and FreshRSS exposes
that through its Google Reader compatible API. `server/freshrss_sync.py` pulls
items that are read and/or starred, converts the feed HTML to text, fetches
the full article when the feed only carried an excerpt (uses `trafilatura` if
installed, otherwise the page's `<article>`/`<main>` block), and POSTs to
`/ingest` with `source: "rss"`. The server dedups against browser captures of
the same URL, so an article read on the phone and later opened on the laptop
is one page with two visits.

Setup: in FreshRSS, Administration → Authentication → allow API access, then
set an API password on your profile. Then:

```sh
export FRESHRSS_URL=https://rss.example.net FRESHRSS_USER=paul FRESHRSS_API_PASSWORD=…
export AUTODIDACT_URL=http://127.0.0.1:8765 AUTODIDACT_TOKEN=…
python freshrss_sync.py --dry-run --days 3        # see what would be sent
python freshrss_sync.py --state ./freshrss_state.json
```

`deploy/install.sh` installs an hourly cron job for it once `FRESHRSS_URL` is in `/srv/autodidact/env`. Search results show `rss · <feed>` on
these hits, the UI has a browser/rss filter, and starred items are findable
by searching `starred` or the feed's name.

Caveat: FreshRSS cannot distinguish an article you opened from one swept
away by "mark all as read". `--mode starred` limits ingest to what you
deliberately kept; the default `--mode both` trusts read state. Try both and
see which corpus searches better.

## Milestone 2 (optional now)

`server/embed.py` fills `summary` and `tags` (which flow into the FTS index
through a trigger, so keyword search improves immediately) and stores
embeddings for the later semantic layer. It talks to any OpenAI-compatible
endpoint; `deploy/install.sh` installs a cron job running it every 15 minutes
against LM Studio on Axiom once `AUTODIDACT_LLM_URL` is in
`/srv/autodidact/env`, and it simply does nothing when Axiom is asleep.

## Tests

`server/tests` covers URL normalisation, FTS query escaping, dedup, dwell,
filters, deletion, schema migration, and a FreshRSS sync run against a fake
Google Reader endpoint and a live server (23 tests). `extension/test/e2e.mjs` loads the built
extension into headless Chromium, configures it through its own options page,
dwells on a local article, triggers an SPA navigation, and checks both pages
are searchable with the nav and footer text absent. Needs `npm i playwright`
and the server running with `AUTODIDACT_TOKEN=e2e-token`.
