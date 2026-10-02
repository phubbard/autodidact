# Autodidact — status and handoff

Updated 2026-10-01. Keep this current at the end of every working session: what changed, what's next, what's blocked on Paul.

## Where things stand

Milestone 1 (capture + keyword search, two sources) is **built, tested, and deployed**. The server runs on the Pi (`webserver`) under systemd at `/srv/autodidact`, Caddy serves `https://autodidact.phfactor.net` to LAN + Tailscale, and `/stats` answers. FreshRSS sync is **enabled** (hourly at :17) and its first run put 1,162 pages from 137 domains into the corpus. No browser captures yet: the extensions still have to be loaded. Enrichment is not enabled.

- Repo: `phubbard/autodidact`, `main` at a7dce39 plus this session's commit. Local checkout `~/code/autodidact` on Paul's Mac.
- CI (`.github/workflows/ci.yml`): **green** on 920e941 and 711b098 (both jobs, ~20 s).
- Tests: 23 pytest passing locally; `extension/test/e2e.mjs` passed in headless Chromium.
- The design doc that used to live in a Claude doc is now `docs/ARCHITECTURE.md`. The Claude Project ("autodidact") is being retired in favor of this repo + `CLAUDE.md`.

## What exists (component by component)

**Extension** (`extension/`): MV3, one codebase, `build.sh` derives Firefox. Dwell-gated capture (8 s visible+focused), Readability-style extraction, SPA detection by URL polling, 15 s dwell heartbeat, blocklist with host globs, 1 h pause, incognito guard, `storage.local` queue with 1-min alarm retry, badge, options page with "Test connection", popup with pause / block-this-domain / open-search.

**Server** (`server/app.py`, `db.py`): Flask + SQLite (WAL). `pages` deduped on normalized-URL hash + content hash, `visits` upserted on client `visit_id`, `embeddings`, `pages_fts` (FTS5 porter, external content, insert/update/delete triggers). Routes: `POST /ingest`, `POST /dwell` (bearer), `GET /search` (AND then OR fallback, last-word prefix, `after`/`before`/`domain`/`source`, recent listing when `q` empty), `GET /page/<id>` JSON or `?format=html`, `DELETE /page/<id>`, `DELETE /domain/<d>` (bearer), `GET /stats` with by_source, search UI at `/` with time chips, site box, browser/rss chips. `MIGRATIONS` list in `db.py` adds columns to pre-existing DBs on open.

**FreshRSS sync** (`server/freshrss_sync.py`): Google Reader API (ClientLogin, `stream/contents` with `xt=unread` for read items, starred stream, continuation paging). HTML→text via stdlib `HTMLParser`; full-article fetch when excerpt < 700 chars (trafilatura if installed, else `<article>`/`<main>`). POSTs `source=rss`, feed title, `published_at`, `visit_id="freshrss:<item id>"`; puts `"starred · <feed>"` in description so both are searchable. JSON state file of seen ids. Flags: `--mode both|read|starred`, `--days`, `--dry-run`, `--limit`, `--state`.

**Enrichment** (`server/embed.py`): M2 job — summary + 5 tags (flow into FTS via trigger) and embeddings as float32 blobs, against an OpenAI-compatible endpoint. **Written, never run against a real model.** Model ids are guesses.

**Deploy** (`deploy/install.sh`): one idempotent installer/updater, tested in a sandbox against stubbed `systemctl`/`caddy` (two consecutive runs converge; the generated unit's `ExecStart` was exercised against a real gunicorn). Clone/pull to `/srv/autodidact/src`, venv, `env` with generated token (600), hardened systemd unit (gunicorn 2 workers × 4 threads, `ProtectSystem=strict`, `ReadWritePaths=data`), Caddy site in `conf.d` with `remote_ip` = detected LAN CIDR + `100.64.0.0/10` (Tailscale) + loopback, `caddy validate` before reload, cron entries only for configured sources (`FRESHRSS_URL` → hourly sync at :17; `AUTODIDACT_LLM_URL` → embed every 15 min), smoke test of `/stats` locally and via https. The static `deploy/*.service|*.snippet|*.cron` files were removed in favor of the script. Note the earlier Caddy snippet's `192.168.0.0/16 10.0.0.0/8` matcher would have locked Paul out of his own (public /24) LAN; the detection fixes that.

## Next steps, in order

1. ~~Confirm CI is green~~ — done 2026-09-18.
2. ~~Deploy to the Pi~~ — done 2026-10-01 via `deploy/install.sh` (user `pfh`). DNS: local CNAME on the UCG + Cloudflare CNAME added; the Pi-hole had cached NXDOMAIN from the install run and needed `pihole restartdns`.
3. **Install the extension** — done on the MacBook Air for Brave, Safari and Zen (2026-10-01; all three seen posting to `/ingest` with 200s). Remaining browsers/machines as wanted. Instructions: `extension/build.sh`, then load unpacked (`dist/chromium`) in Chrome / Brave / Arc / Edge; temporary add-on in Firefox (and grant "Access your data for all websites" in the add-on's Permissions tab). Set server URL + token in Settings, hit "Test connection". For a permanent Firefox install, submit `dist/autodidact-firefox.zip` to AMO as unlisted and install the signed `.xpi`.
4. **Let the corpus grow** for a few weeks. Then: tune the dwell threshold from the `visits` table (distribution of `dwell_s` on pages Paul later searched for vs. never touched); decide FreshRSS `--mode` (both vs starred) by which corpus searches better.
5. **M2**: run `embed.py` against LM Studio on Axiom; confirm the loaded model ids (`AUTODIDACT_EMBED_MODEL`, `AUTODIDACT_CHAT_MODEL`); check summary/tag quality on ~20 pages; then add semantic merge into `/search` (embed the query, cosine over an in-memory float32 matrix, blend with BM25 rank).
6. **Safari, macOS + iOS (M4, started early)**: one combined Xcode project in `safari/Autodidact/` (replaced the Mac-only project on 2026-10-01; same bundle ids and team). Builds for macOS, iOS simulator and device. Enabled in Mac Safari; installed on Paul's iPhone 16 Pro, where Test connection reaches the server. Both capture normally since the queue fix under "Open bugs". README.md has the Safari section.
7. **M3**: LLM query rewrite (keywords + date range) and rerank of top 20; date slider in UI.

## Decisions waiting on Paul

- License (none in repo).
- Pi 5 vs Axiom for the server — leaning Pi for storage/search, Axiom for enrichment only; deploy files assume this.
- Dwell threshold (8 s default) — tune after data.
- Embedding model on LM Studio — code defaults to `text-embedding-nomic-embed-text-v1.5`.
- FreshRSS mode (both vs starred) — the cron runs the default `both`. Data point from 2026-10-01: 1,110 read items and **0 starred** in the previous 7 days, so `starred` would be an empty corpus unless Paul starts starring. About 160 items a day, heaviest feeds NOTUS (176/wk), Chronoscout (70), Boing Boing (67), Ars Technica (63).
- Backfill from browser history files (`places.sqlite`, Chrome `History`) — cheap head start, thin data.

## Open bugs

- **Safari queue never drained — fixed 2026-10-01, confirmed on Mac Safari and the iPhone.** Safari's background console showed `Invalid call to browser.storage.local.set(). Exceeded storage quota.` at the queue write in `flush()`, with only a couple of pages queued. The POST had already succeeded, the item was never removed, and it was re-posted every minute while everything behind it waited. Fix: `unlimitedStorage` permission in the manifest (lifts Safari's `storage.local` quota), plus `saveQueue()` in `background.js`, which removes the key and retries when a write is rejected. Why Safari counts a small queue as over quota was not established. A stubbed-API simulation reproduces the stall with the old code and drains with the new. Rebuilt for Mac Safari and reinstalled on the iPhone. **Mac Safari confirmed** from the gunicorn log: after the rebuild the stuck item was sent once more at 22:09:01 with its `/dwell`, and the next capture at 22:09:26 arrived alone. **iPhone confirmed** at 22:11: the stuck item went out once more with its `/dwell`, then `lobste.rs` and `jimmyhmiller.com` arrived as new pages with heartbeats and no repeats.
- The extension used to capture the Autodidact search UI itself; `background.js` now skips any page on the configured server's origin (`skipped: 'server'`). The six copies already captured (from Safari and Zen) were deleted on 2026-10-01 with `DELETE /domain/autodidact.phfactor.net`, with Paul's OK. Brave, Chromium and Zen keep capturing the search UI until they are reloaded with the new build.

## Known rough edges

- `embed.py` is untested against a real endpoint; expect small API-shape fixes on first run.
- `e2e.mjs` requires a manually started server with `AUTODIDACT_TOKEN=e2e-token` and `npm i playwright` in `extension/`; it is not wired into CI.
- Firefox host-permission grant is a manual post-install step; easy to forget, results in silent non-capture.
- `MIN_TEXT_CHARS` differs between extension (200) and server (100) on purpose; documented in CLAUDE.md.
- Full-article fetch gets 401 from `marganna.phfactor.net` (family gate) and 403 from some paywalled sites; those items are stored with the feed's own text, and the failure is one log line each.
- No retention or size monitoring; `/stats` is the only visibility. Fine for a year at projected volumes.

## Session log

- 2026-09-16 — Design doc written ("Autodidact Architecture").
- 2026-09-18 — M1 built: extension, server, FTS5, tests, e2e (b7c5db6). FreshRSS source added (9d52dec). CI added (920e941). Pushed to GitHub.
- 2026-09-18 — Handoff to a Claude Code project: added `CLAUDE.md`, `docs/ARCHITECTURE.md` (exported from the Claude doc), this file, `.claude/settings.json`; fixed hard-coded container path in `e2e.mjs`.
- 2026-09-18 — Resumed in Claude Code on the Mac: CI confirmed green, 23 tests pass locally (Python 3.14), removed stray `Claude outputs/` duplicate of `.claude/settings.json`.
- 2026-10-01 — Wrote and sandbox-tested `deploy/install.sh`; removed the static deploy files; README/CLAUDE.md updated.
- 2026-10-01 — Deployed on webserver with the installer (clean run; local `/stats` OK). DNS records added; Pi-hole negative cache cleared with `pihole restartdns`; https resolves. Built `extension/dist`. Safari converter attempted, errored (undiagnosed). CLAUDE.md rewritten as a full handoff for Claude Code on the Mac; the Cowork/Claude Project side is retired.
- 2026-10-01 — Claude Code on the Mac: enabled FreshRSS sync on the Pi (`FRESHRSS_URL=http://127.0.0.1:8090`, user `pfh`; dry run verified on both the loopback and `https://news.phfactor.net`), re-ran the installer, ran the first sync by hand: 1,166 sent, 34 skipped, 0 failed in about 5 minutes; DB 10 MB. Reproduced and fixed the Safari build error (signing team + bundle id case). CLAUDE.md updated to match.
- 2026-10-01 — Brave (unpacked `dist/chromium`) and Safari (Xcode build; needed a Clean Build Folder after the signing fix) are both working, per Paul (`/stats` showed 1 `web` page at the time). Zen (Firefox fork, 1.22.3b) is next: its build has `MOZ_REQUIRE_SIGNING: false`, so an unsigned `.xpi` can be installed permanently once `xpinstall.signatures.required` is turned off in about:config; `build.sh` now also writes `dist/autodidact-firefox.xpi`.
- 2026-10-01 — Zen installed and confirmed from the gunicorn log (Firefox/156 user agent). Three browsers now feed the corpus. Noticed the extension captured the Autodidact search UI itself (`autodidact.phfactor.net`); the server's own host is not in the default blocklist. Open question for Paul: skip the configured server origin in `background.js`.
- 2026-10-01 — iOS: generated a combined iOS + macOS Safari project in `safari2/` (untracked; converter run without `--macos-only`, team added to all four targets). Builds for simulator, macOS and device (wildcard team provisioning profile, no new App IDs). Installed on Paul's iPhone 16 Pro with `xcrun devicectl device install app`. Xcode's "platform doesn't match" error is the scheme selector: `Autodidact (iOS)` goes with the iPhone, `Autodidact (macOS)` with My Mac. Pending: Paul enables it in iOS Safari and confirms capture; then decide whether `safari2/` replaces `safari/`.
- 2026-10-01 — Swapped the combined iOS + macOS project into `safari/` (Paul's call). Rebuilt the macOS scheme into the default DerivedData and removed the throwaway builds, because Safari had started loading the extension from a scratch `-derivedDataPath` build. While checking for the iPhone's first capture, found the Safari flush bug above. Corpus: 1,162 rss + 10 web.
- 2026-10-01 — Paul pulled the Safari console error (storage quota on the queue write). Added `unlimitedStorage`, `saveQueue()` fallback and the skip-own-server rule; rebuilt `dist/`, the Mac Safari app and the iPhone app. Brave, Chromium and Zen need a reload/reinstall to pick the change up (they were not affected by the bug). `test_freshrss.py` is flaky (connection reset, about 1 run in 3); being fixed in a separate session.
- 2026-10-01 — Fixed flaky `test_freshrss.py` (16 of 25 suite runs failed with `ConnectionResetError` in `FreshRSS._login`). The fake Google Reader handler answered the ClientLogin POST and closed without reading the request body; `http.client` sends headers and body in separate writes, so the body often arrived unread and the kernel sent RST instead of FIN. The handler now drains `Content-Length` bytes. Test-harness bug only; real FreshRSS reads the body, so `freshrss_sync.py` is unchanged. 40 consecutive full-suite runs pass.
- 2026-10-01 — iPhone capture confirmed after the queue fix. Five clients feed the corpus: Brave, Chromium (if loaded), Zen, Mac Safari, iPhone Safari. Mac Safari captured an `appstoreconnect.apple.com` page; blocklist additions for signed-in admin sites are Paul's call.
- 2026-10-01 — Zen updated to 0.1.1 (verified in the profile's installed `.xpi`). README.md gained Safari (macOS + iOS), unsigned Firefox-fork install and update notes. Flaky FreshRSS test fixed in a parallel session (595979f). Removed the `git push` deny rule from `.claude/settings.json` at Paul's request, so Claude Code on the Mac can push.
