# Autodidact — status and handoff

Updated 2026-09-18. Keep this current at the end of every working session: what changed, what's next, what's blocked on Paul.

## Where things stand

Milestone 1 (capture + keyword search, two sources) is **built and tested, not yet deployed**. Nothing is running on the Pi yet; no real corpus exists.

- Repo: `phubbard/autodidact`, `main` at 920e941 + this handoff commit. Local checkout `~/code/autodidact` on Paul's Mac.
- CI (`.github/workflows/ci.yml`): pushed, first run's result **not yet confirmed**. Check the Actions tab.
- Tests: 23 pytest passing locally; `extension/test/e2e.mjs` passed in headless Chromium.
- The design doc that used to live in a Claude doc is now `docs/ARCHITECTURE.md`. The Claude Project ("autodidact") is being retired in favor of this repo + `CLAUDE.md`.

## What exists (component by component)

**Extension** (`extension/`): MV3, one codebase, `build.sh` derives Firefox. Dwell-gated capture (8 s visible+focused), Readability-style extraction, SPA detection by URL polling, 15 s dwell heartbeat, blocklist with host globs, 1 h pause, incognito guard, `storage.local` queue with 1-min alarm retry, badge, options page with "Test connection", popup with pause / block-this-domain / open-search.

**Server** (`server/app.py`, `db.py`): Flask + SQLite (WAL). `pages` deduped on normalized-URL hash + content hash, `visits` upserted on client `visit_id`, `embeddings`, `pages_fts` (FTS5 porter, external content, insert/update/delete triggers). Routes: `POST /ingest`, `POST /dwell` (bearer), `GET /search` (AND then OR fallback, last-word prefix, `after`/`before`/`domain`/`source`, recent listing when `q` empty), `GET /page/<id>` JSON or `?format=html`, `DELETE /page/<id>`, `DELETE /domain/<d>` (bearer), `GET /stats` with by_source, search UI at `/` with time chips, site box, browser/rss chips. `MIGRATIONS` list in `db.py` adds columns to pre-existing DBs on open.

**FreshRSS sync** (`server/freshrss_sync.py`): Google Reader API (ClientLogin, `stream/contents` with `xt=unread` for read items, starred stream, continuation paging). HTML→text via stdlib `HTMLParser`; full-article fetch when excerpt < 700 chars (trafilatura if installed, else `<article>`/`<main>`). POSTs `source=rss`, feed title, `published_at`, `visit_id="freshrss:<item id>"`; puts `"starred · <feed>"` in description so both are searchable. JSON state file of seen ids. Flags: `--mode both|read|starred`, `--days`, `--dry-run`, `--limit`, `--state`.

**Enrichment** (`server/embed.py`): M2 job — summary + 5 tags (flow into FTS via trigger) and embeddings as float32 blobs, against an OpenAI-compatible endpoint. **Written, never run against a real model.** Model ids are guesses.

**Deploy** (`deploy/`): systemd unit (gunicorn, 2 workers, 127.0.0.1:8765, `User=pi`, `/srv/autodidact/…`), Caddy snippet for `autodidact.phfactor.net` with LAN-only `remote_ip` matcher, cron lines for `freshrss_sync.py` (hourly at :17, `--days 7`) and `embed.py` (every 15 min, `--batch 50`, `AUTODIDACT_LLM_URL=http://axiom.phfactor.net:1234/v1`).

## Next steps, in order

1. **Confirm CI is green** on GitHub Actions for 920e941 and this commit. If the extension job fails it's probably `jq`/`zip` availability on the runner.
2. **Deploy to the Pi.** Runbook:
   ```sh
   sudo mkdir -p /srv/autodidact/data && sudo chown -R pi /srv/autodidact
   git clone git@github.com:phubbard/autodidact.git /srv/autodidact/src   # or rsync
   ln -s /srv/autodidact/src/server /srv/autodidact/server
   python3 -m venv /srv/autodidact/venv && /srv/autodidact/venv/bin/pip install -r /srv/autodidact/server/requirements.txt
   # /srv/autodidact/env  (chmod 600):
   #   AUTODIDACT_TOKEN=$(openssl rand -hex 24)
   #   FRESHRSS_URL=https://…  FRESHRSS_USER=…  FRESHRSS_API_PASSWORD=…
   sudo cp deploy/autodidact.service /etc/systemd/system/ && sudo systemctl enable --now autodidact
   # append deploy/Caddyfile.snippet to the Caddyfile, reload caddy
   sudo cp deploy/freshrss.cron /etc/cron.d/autodidact-freshrss
   sudo cp deploy/embed.cron    /etc/cron.d/autodidact-embed     # harmless if Axiom is asleep
   curl -s https://autodidact.phfactor.net/stats
   ```
   In FreshRSS: Administration → Authentication → allow API access; set an API password on the profile. Dry-run the sync first: `python freshrss_sync.py --dry-run --days 3`.
3. **Install the extension** everywhere: `extension/build.sh`, then load unpacked (`dist/chromium`) in Chrome / Brave / Arc / Edge; temporary add-on in Firefox (and grant "Access your data for all websites" in the add-on's Permissions tab). Set server URL + token in Settings, hit "Test connection". For a permanent Firefox install, submit `dist/autodidact-firefox.zip` to AMO as unlisted and install the signed `.xpi`.
4. **Let the corpus grow** for a few weeks. Then: tune the dwell threshold from the `visits` table (distribution of `dwell_s` on pages Paul later searched for vs. never touched); decide FreshRSS `--mode` (both vs starred) by which corpus searches better.
5. **M2**: run `embed.py` against LM Studio on Axiom; confirm the loaded model ids (`AUTODIDACT_EMBED_MODEL`, `AUTODIDACT_CHAT_MODEL`); check summary/tag quality on ~20 pages; then add semantic merge into `/search` (embed the query, cosine over an in-memory float32 matrix, blend with BM25 rank).
6. **M3**: LLM query rewrite (keywords + date range) and rerank of top 20; date slider in UI. **M4**: Safari via `xcrun safari-web-extension-converter extension/dist/chromium`.

## Decisions waiting on Paul

- License (none in repo).
- Pi 5 vs Axiom for the server — leaning Pi for storage/search, Axiom for enrichment only; deploy files assume this.
- Dwell threshold (8 s default) — tune after data.
- Embedding model on LM Studio — code defaults to `text-embedding-nomic-embed-text-v1.5`.
- FreshRSS mode (both vs starred) — after data.
- Backfill from browser history files (`places.sqlite`, Chrome `History`) — cheap head start, thin data.

## Known rough edges

- `embed.py` is untested against a real endpoint; expect small API-shape fixes on first run.
- `e2e.mjs` requires a manually started server with `AUTODIDACT_TOKEN=e2e-token` and `npm i playwright` in `extension/`; it is not wired into CI.
- Firefox host-permission grant is a manual post-install step; easy to forget, results in silent non-capture.
- `MIN_TEXT_CHARS` differs between extension (200) and server (100) on purpose; documented in CLAUDE.md.
- No retention or size monitoring; `/stats` is the only visibility. Fine for a year at projected volumes.

## Session log

- 2026-09-16 — Design doc written ("Autodidact Architecture").
- 2026-09-18 — M1 built: extension, server, FTS5, tests, e2e (b7c5db6). FreshRSS source added (9d52dec). CI added (920e941). Pushed to GitHub.
- 2026-09-18 — Handoff to a Claude Code project: added `CLAUDE.md`, `docs/ARCHITECTURE.md` (exported from the Claude doc), this file, `.claude/settings.json`; fixed hard-coded container path in `e2e.mjs`.
