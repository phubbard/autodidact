"""Autodidact ingest and search server.

Environment:
  AUTODIDACT_DB     path to the SQLite file (default: ./autodidact.db)
  AUTODIDACT_TOKEN  bearer token the extension must present (required for /ingest, /dwell, DELETE)
  AUTODIDACT_HOST / AUTODIDACT_PORT  bind address for `python app.py` (default 127.0.0.1:8765)
"""

import datetime as dt
import html
import json
import os
import re
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from urllib.parse import unquote

from flask import Flask, abort, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import extract
import llm
import policy
import vectors

MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 250_000
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
SOURCES = {"web", "rss", "news", "shared"}   # news = Apple News via the share sheet, shared = any other shared URL
ASK_SOURCES = 6                  # /ask: pages handed to the LLM
ASK_CHUNKS_PER_PAGE = 2          # /ask: best-matching passages per page
ASK_TIMEOUT_S = 90
FUSE_DEPTH = 100                 # hybrid search: how deep each list goes before fusing
RRF_K = 60                       # reciprocal rank fusion constant (the usual default)
MIN_URL_TEXT_CHARS = 200         # /ingest-url: below this the page is a shell (News+ exclusive, paywall, JS-only app)
REQUEST_LOG_KEEP = 2000          # rows kept in the requests table for /debug
REQUEST_LOG_SKIP = ("/debug",)   # the debug page polling itself is not interesting
DLQ_PATHS = {"/ingest", "/ingest-url", "/dwell"}   # unexpected errors here are dead-lettered, not 500s
REPLAY_HEADER = "X-Autodidact-Replay"              # marks a request re-sent from the dead-letter queue


def create_app(db_path=None, token=None, policy_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("AUTODIDACT_DB", "autodidact.db")
    # The capture policy lives next to the database (data/ on the Pi, which the service may write).
    app.config["POLICY_PATH"] = policy_path or os.environ.get("AUTODIDACT_POLICY") or os.path.join(
        os.path.dirname(os.path.abspath(app.config["DB_PATH"])), "policy.txt")
    policy_file = policy.PolicyFile(app.config["POLICY_PATH"])
    # Semantic search model (vectors.py); missing files or packages just mean keyword-only search.
    app.config["MODEL_DIR"] = os.environ.get("AUTODIDACT_MODEL_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(app.config["DB_PATH"])), "models", vectors.MODEL)
    semantic = {"encoder": None, "index": None, "loaded": False}

    def _semantic():
        """(encoder, index) for this worker, loaded on first use; (None, None) when unavailable."""
        if not semantic["loaded"]:
            semantic["loaded"] = True
            enc = app.config.get("ENCODER")  # tests inject a fake
            if enc is None:
                try:
                    enc = vectors.load_encoder(app.config["MODEL_DIR"])
                except Exception:  # noqa: BLE001 — a broken model file must not break search
                    app.logger.exception("could not load %s", app.config["MODEL_DIR"])
            if enc is not None:
                semantic["encoder"], semantic["index"] = enc, vectors.VectorIndex()
        return semantic["encoder"], semantic["index"]

    def _warm():
        # Load the model and vectors when the worker starts, not on the first search: under load
        # (a backfill using the CPU) that first search took long enough to fail.
        with app.app_context():
            try:
                enc, index = _semantic()
                if enc is not None:
                    enc.encode(["warm up"])
                    index.refresh(get_db())
            except Exception:  # noqa: BLE001
                app.logger.exception("semantic warm-up failed")

    if os.environ.get("AUTODIDACT_WARM") == "1":  # set by the systemd unit; off for tests and dev
        threading.Thread(target=_warm, name="semantic-warmup", daemon=True).start()
    app.config["TOKEN"] = token if token is not None else os.environ.get("AUTODIDACT_TOKEN", "")
    app.config["JSON_SORT_KEYS"] = False
    # gunicorn listens on loopback only and Caddy sets X-Forwarded-For, so the
    # last hop in that header is the real client. Requests that skip Caddy
    # (freshrss_sync.py, curl on the Pi) have no header and stay 127.0.0.1.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    # Make sure the schema exists before the first request.
    db.connect(app.config["DB_PATH"]).close()

    # ---------- plumbing ----------

    def get_db():
        if "db" not in g:
            g.db = db.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def start_timer():
        g.t0 = time.perf_counter()

    @app.after_request
    def log_request(resp):
        if request.path.startswith(REQUEST_LOG_SKIP) or request.path.startswith("/static"):
            return resp
        try:
            note = g.get("req_note")
            if note is None and resp.status_code >= 400 and not resp.is_json:
                m = re.search(r"<p>(.*?)</p>", resp.get_data(as_text=True), re.S)
                note = html.unescape(m.group(1)).strip().split(". ")[0] if m else None
            conn = get_db()
            if resp.status_code >= 500:
                # The request failed part-way; committing the log entry below must not also commit
                # whatever it had half-written.
                conn.rollback()
            cur = conn.execute(
                "INSERT INTO requests (ts, method, path, query, status, ms, ip, ua, device, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), request.method, request.path[:300],
                 request.query_string.decode("utf-8", "replace")[:300] or None, resp.status_code,
                 round((time.perf_counter() - g.get("t0", time.perf_counter())) * 1000, 1),
                 request.remote_addr, (request.headers.get("User-Agent") or "")[:300] or None,
                 unquote(request.headers.get("X-Autodidact-Device") or "").strip()[:64] or None,
                 (note or "")[:200] or None),
            )
            if cur.lastrowid % 100 == 0:
                conn.execute("DELETE FROM requests WHERE id <= ?", (cur.lastrowid - REQUEST_LOG_KEEP,))
            conn.commit()
        except Exception as e:  # logging must never break a request
            app.logger.warning("request log failed: %s", e)
        return resp

    @app.after_request
    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Autodidact-Device"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return resp

    @app.route("/<path:_any>", methods=["OPTIONS"])
    @app.route("/", methods=["OPTIONS"])
    def preflight(_any=None):
        return "", 204

    # /ingest-url is called by a Shortcut that can only show what it gets back, so every outcome
    # there, failures included, is JSON with a ready-to-show "message". Other routes keep Flask's
    # default error pages.
    def _ingest_url_error(code, reason):
        g.req_note = reason
        return jsonify({"ok": False, "error": reason, "message": f"Not saved: {reason}"}), code

    @app.errorhandler(HTTPException)
    def http_error(e):
        if request.path != "/ingest-url":
            return e
        reason = "wrong or missing token" if e.code == 401 else (e.description or e.name)
        return _ingest_url_error(e.code, reason)

    @app.errorhandler(Exception)
    def unexpected_error(e):
        if request.path not in DLQ_PATHS or request.method != "POST":
            raise e
        app.logger.exception("%s failed", request.path)
        error = f"{type(e).__name__}: {e}"[:500]
        if request.headers.get(REPLAY_HEADER):
            # A replay that failed again: report it, don't queue a second copy.
            g.req_note = f"replay failed: {error}"[:200]
            return jsonify({"ok": False, "error": error}), 500
        try:
            dl_id = _dead_letter(error, traceback.format_exc())
        except Exception:  # noqa: BLE001 — the DB itself is unhappy; let the client retry later
            app.logger.exception("could not write dead letter")
            if request.path == "/ingest-url":
                return _ingest_url_error(503, f"server error ({type(e).__name__}), and it could not be queued")
            return jsonify({"ok": False, "error": error}), 503
        g.req_note = f"dead-lettered #{dl_id}: {error}"[:200]
        # 202: accepted for later processing. The extension treats any 2xx as delivered and moves on.
        reply = {"ok": False, "dead_lettered": dl_id, "error": error}
        if request.path == "/ingest-url":
            reply["message"] = f"Not saved yet: server error ({type(e).__name__}); kept for retry as #{dl_id}"
        return jsonify(reply), 202

    def _dead_letter(error, tb):
        conn = get_db()
        conn.rollback()  # drop whatever the failed request half-wrote
        body = request.get_data(cache=True)
        parsed = request.get_json(silent=True)
        url = (parsed.get("url") if isinstance(parsed, dict) else None) or None
        cur = conn.execute(
            "INSERT INTO dead_letters (ts, path, content_type, body, error, traceback, url, ip, ua, device)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), request.path, request.content_type, body, error, tb,
             str(url)[:2000] if url else None, request.remote_addr,
             (request.headers.get("User-Agent") or "")[:300] or None,
             unquote(request.headers.get("X-Autodidact-Device") or "").strip()[:64] or None))
        conn.commit()
        return cur.lastrowid

    def require_token(f):
        @wraps(f)
        def wrapper(*a, **kw):
            expected = app.config["TOKEN"]
            if not expected:
                abort(503, "AUTODIDACT_TOKEN is not configured on the server")
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {expected}":
                abort(401)
            return f(*a, **kw)
        return wrapper

    # ---------- ingest ----------

    def _store_page(body: dict, source: str, min_chars: int):
        """Shared by /ingest and /ingest-url. Validates, dedups, records a visit; returns the JSON reply."""
        body = {k: _clean_text(v) if isinstance(v, str) else v for k, v in body.items()}
        url = (body.get("url") or "").strip()
        text = (body.get("text") or "").strip()
        if not url.startswith(("http://", "https://")):
            abort(400, "url must be http(s)")
        if len(text) < min_chars:
            abort(400, f"text shorter than {min_chars} chars")
        text = text[:MAX_TEXT_CHARS]

        canonical = (body.get("canonical_url") or "").strip()
        if canonical.startswith(("http://", "https://")) and db.domain_of(canonical) == db.domain_of(url):
            url = canonical

        norm = db.normalize_url(url)
        url_hash = db.sha256(norm)
        content_hash = db.sha256(text)
        now = int(body.get("captured_at") or time.time())
        dwell = max(0, int(body.get("dwell_s") or 0))
        visit_id = (body.get("visit_id") or "").strip() or None
        browser = _browser_name(body.get("browser") or "")
        feed = (body.get("feed") or "").strip()[:200] or None
        published_at = int(body["published_at"]) if body.get("published_at") else None

        conn = get_db()
        row = conn.execute(
            "SELECT id FROM pages WHERE url_hash = ? AND content_hash = ?", (url_hash, content_hash)
        ).fetchone()
        created = row is None
        if created:
            cur = conn.execute(
                """INSERT INTO pages (url, url_hash, content_hash, title, description, text, lang, domain,
                                      first_seen, last_seen, visit_count, total_dwell_s, source, feed, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)""",
                (norm, url_hash, content_hash, (body.get("title") or "")[:500],
                 (body.get("description") or "")[:2000], text, (body.get("lang") or "")[:16],
                 db.domain_of(norm), now, now, source, feed, published_at),
            )
            page_id = cur.lastrowid
        else:
            page_id = row["id"]

        cur = conn.execute(
            "INSERT OR IGNORE INTO visits (visit_id, page_id, seen_at, dwell_s, browser) VALUES (?, ?, ?, ?, ?)",
            (visit_id, page_id, now, dwell, browser),
        )
        new_visit = cur.rowcount == 1
        if not new_visit and visit_id:
            conn.execute("UPDATE visits SET dwell_s = MAX(dwell_s, ?) WHERE visit_id = ?", (dwell, visit_id))
        _refresh_page_stats(conn, page_id)
        conn.commit()
        g.req_note = f"{source} {db.domain_of(norm)}: " + (
            "new page" if created else "new visit" if new_visit else "already stored")
        return {"id": page_id, "created": created, "new_visit": new_visit, "url": norm,
                "title": (body.get("title") or "")[:500]}

    @app.post("/ingest")
    @require_token
    def ingest():
        body = request.get_json(silent=True) or {}
        source = (body.get("source") or "web").strip().lower()
        if source not in SOURCES:
            abort(400, f"source must be one of {sorted(SOURCES)}")
        if source == "web":
            # Extensions apply the same policy before sending; this catches ones that have not
            # fetched it yet (or predate it). A 200 tells them to drop the item from their queue.
            url = (body.get("url") or "").strip()
            page = {"platform": body.get("platform"), "blog_front": body.get("blog_front")}
            rule = policy_file.get().blocks(url, page) if url.startswith(("http://", "https://")) else None
            if rule:  # (a malformed URL falls through to _store_page's 400)
                g.req_note = f"skipped by policy: {rule}"
                return jsonify({"skipped": rule})
        res = _store_page(body, source, MIN_TEXT_CHARS)
        return jsonify({"id": res["id"], "created": res["created"], "new_visit": res["new_visit"]})

    @app.post("/ingest-url")
    @require_token
    def ingest_url():
        """Share-sheet entry point: the server fetches and extracts the page itself.

        Body: {"url": …, "title"?: …, "note"?: …, "source"?: "news"|"shared", "client"?: "iPhone"}.
        Apple News links are unwrapped to the publisher's URL when the story has one; a
        News+ exclusive is stored with the landing page's title/description and the apple.news link.
        """
        body = request.get_json(silent=True) or {}
        raw = body.get("url")
        if isinstance(raw, list):  # a Shortcuts list variable arrives as a JSON array
            raw = next((x for x in raw if isinstance(x, str) and x.strip()), "")
        url = raw.strip() if isinstance(raw, str) else ""
        if not url.startswith(("http://", "https://")):
            # Say what arrived: a Shortcut wired to the wrong variable is otherwise hard to diagnose.
            got = "nothing" if raw in (None, "", []) else repr(raw)[:120]
            abort(400, f"url must be http(s); got {got} (keys: {', '.join(sorted(body)) or 'none'})")
        source = (body.get("source") or "").strip().lower()
        if not source:
            source = "news" if extract.is_apple_news(url) else "shared"
        if source not in SOURCES:
            abort(400, f"source must be one of {sorted(SOURCES)}")

        try:
            art = extract.fetch_article(url)
        except extract.FetchError as e:
            abort(502, str(e))

        title = (body.get("title") or "").strip() or art["title"] or art["url"]
        note = (body.get("note") or "").strip()
        text = art["text"]
        shell = len(text) < MIN_URL_TEXT_CHARS
        if shell:
            # Nothing much extractable (News+ exclusive, paywall shell, JS-only page): keep what we know
            # so the story is still findable by title, and say so in the stored text.
            text = "\n".join(p for p in (title, art["description"], text,
                                         f"No article text could be fetched from {art['url']}") if p)
        if note:
            text = note + "\n\n" + text
        description = art["description"]
        if source == "news":
            description = ("apple news · " + description).strip(" ·")

        res = _store_page({
            "url": art["url"],
            "canonical_url": art["canonical"],
            "title": title,
            "description": description,
            "text": text,
            "lang": art["lang"],
            "visit_id": (body.get("visit_id") or "").strip() or None,
            "browser": (body.get("client") or "Shortcut")[:40],
        }, source, 1)
        res["via"] = art["via"] or (url if extract.is_apple_news(url) else "")
        res["chars"] = len(text)
        res["source"] = source
        res["ok"] = True
        verb = "Saved" if res["created"] else "Already saved"
        res["message"] = f"{verb}: {title}" + (" (title and link only; no article text)" if shell else "")
        return jsonify(res)

    @app.post("/dwell")
    @require_token
    def dwell():
        body = request.get_json(silent=True) or {}
        visit_id = (body.get("visit_id") or "").strip()
        dwell_s = max(0, int(body.get("dwell_s") or 0))
        if not visit_id:
            abort(400, "visit_id required")
        conn = get_db()
        row = conn.execute("SELECT page_id FROM visits WHERE visit_id = ?", (visit_id,)).fetchone()
        if row is None:
            abort(404, "unknown visit_id")
        g.req_note = f"{dwell_s}s"
        conn.execute("UPDATE visits SET dwell_s = MAX(dwell_s, ?) WHERE visit_id = ?", (dwell_s, visit_id))
        _refresh_page_stats(conn, row["page_id"])
        conn.commit()
        return jsonify({"ok": True})

    # ---------- search ----------

    def _run_search(q, limit, offset):
        """Hybrid (or keyword, or recent) search with the filters in request.args. Returns the /search reply."""
        after = _parse_date(request.args.get("after"))
        before = _parse_date(request.args.get("before"), end_of_day=True)
        domain = (request.args.get("domain") or "").strip().lower()
        source = (request.args.get("source") or "").strip().lower()

        conn = get_db()
        where, params = [], []
        if after:
            where.append("p.last_seen >= ?"); params.append(after)
        if before:
            where.append("p.first_seen <= ?"); params.append(before)
        if domain:
            where.append("(p.domain = ? OR p.domain LIKE ?)"); params.extend([domain, f"%.{domain}"])
        if source in SOURCES:
            where.append("p.source = ?"); params.append(source)
        filt = (" AND " + " AND ".join(where)) if where else ""

        cols = """p.id, p.url, p.title, p.domain, p.first_seen, p.last_seen, p.visit_count,
                  p.total_dwell_s, p.source, p.feed, p.published_at"""
        encoder, index = _semantic()

        if not q:
            rows = conn.execute(
                f"""SELECT {cols}, substr(p.text, 1, 240) AS snippet, 0 AS score
                    FROM pages p WHERE 1=1 {filt} ORDER BY p.last_seen DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
            return {"q": q, "mode": "recent", "results": [_hit(r) for r in rows]}

        sql = f"""SELECT {cols},
                         snippet(pages_fts, 2, '<mark>', '</mark>', ' … ', 28) AS snippet,
                         bm25(pages_fts, 3.0, 2.0, 1.0, 3.0, 3.0, 1.0) AS score
                  FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid
                  WHERE pages_fts MATCH ? {filt}
                  ORDER BY score LIMIT ? OFFSET ?"""
        mode = "all"
        rows = []
        use_semantic = encoder is not None and len(q) >= 3
        # With semantic search on, take a deeper keyword list to fuse; paginate after fusing.
        k_limit, k_offset = (FUSE_DEPTH, 0) if use_semantic else (limit, offset)
        for mode, fts in (("all", fts_query(q, "AND")), ("any", fts_query(q, "OR"))):
            if not fts:
                break
            rows = conn.execute(sql, (fts, *params, k_limit, k_offset)).fetchall()
            if rows:
                break
        if not use_semantic:
            return {"q": q, "mode": mode, "results": [_hit(r) for r in rows]}

        try:
            index.refresh(conn)
            allowed = ({r[0] for r in conn.execute(f"SELECT p.id FROM pages p WHERE 1=1 {filt}", params)}
                       if where else None)
            near = index.search(encoder.encode([q])[0], k=FUSE_DEPTH, allowed=allowed)
        except Exception:  # noqa: BLE001 — semantic trouble degrades to keyword search
            app.logger.exception("semantic search failed")
            return {"q": q, "mode": mode, "results": [_hit(r) for r in rows[offset:offset + limit]]}

        # Reciprocal rank fusion: robust to the two lists' scores being on different scales.
        fused, keyword_hits, semantic_hits = {}, {}, {}
        keyword_ids = {r["id"] for r in rows}
        for rank, r in enumerate(rows):
            fused[r["id"]] = fused.get(r["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
            keyword_hits[r["id"]] = r
        for rank, (pid, chunk, cos) in enumerate(near):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
            semantic_hits[pid] = (chunk, cos)
        page_ids = sorted(fused, key=fused.get, reverse=True)[offset:offset + limit]
        extra = [pid for pid in page_ids if pid not in keyword_hits]
        if extra:
            marks = ",".join("?" * len(extra))
            for r in conn.execute(f"SELECT {cols}, p.title, p.description, p.text, p.summary FROM pages p "
                                  f"WHERE p.id IN ({marks})", extra):
                snippet = vectors.chunk_text(r, semantic_hits[r["id"]][0]).replace("\n", " ")[:240]
                keyword_hits[r["id"]] = {**{k: r[k] for k in r.keys() if k not in ("text", "description", "summary")},
                                         "snippet": html.escape(snippet) + " …", "score": 0}
        results = []
        for pid in page_ids:
            if pid not in keyword_hits:
                continue  # deleted between the two queries
            hit = _hit(keyword_hits[pid])
            in_keyword = pid in keyword_ids
            hit["match"] = "both" if in_keyword and pid in semantic_hits else "keyword" if in_keyword else "semantic"
            if pid in semantic_hits:
                hit["similarity"] = round(semantic_hits[pid][1], 3)
            hit["fused"] = round(fused[pid], 5)
            results.append(hit)
        return {"q": q, "mode": "hybrid", "keyword_mode": mode, "results": results}

    ASK_PROMPT = (
        "You help Paul find and recall things he has read. Answer his question using only the numbered "
        "sources below, which are passages from pages he read, with the date he read them. Cite sources "
        "inline as [1], [2]. Be brief and concrete (names, numbers, part numbers when present). If the "
        "sources do not answer the question, say so in one sentence and point to the closest sources. "
        "Never invent details that are not in the sources.")

    def _passages(page, qvec, index):
        """The page's best-matching chunks for the question, or its opening when it has no vectors."""
        if qvec is not None and index is not None:
            scored = index.chunk_scores(page["id"], qvec)
            texts = [vectors.chunk_text(page, ch) for ch, _ in scored[:ASK_CHUNKS_PER_PAGE]]
            texts = [t for t in texts if t]
            if texts:
                return texts
        return [(page["text"] or "")[:1800]]

    @app.get("/ask")
    def ask():
        """Answer a question from the archive: hybrid retrieval, then the LLM on Axiom writes an answer
        citing the sources. If the LLM is unavailable the reply still carries the sources."""
        q = (request.args.get("q") or "").strip()
        if len(q) < 3:
            abort(400, "ask a question")
        t0 = time.perf_counter()
        found = _run_search(q, ASK_SOURCES, 0)["results"]
        sources, blocks = [], []
        if found:
            conn = get_db()
            encoder, index = _semantic()
            qvec = encoder.encode([q])[0] if encoder is not None else None
            marks = ",".join("?" * len(found))
            pages = {r["id"]: r for r in conn.execute(
                f"SELECT id, title, description, text, summary, url, domain, first_seen FROM pages WHERE id IN ({marks})",
                [h["id"] for h in found])}
            for h in found:
                page = pages.get(h["id"])
                if page is None:
                    continue
                n = len(sources) + 1
                sources.append({"n": n, "id": page["id"], "title": page["title"] or page["url"], "url": page["url"],
                                "domain": page["domain"], "first_seen_iso": _fmt_ts(page["first_seen"])})
                text = "\n…\n".join(_passages(page, qvec, index))
                blocks.append(f"[{n}] {page['title'] or page['url']} ({page['domain']}, read {_fmt_ts(page['first_seen'])[:10]})\n{text}")
        reply = {"q": q, "answer": None, "sources": sources}
        if not sources:
            reply["error"] = "Nothing in the archive matches that."
            return jsonify(reply)
        ask_llm = app.config.get("LLM") or llm.chat   # tests inject a fake
        if not (app.config.get("LLM") or llm.configured()):
            reply["error"] = "No LLM configured (AUTODIDACT_LLM_URL); here are the closest pages."
            return jsonify(reply)
        messages = [{"role": "system", "content": ASK_PROMPT},
                    {"role": "user", "content": "Sources:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {q}"}]
        try:
            answer, model_name = ask_llm(messages, timeout=ASK_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — Axiom asleep, slow, or LM Studio down: sources still help
            app.logger.warning("ask: LLM failed: %s", e)
            reply["error"] = f"The LLM did not answer ({type(e).__name__}); here are the closest pages."
            return jsonify(reply)
        # gpt-oss writes citations as 【1】 or 【1†L3-L5】; the UI links plain [1].
        answer = re.sub(r"【(\d+)[^】]*】", r"[\1]", answer)
        reply.update(answer=answer, model=model_name, ms=round((time.perf_counter() - t0) * 1000))
        g.req_note = f"answered with {len(sources)} sources in {reply['ms']} ms"
        return jsonify(reply)

    @app.get("/search")
    def search():
        q = (request.args.get("q") or "").strip()
        limit = min(MAX_LIMIT, max(1, int(request.args.get("limit") or DEFAULT_LIMIT)))
        offset = max(0, int(request.args.get("offset") or 0))
        return jsonify(_run_search(q, limit, offset))

    @app.get("/page/<int:page_id>")
    def page(page_id):
        conn = get_db()
        row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row is None:
            abort(404)
        visits = conn.execute(
            "SELECT seen_at, dwell_s, browser FROM visits WHERE page_id = ? ORDER BY seen_at DESC", (page_id,)
        ).fetchall()
        d = dict(row)
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["visits"] = [dict(v) for v in visits]
        if request.accept_mimetypes.best == "text/html" or request.args.get("format") == "html":
            try:
                related = _related(page_id)
            except Exception:  # noqa: BLE001 — the page itself must still render
                app.logger.exception("related pages failed for %s", page_id)
                related = []
            return render_template("page.html", page=d, fmt=_fmt_ts, related=related)
        return jsonify(d)

    def _related(page_id, limit=8):
        """Pages nearest in meaning to *page_id*: [{id, title, url, domain, source, similarity, ...}].
        Other stored versions of the same URL are left out. [] when semantic search is unavailable."""
        encoder, index = _semantic()
        if encoder is None:
            return []
        conn = get_db()
        index.refresh(conn)
        vec = index.page_vector(page_id)
        if vec is None:
            return []
        me = conn.execute("SELECT url_hash FROM pages WHERE id = ?", (page_id,)).fetchone()
        near = [n for n in index.search(vec, k=limit * 3 + 5) if n[0] != page_id]
        if not near:
            return []
        sims = {pid: cos for pid, _, cos in near}
        marks = ",".join("?" * len(sims))
        rows = {r["id"]: r for r in conn.execute(
            f"""SELECT id, url, url_hash, title, domain, source, feed, first_seen, last_seen FROM pages
                WHERE id IN ({marks})""", list(sims))}
        out, seen_urls = [], {me["url_hash"]} if me else set()
        for pid, _, cos in near:
            r = rows.get(pid)
            if r is None or r["url_hash"] in seen_urls:
                continue
            seen_urls.add(r["url_hash"])
            d = _hit({k: r[k] for k in r.keys() if k != "url_hash"})
            d["similarity"] = round(cos, 3)
            out.append(d)
            if len(out) == limit:
                break
        return out

    @app.get("/page/<int:page_id>/related")
    def page_related(page_id):
        if get_db().execute("SELECT 1 FROM pages WHERE id = ?", (page_id,)).fetchone() is None:
            abort(404)
        limit = min(50, max(1, int(request.args.get("limit") or 8)))
        return jsonify({"id": page_id, "related": _related(page_id, limit)})

    @app.delete("/page/<int:page_id>")
    @require_token
    def delete_page(page_id):
        conn = get_db()
        cur = conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        conn.commit()
        if cur.rowcount == 0:
            abort(404)
        return jsonify({"deleted": page_id})

    @app.delete("/domain/<domain>")
    @require_token
    def delete_domain(domain):
        conn = get_db()
        cur = conn.execute("DELETE FROM pages WHERE domain = ? OR domain LIKE ?", (domain, f"%.{domain}"))
        conn.commit()
        return jsonify({"deleted": cur.rowcount, "domain": domain})

    @app.get("/policy")
    def get_policy():
        return jsonify(policy_file.get().as_json())

    @app.get("/stats")
    def stats():
        conn = get_db()
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        visits = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        domains = conn.execute("SELECT COUNT(DISTINCT domain) FROM pages").fetchone()[0]
        enriched = conn.execute("SELECT COUNT(*) FROM pages WHERE summary IS NOT NULL").fetchone()[0]
        by_source = {r["source"]: r["n"] for r in
                     conn.execute("SELECT source, COUNT(*) AS n FROM pages GROUP BY source")}
        newest = conn.execute("SELECT MAX(last_seen) FROM pages").fetchone()[0]
        size = os.path.getsize(app.config["DB_PATH"]) if os.path.exists(app.config["DB_PATH"]) else 0
        dead = conn.execute("SELECT COUNT(*) FROM dead_letters WHERE replayed_at IS NULL").fetchone()[0]
        return jsonify({"pages": pages, "visits": visits, "domains": domains, "enriched": enriched,
                        "by_source": by_source, "newest": newest, "db_bytes": size, "dead_letters": dead})

    # ---------- dead-letter queue ----------

    @app.get("/debug/dlq")
    def dlq_list():
        show_all = request.args.get("all") == "1"
        rows = get_db().execute(
            "SELECT id, ts, path, content_type, length(body) AS bytes, substr(body, 1, 400) AS head, error, url, ip,"
            " ua, device, replayed_at, replay_result FROM dead_letters"
            + ("" if show_all else " WHERE replayed_at IS NULL") + " ORDER BY id DESC LIMIT 200").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # A safe, printable preview: bytes as received, non-ASCII and control characters escaped.
            d["preview"] = bytes(d.pop("head")).decode("ascii", "backslashreplace")
            d["time_iso"] = dt.datetime.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            d["browser"], d["os"] = _ua_summary(d["ua"] or "")
            out.append(d)
        return jsonify({"dead_letters": out})

    @app.get("/debug/dlq/<int:dl_id>")
    def dlq_item(dl_id):
        row = get_db().execute("SELECT * FROM dead_letters WHERE id = ?", (dl_id,)).fetchone()
        if row is None:
            abort(404)
        if request.args.get("raw") == "1":  # the exact bytes, for offline debugging
            return app.response_class(bytes(row["body"]), mimetype="application/octet-stream")
        d = dict(row)
        d["body"] = bytes(d["body"]).decode("ascii", "backslashreplace")
        return jsonify(d)

    def _replay(row):
        """Send a dead letter back through its route. Returns (ok, result)."""
        headers = {"Authorization": f"Bearer {app.config['TOKEN']}", REPLAY_HEADER: str(row["id"])}
        if row["ua"]:
            headers["User-Agent"] = row["ua"]
        if row["device"]:
            headers["X-Autodidact-Device"] = row["device"]
        r = app.test_client().post(row["path"], data=bytes(row["body"]),
                                   content_type=row["content_type"] or "application/json", headers=headers)
        result = r.get_data(as_text=True)[:500]
        ok = 200 <= r.status_code < 300
        conn = get_db()
        conn.execute("UPDATE dead_letters SET replayed_at = ?, replay_result = ? WHERE id = ?",
                     (time.time() if ok else None, f"{r.status_code} {result}", row["id"]))
        conn.commit()
        return ok, {"id": row["id"], "status": r.status_code, "ok": ok, "result": r.get_json(silent=True) or result}

    @app.post("/dlq/<int:dl_id>/replay")
    @require_token
    def dlq_replay_one(dl_id):
        row = get_db().execute("SELECT * FROM dead_letters WHERE id = ?", (dl_id,)).fetchone()
        if row is None:
            abort(404)
        if row["replayed_at"]:
            abort(409, f"dead letter #{dl_id} was already replayed")
        ok, res = _replay(row)
        return jsonify(res), (200 if ok else 500)

    @app.post("/dlq/replay")
    @require_token
    def dlq_replay_all():
        rows = get_db().execute("SELECT * FROM dead_letters WHERE replayed_at IS NULL ORDER BY id").fetchall()
        results = [_replay(row)[1] for row in rows]
        return jsonify({"replayed": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results),
                        "results": results})

    @app.delete("/dlq/<int:dl_id>")
    @require_token
    def dlq_delete(dl_id):
        conn = get_db()
        cur = conn.execute("DELETE FROM dead_letters WHERE id = ?", (dl_id,))
        conn.commit()
        if cur.rowcount == 0:
            abort(404)
        return jsonify({"deleted": dl_id})

    @app.get("/")
    def index():
        return render_template("index.html")

    # ---------- debug ----------

    @app.get("/debug")
    def debug_page():
        return render_template("debug.html")

    @app.get("/debug/requests")
    def debug_requests():
        limit = min(500, max(1, int(request.args.get("limit") or 50)))
        quiet = request.args.get("quiet") == "1"
        # quiet hides the routine noise (preflights, successful heartbeats) but keeps failures.
        where = "WHERE method != 'OPTIONS' AND NOT (path = '/dwell' AND status < 400)" if quiet else ""
        rows = [dict(r) for r in get_db().execute(
            f"SELECT * FROM requests {where} ORDER BY id DESC LIMIT ?", (limit,))]
        names = _hostnames({r["ip"] for r in rows if r["ip"]})
        for r in rows:
            r["browser"], r["os"] = _ua_summary(r["ua"] or "")
            r["hostname"] = names.get(r["ip"], "")
            r["time_iso"] = dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"now": time.time(), "requests": rows})

    return app


# ---------- helpers ----------

_TOKEN_RE = re.compile(r'"([^"]+)"|(\S+)')
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def fts_query(q: str, joiner: str = "AND") -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Quoted phrases are kept as phrases; everything else is split into words and
    quoted so FTS5 operators in user input cannot break the query. The last bare
    word gets a prefix wildcard so partial words match while typing.
    """
    terms = []
    for phrase, word in _TOKEN_RE.findall(q):
        if phrase:
            words = _WORD_RE.findall(phrase)
            if words:
                terms.append('"' + " ".join(words) + '"')
        elif word:
            for w in _WORD_RE.findall(word):
                terms.append(f'"{w}"')
    if not terms:
        return ""
    if " " not in terms[-1] and len(terms[-1]) >= 5:  # a bare word of 3+ chars, in quotes
        terms[-1] = terms[-1] + "*"
    return f" {joiner} ".join(terms)


def _parse_date(s, end_of_day=False):
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    try:
        d = dt.datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        abort(400, f"bad date: {s}")
    if end_of_day:
        d = d + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    return int(d.timestamp())


def _fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _hit(r):
    d = dict(r)
    d["first_seen_iso"] = _fmt_ts(d["first_seen"])
    d["last_seen_iso"] = _fmt_ts(d["last_seen"])
    return d


_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _clean_text(s: str) -> str:
    """Replace lone UTF-16 surrogates with U+FFFD.

    JavaScript strings can hold half an emoji (a page's own broken text, or a slice that cut a pair
    in two); JSON carries it as "\\ud83d" and Python decodes it to a str that cannot be encoded as
    UTF-8. Hashing or storing it then raised UnicodeEncodeError, a 500 the extension retried forever.
    Properly paired surrogates never reach here: json.loads joins them into one character.
    """
    return _LONE_SURROGATE_RE.sub("\ufffd", s)


def _refresh_page_stats(conn, page_id):
    conn.execute(
        """UPDATE pages SET
             visit_count   = (SELECT COUNT(*) FROM visits WHERE page_id = ?),
             total_dwell_s = (SELECT COALESCE(SUM(dwell_s), 0) FROM visits WHERE page_id = ?),
             -- A page can have no visits: when an open tab's text changes, the extension re-sends it
             -- under the same visit_id, the new text becomes a new pages row, and the visit stays with
             -- the first version. MAX() is then NULL; keep the row's own timestamp instead.
             last_seen     = COALESCE((SELECT MAX(seen_at) FROM visits WHERE page_id = ?), last_seen)
           WHERE id = ?""",
        (page_id, page_id, page_id, page_id),
    )


def _browser_name(ua: str) -> str:
    ua = ua or ""
    for name in ("Firefox", "Edg", "OPR", "Brave", "Vivaldi", "Arc", "Chrome", "Safari"):
        if name in ua:
            return {"Edg": "Edge", "OPR": "Opera"}.get(name, name)
    return ua[:40]


_UA_BROWSERS = [  # first match wins; Chromium browsers (Brave, Arc, Vivaldi) all report Chrome
    (re.compile(r"Edg(?:A|iOS)?/(\d+)"), "Edge"),
    (re.compile(r"OPR/(\d+)"), "Opera"),
    (re.compile(r"(?:Firefox|FxiOS)/(\d+)"), "Firefox"),
    (re.compile(r"(?:Chrome|CriOS)/(\d+)"), "Chrome"),
    (re.compile(r"Version/(\d+(?:\.\d+)?).*Safari/"), "Safari"),
]
_UA_OS = [("iPhone", "iOS"), ("iPad", "iPadOS"), ("Android", "Android"), ("Windows", "Windows"),
          ("CrOS", "ChromeOS"), ("Macintosh", "macOS"), ("Linux", "Linux")]


def _ua_summary(ua: str):
    """(browser, os) from a User-Agent string. OS versions in UAs are frozen, so none are shown."""
    if not ua:
        return "", ""
    for prefix, name in (("Python-urllib", "Python urllib"), ("curl/", "curl"),
                         ("NetworkingExtension", "iOS link preview")):
        if ua.startswith(prefix):
            return name, ""
    if "facebookexternalhit" in ua:
        return "link preview", ""
    os_name = next((name for key, name in _UA_OS if key in ua), "")
    for rx, name in _UA_BROWSERS:
        m = rx.search(ua)
        if m:
            return f"{name} {m.group(1)}", os_name
    return ua.split(" ")[0][:40], os_name


_rdns_cache: dict = {}   # ip -> (hostname, expires_at)
_rdns_lock = threading.Lock()
_rdns_pool = None


def _reverse_dns(ip: str) -> str:
    try:
        name = "localhost" if ip in ("127.0.0.1", "::1") else socket.gethostbyaddr(ip)[0]
    except (OSError, ValueError):
        name = ""
    with _rdns_lock:
        _rdns_cache[ip] = (name, time.time() + (3600 if name else 600))
    return name


def _hostnames(ips, timeout=0.8) -> dict:
    """Best-effort reverse DNS, cached. Lookups that miss the deadline finish in
    the background and fill the cache for the next poll; they never block long."""
    global _rdns_pool
    now, out, pending = time.time(), {}, {}
    with _rdns_lock:
        for ip in ips:
            hit = _rdns_cache.get(ip)
            if hit and hit[1] > now:
                out[ip] = hit[0]
            else:
                pending[ip] = None
        if pending and _rdns_pool is None:
            _rdns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rdns")
    for ip in pending:
        pending[ip] = _rdns_pool.submit(_reverse_dns, ip)
    deadline = now + timeout
    for ip, fut in pending.items():
        try:
            out[ip] = fut.result(timeout=max(0.0, deadline - time.time()))
        except Exception:
            out[ip] = ""
    return out


if __name__ == "__main__":
    application = create_app()
    application.run(
        host=os.environ.get("AUTODIDACT_HOST", "127.0.0.1"),
        port=int(os.environ.get("AUTODIDACT_PORT", "8765")),
        debug=os.environ.get("AUTODIDACT_DEBUG") == "1",
    )
