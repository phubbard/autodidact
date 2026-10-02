# Autodidact — guide for Claude Code

Autodidact records the text of web pages Paul dwells on (browser extension) and articles he reads or stars in FreshRSS (hourly sync), stores extracted text in SQLite on his home network, and finds pages months later from vague recollections. Read `docs/ARCHITECTURE.md` for the design and reasoning and `docs/STATUS.md` for where things stand and what to do next. Keep `docs/STATUS.md` current at the end of every work session.

Owner: Paul Hubbard (pfh@phfactor.net). GitHub: `phubbard/autodidact`. This checkout lives at `~/code/autodidact` on Paul's M2 MacBook Air. Everything runs on Paul's LAN: the Flask server on the Pi 5 (`webserver`, served as `https://autodidact.phfactor.net` behind Caddy, LAN and Tailscale only), LLM enrichment via LM Studio on Axiom (Mac Studio, `axiom.phfactor.net:1234`, sleeps). No cloud services, ever.

## Current state (2026-10-01)

- **Server is deployed and running** on the Pi at `/srv/autodidact/{src,venv,data,env}`, installed by `deploy/install.sh` (user `pfh`, gunicorn on `127.0.0.1:8765`, systemd unit `autodidact`, Caddy site `/etc/caddy/conf.d/autodidact.caddy`, cron in `/etc/cron.d/autodidact`). `https://autodidact.phfactor.net/stats` answers from the LAN. Corpus is empty until the extensions are loaded.
- **Extension builds** are in `extension/dist/{chromium,firefox}` (gitignored; regenerate with `extension/build.sh`, which wipes and recreates `dist/`, the folder unpacked installs load from). Loaded and confirmed posting on the MacBook Air: Brave (unpacked), Zen (Firefox build; whether as a temporary add-on or the unsigned `.xpi` was not recorded — a temporary one disappears when Zen restarts).
- **Safari, macOS and iOS**: one combined Xcode project in `safari/Autodidact/` (see "Safari" below). Enabled in Safari on the MacBook Air and installed on Paul's iPhone 16 Pro. **Open bug (2026-10-01):** Mac Safari re-posts the same `/ingest` item every minute (server answers 200, dedups it, the queue never drains), and the iPhone passes Test connection but has not delivered a page. Both point at the background worker's flush path under Safari; undiagnosed. See STATUS.md.
- **FreshRSS sync is enabled** (2026-10-01): `FRESHRSS_URL=http://127.0.0.1:8090` (the `freshrss` container's published port on the Pi, bypassing DNS and Caddy; `https://news.phfactor.net` also works), user `pfh`, hourly at :17, default `--mode both --days 7`. First run imported about a week of read items (~1,100). Paul had starred nothing in that week, so `--mode starred` would currently be an empty corpus.
- **Enrichment** (`embed.py`) has never run against a real model.

## Layout

```
extension/src/        MV3 WebExtension, one codebase for Chromium + Firefox (+ Safari via Xcode wrapper)
  manifest.json       Chromium manifest; build.sh derives the Firefox one with jq
  content.js          dwell timer, Readability-style extraction, SPA detection, dwell heartbeat
  background.js       blocklist / pause / incognito guard, storage.local queue, retry alarm, badge
  options.{html,js}   server URL, token, dwell seconds, blocklist, "Test connection"
  popup.{html,js}     pause 1 h, block this domain, open search
extension/build.sh    → dist/chromium, dist/firefox, dist/autodidact-{chromium,firefox}.zip
extension/test/e2e.mjs  Playwright headless-Chromium end-to-end test (needs a running server)
server/app.py         Flask app factory create_app(db_path, token); all HTTP routes + search UI
server/db.py          schema (pages, visits, embeddings, pages_fts), MIGRATIONS, normalize_url
server/freshrss_sync.py  Google Reader API client → POST /ingest with source=rss
server/embed.py       M2 enrichment job: summary + tags + embeddings via OpenAI-compatible endpoint
server/templates/     index.html (search UI), page.html (stored text view)
server/tests/         pytest: test_app.py (Flask client), test_freshrss.py (fake GReader + live server)
deploy/install.sh     idempotent Pi installer/updater: clone/pull, venv, env+token, systemd, Caddy conf.d, cron
safari/Autodidact/    Xcode project from safari-web-extension-converter: iOS + macOS app and extension targets
.github/workflows/ci.yml  pytest + node --check + build.sh; uploads extension zips
```

## Commands

```sh
# Server (from repo root; CI does exactly this)
python3 -m venv server/venv && server/venv/bin/pip install -r server/requirements.txt
server/venv/bin/python -m pytest -q server/tests            # 23 tests, all should pass
AUTODIDACT_DB=./autodidact.db AUTODIDACT_TOKEN=dev server/venv/bin/python server/app.py
#   → http://127.0.0.1:8765  (UI at /, JSON at /search?q=…)

# Extension
extension/build.sh                                          # needs jq and zip
for f in extension/src/*.js; do node --check "$f"; done      # syntax check, no bundler

# End-to-end (server must be running with AUTODIDACT_TOKEN=e2e-token; needs `npm i playwright` in extension/)
cd extension && node test/e2e.mjs

# FreshRSS sync, dry run
FRESHRSS_URL=… FRESHRSS_USER=… FRESHRSS_API_PASSWORD=… AUTODIDACT_TOKEN=… \
  python server/freshrss_sync.py --dry-run --days 3

# Safari (macOS host with Xcode; see "Safari" below)
(cd safari/Autodidact && xcodebuild -project Autodidact.xcodeproj -scheme "Autodidact (macOS)" -configuration Debug build)
```

There is no build step for the extension beyond copying `src/` and patching the manifest. No bundler, no TypeScript, no npm project — Playwright is the only JS dependency and only for the e2e test.

## Operating the deployment

Paul can ssh to the Pi; Claude Code sessions on the Mac can too if his keys are available (`ssh webserver`). The sandboxed shells used by Cowork cannot.

```sh
# On the Pi
sudo /srv/autodidact/src/deploy/install.sh     # update: pulls main, pip install, restarts, refreshes cron
systemctl status autodidact                     # service
journalctl -u autodidact -f                     # gunicorn access + error log
journalctl -u caddy -n 30 --no-pager            # certificate / proxy problems
curl -s http://127.0.0.1:8765/stats             # bypasses Caddy
curl -s https://autodidact.phfactor.net/stats   # through Caddy (LAN only)
sudo cat /srv/autodidact/env                    # token and FreshRSS/LLM settings (chmod 600, owner pfh)
sqlite3 /srv/autodidact/data/autodidact.db 'select count(*), source from pages group by source'   # sqlite3 CLI is not installed on the Pi; use /stats, or apt install sqlite3
tail /srv/autodidact/data/freshrss.log /srv/autodidact/data/embed.log
```

Facts about the deployment that are easy to get wrong:

- Paul's LAN is a **public /24, `204.128.136.0/24`**, not RFC 1918. The Caddy `remote_ip` matcher is detected from the Pi's interface by `install.sh` and also allows `100.64.0.0/10` (Tailscale) and loopback. A hand-written `192.168.0.0/16` matcher locks Paul out of his own network — that mistake was made once already.
- The Pi is `204.128.136.3`. DNS: Pi-hole at `.5` (what the Pi's `/etc/resolv.conf` points at) forwards `phfactor.net` to the UCG-Max at `.11`, where local records live. `autodidact.phfactor.net` is a local CNAME to `webserver.phfactor.net` on the UCG and a public CNAME on Cloudflare (→ `76.167.221.59`, the WAN side, same as the other vhosts). The Pi-hole cached an NXDOMAIN when the installer ran before the record existed; `pihole restartdns` fixed it. If a name "doesn't resolve" on the Pi but `dig @204.128.136.5` answers, suspect that cache first.
- `install.sh` appended `import /etc/caddy/conf.d/*.caddy` to `/etc/caddy/Caddyfile` (backup at `Caddyfile.bak`). Caddy has ~13 other vhosts in that file; never rewrite it wholesale.
- The systemd unit runs with `ProtectSystem=strict` and `ReadWritePaths=/srv/autodidact/data`; anything the server must write goes under `data/`.
- Rotating the token: edit `AUTODIDACT_TOKEN` in `env`, `sudo systemctl restart autodidact`, update every extension's Settings. The token was pasted into a chat transcript on 2026-10-01; Paul may want to rotate it.

## Environment variables

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `AUTODIDACT_DB` | app.py, embed.py | `autodidact.db` | SQLite path; WAL mode. `/srv/autodidact/data/autodidact.db` on the Pi |
| `AUTODIDACT_TOKEN` | app.py, freshrss_sync.py | `""` | Bearer token for `/ingest`, `/dwell`, `DELETE`. Empty token = ingest refused (503) |
| `AUTODIDACT_HOST` / `PORT` / `DEBUG` | app.py `__main__` | `127.0.0.1` / `8765` / off | dev server only; gunicorn's bind is in the unit `install.sh` generates |
| `AUTODIDACT_URL` | freshrss_sync.py | `http://127.0.0.1:8765` | where to POST |
| `FRESHRSS_URL`, `FRESHRSS_USER`, `FRESHRSS_API_PASSWORD` | freshrss_sync.py | — | GReader API; API access must be enabled in FreshRSS admin, API password set on the profile. On the Pi: `http://127.0.0.1:8090`, `pfh` |
| `FRESHRSS_STATE` | freshrss_sync.py | `freshrss_state.json` | JSON of seen item ids; cron uses `data/freshrss_state.json` |
| `AUTODIDACT_LLM_URL` | embed.py | `http://localhost:1234/v1` | LM Studio on Axiom in prod; presence in `env` enables the embed cron |
| `AUTODIDACT_CHAT_MODEL` | embed.py | `""` (first loaded non-embedding model) | for summary + tags |
| `AUTODIDACT_EMBED_MODEL` | embed.py | `text-embedding-nomic-embed-text-v1.5` | unconfirmed against real LM Studio |

`install.sh` generates `env` with the token, `AUTODIDACT_DB` and `AUTODIDACT_URL`, and folds any `FRESHRSS_*` / `AUTODIDACT_LLM_URL` it was given into it; cron jobs are written only for sources that are configured, so after editing `env` re-run the installer. `.gitignore` excludes `*.db*`, `venv/`, `dist/`, `node_modules/`.

## Safari (macOS and iOS)

One Xcode project, `safari/Autodidact/Autodidact.xcodeproj`, wraps the same WebExtension for both platforms. It was generated by Apple's converter without `--macos-only`:

```sh
xcrun safari-web-extension-converter extension/src --project-location safari \
  --app-name Autodidact --bundle-identifier net.phfactor.autodidact --no-open --no-prompt
```

Do not re-run the converter (it refuses to overwrite, and the project has been edited since). What exists: four targets, `Autodidact (iOS)`, `Autodidact (macOS)` and an `Autodidact Extension` for each; two schemes, `Autodidact (iOS)` and `Autodidact (macOS)`. Bundle ids are `net.phfactor.autodidact` and `net.phfactor.autodidact.Extension` on both platforms, team NSR65JVW9F on all four targets (the converter sets no team; an extension without one is ad hoc signed and the build fails with "Embedded binary is not signed with the same certificate as the parent app"). The extension's resources are file references to `../../../extension/src/*`, so edits in `extension/src` flow into the next Xcode build; never copy them into the project. The converter warns that `open_in_tab` is unsupported in Safari; that is harmless.

```sh
cd safari/Autodidact
xcodebuild -project Autodidact.xcodeproj -scheme "Autodidact (macOS)" -configuration Debug build
xcodebuild -project Autodidact.xcodeproj -scheme "Autodidact (iOS)" -destination 'generic/platform=iOS Simulator' build
xcodebuild -project Autodidact.xcodeproj -scheme "Autodidact (iOS)" -destination 'id=<device udid>' -derivedDataPath <dir> build
xcrun devicectl list devices
xcrun devicectl device install app --device <udid> <dir>/Build/Products/Debug-iphoneos/Autodidact.app
```

Things that went wrong, so they are not rediscovered:

- **"Platform doesn't match … supported platforms" in Xcode** means the scheme and the destination disagree: `Autodidact (iOS)` runs on the iPhone, `Autodidact (macOS)` on My Mac.
- **Certificate error persists after a signing change**: the embedded copy of the extension in DerivedData is stale. Product → Clean Build Folder (⇧⌘K), then build again.
- **Bundle id prefix check is case-sensitive.** The first, Mac-only conversion produced `net.phfactor.Autodidact` for the app and failed with "Embedded binary's bundle identifier is not prefixed with the parent app's bundle identifier".
- **Safari loads whichever build was registered last.** Any `xcodebuild` of the macOS scheme, even into a throwaway `-derivedDataPath`, registers that copy of the extension with Safari (`pluginkit -mAvvv -p com.apple.Safari.web-extension | grep -A3 phfactor` shows the path). Delete throwaway Mac builds afterwards and rebuild into the default DerivedData, or Safari ends up running the extension from a temp directory.
- **Device builds** sign with the wildcard team provisioning profile; no App IDs had to be registered.

Enabling it. macOS: run the app once, Safari → Settings → Extensions → enable Autodidact, open its row and choose **"Always Allow on Every Website"** (Safari treats `<all_urls>` as per-site, like Firefox; without this nothing is captured). iOS: open the app once, then Settings → Apps → Safari → Extensions → Autodidact → Allow Extension, and All Websites → Allow. Then on either: Autodidact popup → Settings → URL + token → Test connection. The phone reaches the server only on home Wi-Fi or Tailscale; elsewhere captures queue (max 500) and flush later. If the extension is missing from Safari on the Mac: Settings → Advanced → "Show features for web developers", Develop → Allow Unsigned Extensions, relaunch.

Still to do: a Safari section in README.md; installing the Mac app somewhere more durable than DerivedData.

## Architecture in one paragraph

Two sources feed one server. The extension is deliberately dumb: it knows the blocklist and the dwell threshold and nothing else, so policy changes happen on the Pi instead of in five browsers. Captures are extracted text only (no HTML, no screenshots), capped at 200 KB, sent as JSON with a bearer token, queued in `storage.local` and retried every minute so a down server loses nothing. The server normalizes the URL, dedups on `(url_hash, content_hash)` — same page again is a new `visits` row; same URL with changed text is a new `pages` row so old content stays searchable — and updates FTS5 synchronously via triggers. Enrichment (summary, tags, embeddings) is a separate cron job so ingest never waits on Axiom being awake. Search is layered: FTS5 BM25 now (M1), semantic merge next (M2), LLM query rewrite + rerank later (M3). Time filtering is first-class because a two-month window prunes more than any ranking trick.

## Conventions and rules

- **Python**: stdlib + Flask only on the server hot path. `trafilatura` is optional (import guarded) and only used by `freshrss_sync.py`. Don't add numpy to `app.py` until M2 semantic search actually needs it. Type hints where they help; no type checker is configured.
- **JavaScript**: plain ES2020+, no build, no modules in content/background scripts (they run as classic scripts). `const api = globalThis.browser ?? globalThis.chrome;` is the whole cross-browser shim — keep it that way. Anything that differs between Chromium and Firefox goes in `build.sh`'s jq patch, not in code paths. Safari gets the Chromium manifest unchanged.
- **Schema changes**: add the column to `SCHEMA` *and* append a tuple to `MIGRATIONS` in `db.py`. `connect()` applies migrations before running the idempotent schema. Add a migration test like `test_migration_adds_columns`. Never rewrite existing rows' hashes. The production DB now exists; migrations must be safe on a live file.
- **FTS**: user input goes through `fts_query()` which quotes every term so FTS5 operators can't break or inject. Keep that; do not concatenate raw user text into a MATCH clause.
- **Dedup and normalization** live only in `db.normalize_url` and the `/ingest` handler. If you change what counts as "the same URL", existing rows won't retroactively merge — say so in STATUS.md.
- **Auth**: `/ingest`, `/dwell`, and both `DELETE` routes require `Authorization: Bearer <token>`. Read routes (`/`, `/search`, `/page`, `/stats`) are unauthenticated by design because Caddy restricts to LAN + Tailscale. Don't add a login page.
- **Privacy is edge-enforced.** Blocklist, dwell gate, min-text, incognito guard and pause all happen in the browser before anything is sent. Don't move filtering server-side, and don't log page text on the server.
- **Deploy changes** go in `deploy/install.sh` and nowhere else; it must stay idempotent (two consecutive runs converge, verified with `DRY_RUN=1` against stubbed `systemctl`/`caddy`). Never hand-edit files under `/srv/autodidact/src` on the Pi — they are a git checkout the installer fast-forwards.
- **Tests**: every server change gets a pytest; `server/tests/test_app.py` uses `create_app(tmp_db, token)` with Flask's test client. Extension changes: at minimum `node --check`; behavior changes should be exercised by `e2e.mjs`. Run the full pytest suite before committing.
- **Commits**: small, imperative subject lines, matching the existing history ("Add FreshRSS as a second source"). Commit freely on `main`. Pushing is fine from Claude Code on the Mac if `git push` works with Paul's keys; Cowork sessions cannot push.
- **Docs**: README.md is the user-facing install/run guide; `docs/ARCHITECTURE.md` is the design; `docs/STATUS.md` is the running handoff. Update STATUS.md at the end of any session that changes state.

## Gotchas learned the hard way

- Wrapping `history.pushState` from a content script does **not** intercept the page's own calls (isolated world). SPA navigation is detected by polling `location.href` once a second. Don't "fix" this back.
- Messages sent from `pagehide`/`beforeunload` get dropped by MV3 service workers. Dwell is reported by a 15 s heartbeat instead (`DWELL_HEARTBEAT_S`), and `/dwell` upserts on the client-generated `visit_id`. The background worker coalesces queued heartbeats per visit.
- MV3 service workers are killed when idle; the queue and settings must live in `storage.local`, never in module globals. The 1-minute `alarms` retry is what wakes the worker to flush.
- The default blocklist includes `localhost`, `127.0.0.1`, `192.168.*` and `10.*`. The e2e test has to remove `127.0.0.1` to capture its fixture site; a LAN page on `204.128.136.*` is *not* blocked by default.
- Firefox MV3 treats `<all_urls>` host permission as optional. After install the user must grant "Access your data for all websites" or nothing is captured. Safari has the equivalent ("Always Allow on Every Website"). Doc issue, not a code bug.
- Firefox needs `background.scripts` (not `service_worker`) and a `browser_specific_settings.gecko.id` — `build.sh` handles both. Don't put them in `src/manifest.json` or Chromium rejects it.
- `MIN_TEXT_CHARS` is 200 in the extension and 100 on the server. The extension's is the effective gate; the server's is a sanity check for other sources (RSS excerpts). Keep the server's ≤ the extension's.
- FreshRSS cannot distinguish "read" from "mark all as read". `--mode starred` is the clean corpus; `--mode both` is the complete one. Undecided; see STATUS.md.
- FreshRSS "read time" is really "sync time" — accurate to an hour for recent items, to a day for backfill.
- `pages_fts` is an external-content FTS table. If you ever bulk-edit `pages` outside the triggers (e.g. raw `UPDATE` in a migration on `text`), run `INSERT INTO pages_fts(pages_fts) VALUES('rebuild')`.
- `e2e.mjs` binds a fixture site on port 8091 and expects the server on 8765 with token `e2e-token`; it resolves `../dist/chromium` relative to itself, so run `build.sh` first. On a machine without Playwright's bundled Chromium, set `executablePath` in the launch options.
- The UCG-Max answers a local CNAME with just the CNAME (no A); the Pi-hole chases it. If local-name resolution ever misbehaves again, an A record for `autodidact.phfactor.net → 204.128.136.3` on the UCG removes the chase.

## What not to do

- Don't add a public/internet-facing mode, OAuth, or per-user accounts. Single user, LAN + Tailscale only.
- Don't store HTML snapshots or screenshots in `pages`; if snapshots ever come, they're a separate table keyed by `page_id` (SingleFile was the idea).
- Don't add an ORM, a migration framework, or a frontend framework. The whole server is ~1,000 lines on purpose.
- Don't change the dwell default (8 s) or the blocklist seed without noting it in STATUS.md — those are the two knobs Paul intends to tune from real data.
- Don't reach for Axiom synchronously from any request handler. It sleeps.
- Don't edit the Caddyfile on the Pi by hand; the installer owns `/etc/caddy/conf.d/autodidact.caddy` and the single `import` line.

## Decisions waiting on Paul

License (none in repo); dwell threshold (tune after data); embedding model on LM Studio; FreshRSS mode (both vs starred, after data); browser-history backfill (`places.sqlite`, Chrome `History` — cheap head start, thin data); whether to rotate the token that appeared in a chat transcript.

## Working with Paul

Direct, technically fluent; wants concrete recommendations with caveats flagged rather than hedged options. Surface the decisions above as decisions to make, don't silently pick. When something fails on his machine or the Pi, ask for the exact output rather than guessing from a paraphrase.
