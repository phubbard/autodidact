# Autodidact — guide for Claude Code

Autodidact records the text of web pages Paul dwells on (browser extension) and articles he reads or stars in FreshRSS (hourly sync), stores extracted text in SQLite on the home network, and finds pages months later from vague recollections. Read `docs/ARCHITECTURE.md` for the design and reasoning, `docs/STATUS.md` for where things stand and what to do next. Keep `docs/STATUS.md` current when you finish a work session.

Owner: Paul Hubbard (pfh@phfactor.net). GitHub: `phubbard/autodidact`. Everything runs on Paul's LAN: the Flask server on the Pi 5 (`autodidact.phfactor.net`, behind Caddy, LAN-only), LLM enrichment via LM Studio on Axiom (Mac Studio, `axiom.phfactor.net:1234`, sleeps). No cloud services, ever.

## Layout

```
extension/src/        MV3 WebExtension, one codebase for Chromium + Firefox
  manifest.json       Chromium manifest; build.sh derives the Firefox one with jq
  content.js          dwell timer, Readability-style extraction, SPA detection, heartbeat
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
deploy/               systemd unit, Caddy snippet, cron lines for freshrss_sync.py and embed.py
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

# End-to-end (server must be running with AUTODIDACT_TOKEN=e2e-token; needs `npm i playwright`)
cd extension && node test/e2e.mjs

# FreshRSS sync, dry run
FRESHRSS_URL=… FRESHRSS_USER=… FRESHRSS_API_PASSWORD=… AUTODIDACT_TOKEN=… \
  python server/freshrss_sync.py --dry-run --days 3
```

There is no build step for the extension beyond copying `src/` and patching the manifest. There is no bundler, no TypeScript, no npm project — Playwright is the only JS dependency and only for the e2e test.

## Environment variables

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `AUTODIDACT_DB` | app.py, embed.py | `autodidact.db` | SQLite path; WAL mode |
| `AUTODIDACT_TOKEN` | app.py, freshrss_sync.py | `""` | Bearer token for `/ingest`, `/dwell`, `DELETE`. Empty token = ingest refused. |
| `AUTODIDACT_HOST` / `PORT` / `DEBUG` | app.py `__main__` | `127.0.0.1` / `8765` / off | gunicorn ignores these; see `deploy/autodidact.service` |
| `AUTODIDACT_URL` | freshrss_sync.py | `http://127.0.0.1:8765` | where to POST |
| `FRESHRSS_URL`, `FRESHRSS_USER`, `FRESHRSS_API_PASSWORD` | freshrss_sync.py | — | GReader API; API access must be enabled in FreshRSS admin |
| `FRESHRSS_STATE` | freshrss_sync.py | `freshrss_state.json` | JSON of seen item ids |
| `AUTODIDACT_LLM_URL` | embed.py | `http://localhost:1234/v1` | LM Studio on Axiom in prod |
| `AUTODIDACT_CHAT_MODEL` | embed.py | `""` (whatever is loaded) | for summary + tags |
| `AUTODIDACT_EMBED_MODEL` | embed.py | `text-embedding-nomic-embed-text-v1.5` | unconfirmed against real LM Studio |

On the Pi these live in `/srv/autodidact/env` (never committed). `.gitignore` already excludes `*.db*`, `venv/`, `dist/`, `node_modules/`.

## Architecture in one paragraph

Two sources feed one server. The extension is deliberately dumb: it knows the blocklist and the dwell threshold and nothing else, so policy changes happen on the Pi instead of in five browsers. Captures are extracted text only (no HTML, no screenshots), capped at 200 KB, sent as JSON with a bearer token, queued in `storage.local` and retried every minute so a down server loses nothing. The server normalizes the URL, dedups on `(url_hash, content_hash)` — same page again is a new `visits` row; same URL with changed text is a new `pages` row so old content stays searchable — and updates FTS5 synchronously via triggers. Enrichment (summary, tags, embeddings) is a separate cron job so ingest never waits on Axiom being awake. Search is layered: FTS5 BM25 now (M1), semantic merge next (M2), LLM query rewrite + rerank later (M3). Time filtering is first-class because a two-month window prunes more than any ranking trick.

## Conventions and rules

- **Python**: stdlib + Flask only on the server hot path. `trafilatura` is optional (import guarded) and only used by `freshrss_sync.py`. Don't add numpy to `app.py` until M2 semantic search actually needs it. Type hints where they help; no type checker is configured.
- **JavaScript**: plain ES2020+, no build, no modules in content/background scripts (they run as classic scripts). `const api = globalThis.browser ?? globalThis.chrome;` is the whole cross-browser shim — keep it that way. Anything that differs between Chromium and Firefox goes in `build.sh`'s jq patch, not in code paths.
- **Schema changes**: add the column to `SCHEMA` *and* append a tuple to `MIGRATIONS` in `db.py`. `connect()` applies migrations before running the idempotent schema. Add a migration test like `test_migration_adds_columns`. Never rewrite existing rows' hashes.
- **FTS**: user input goes through `fts_query()` which quotes every term so FTS5 operators can't break or inject. Keep that; do not concatenate raw user text into a MATCH clause.
- **Dedup and normalization** live only in `db.normalize_url` and the `/ingest` handler. If you change what counts as "the same URL", existing rows won't retroactively merge — say so in STATUS.md.
- **Auth**: `/ingest`, `/dwell`, and both `DELETE` routes require `Authorization: Bearer <token>`. Read routes (`/`, `/search`, `/page`, `/stats`) are unauthenticated by design because Caddy restricts to LAN. Don't add a login page.
- **Privacy is edge-enforced.** Blocklist, dwell gate, min-text, incognito guard and pause all happen in the browser before anything is sent. Don't move filtering server-side, and don't log page text on the server.
- **Tests**: every server change gets a pytest; `server/tests/test_app.py` uses `create_app(tmp_db, token)` with Flask's test client. Extension changes: at minimum `node --check`; behavior changes should be exercised by `e2e.mjs`. Run the full pytest suite before committing.
- **Commits**: small, imperative subject lines, matching the existing history ("Add FreshRSS as a second source"). Commit freely on `main`; **do not push** — Paul pushes from his Mac (the sessions' shells have no GitHub credentials).
- **Docs**: README.md is the user-facing install/run guide; `docs/ARCHITECTURE.md` is the design; `docs/STATUS.md` is the running handoff. Update STATUS.md at the end of any session that changes state.

## Gotchas learned the hard way

- Wrapping `history.pushState` from a content script does **not** intercept the page's own calls (isolated world). SPA navigation is detected by polling `location.href` once a second. Don't "fix" this back.
- Messages sent from `pagehide`/`beforeunload` get dropped by MV3 service workers. Dwell is reported by a 15 s heartbeat instead (`DWELL_HEARTBEAT_S`), and `/dwell` upserts on the client-generated `visit_id`.
- MV3 service workers are killed when idle; the queue and settings must live in `storage.local`, never in module globals. The 1-minute `alarms` retry is what wakes the worker to flush.
- Firefox MV3 treats `<all_urls>` host permission as optional. After install the user must grant "Access your data for all websites" or nothing is captured. This is a doc issue, not a code bug.
- Firefox needs `background.scripts` (not `service_worker`) and a `browser_specific_settings.gecko.id` — `build.sh` handles both. Don't put them in `src/manifest.json` or Chromium rejects it.
- `MIN_TEXT_CHARS` is 200 in the extension and 100 on the server. The extension's is the effective gate; the server's is a sanity check for other sources (RSS excerpts). Keep the server's ≤ the extension's.
- FreshRSS cannot distinguish "read" from "mark all as read". `--mode starred` is the clean corpus; `--mode both` is the complete one. Undecided; see STATUS.md.
- FreshRSS "read time" is really "sync time" — accurate to an hour for recent items, to a day for backfill.
- `pages_fts` is an external-content FTS table. If you ever bulk-edit `pages` outside the triggers (e.g. raw `UPDATE` in a migration on `text`), run `INSERT INTO pages_fts(pages_fts) VALUES('rebuild')`.
- `e2e.mjs` binds a fixture site on port 8091 and expects the server on 8765 with token `e2e-token`. It previously hard-coded a container path for `EXT`; it now resolves `../dist/chromium` relative to itself. Run `build.sh` first.

## What not to do

- Don't add a public/internet-facing mode, OAuth, or per-user accounts. Single user, LAN only.
- Don't store HTML snapshots or screenshots in `pages`; if snapshots ever come, they're a separate table keyed by `page_id` (SingleFile was the idea).
- Don't add an ORM, a migration framework, or a frontend framework. The whole server is ~1,000 lines on purpose.
- Don't change the dwell default (8 s) or the blocklist seed without noting it in STATUS.md — those are the two knobs Paul intends to tune from real data.
- Don't reach for Axiom synchronously from any request handler. It sleeps.

## Working with Paul

Direct, technically fluent; wants concrete recommendations with caveats flagged rather than hedged options. He'll make the calls on: license, dwell threshold, embedding model, Pi-vs-Axiom for the server, FreshRSS mode, browser-history backfill. Surface those as decisions to make, don't silently pick.
