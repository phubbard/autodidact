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
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from urllib.parse import unquote

from flask import Flask, abort, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import extract

MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 250_000
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
SOURCES = {"web", "rss", "news", "shared"}   # news = Apple News via the share sheet, shared = any other shared URL
MIN_URL_TEXT_CHARS = 200         # /ingest-url: below this the page is a shell (News+ exclusive, paywall, JS-only app)
REQUEST_LOG_KEEP = 2000          # rows kept in the requests table for /debug
REQUEST_LOG_SKIP = ("/debug",)   # the debug page polling itself is not interesting


def create_app(db_path=None, token=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("AUTODIDACT_DB", "autodidact.db")
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
        if request.path != "/ingest-url":
            raise e
        app.logger.exception("ingest-url failed")
        return _ingest_url_error(500, f"server error ({type(e).__name__}); see journalctl -u autodidact")

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

    @app.get("/search")
    def search():
        q = (request.args.get("q") or "").strip()
        after = _parse_date(request.args.get("after"))
        before = _parse_date(request.args.get("before"), end_of_day=True)
        domain = (request.args.get("domain") or "").strip().lower()
        source = (request.args.get("source") or "").strip().lower()
        limit = min(MAX_LIMIT, max(1, int(request.args.get("limit") or DEFAULT_LIMIT)))
        offset = max(0, int(request.args.get("offset") or 0))

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

        if not q:
            rows = conn.execute(
                f"""SELECT {cols}, substr(p.text, 1, 240) AS snippet, 0 AS score
                    FROM pages p WHERE 1=1 {filt} ORDER BY p.last_seen DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
            return jsonify({"q": q, "mode": "recent", "results": [_hit(r) for r in rows]})

        sql = f"""SELECT {cols},
                         snippet(pages_fts, 2, '<mark>', '</mark>', ' … ', 28) AS snippet,
                         bm25(pages_fts, 3.0, 2.0, 1.0, 3.0, 3.0, 1.0) AS score
                  FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid
                  WHERE pages_fts MATCH ? {filt}
                  ORDER BY score LIMIT ? OFFSET ?"""
        mode = "all"
        rows = []
        for mode, fts in (("all", fts_query(q, "AND")), ("any", fts_query(q, "OR"))):
            if not fts:
                break
            rows = conn.execute(sql, (fts, *params, limit, offset)).fetchall()
            if rows:
                break
        return jsonify({"q": q, "mode": mode, "results": [_hit(r) for r in rows]})

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
            return render_template("page.html", page=d, fmt=_fmt_ts)
        return jsonify(d)

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
        return jsonify({"pages": pages, "visits": visits, "domains": domains, "enriched": enriched,
                        "by_source": by_source, "newest": newest, "db_bytes": size})

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
