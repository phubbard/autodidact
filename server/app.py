"""Autodidact ingest and search server.

Environment:
  AUTODIDACT_DB     path to the SQLite file (default: ./autodidact.db)
  AUTODIDACT_TOKEN  bearer token the extension must present (required for /ingest, /dwell, DELETE)
  AUTODIDACT_HOST / AUTODIDACT_PORT  bind address for `python app.py` (default 127.0.0.1:8765)
"""

import datetime as dt
import json
import os
import re
import time
from functools import wraps

from flask import Flask, abort, g, jsonify, render_template, request

import db

MIN_TEXT_CHARS = 100
MAX_TEXT_CHARS = 250_000
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
SOURCES = {"web", "rss"}


def create_app(db_path=None, token=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("AUTODIDACT_DB", "autodidact.db")
    app.config["TOKEN"] = token if token is not None else os.environ.get("AUTODIDACT_TOKEN", "")
    app.config["JSON_SORT_KEYS"] = False

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

    @app.after_request
    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return resp

    @app.route("/<path:_any>", methods=["OPTIONS"])
    @app.route("/", methods=["OPTIONS"])
    def preflight(_any=None):
        return "", 204

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

    @app.post("/ingest")
    @require_token
    def ingest():
        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        text = (body.get("text") or "").strip()
        if not url.startswith(("http://", "https://")):
            abort(400, "url must be http(s)")
        if len(text) < MIN_TEXT_CHARS:
            abort(400, f"text shorter than {MIN_TEXT_CHARS} chars")
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
        source = (body.get("source") or "web").strip().lower()
        if source not in SOURCES:
            abort(400, f"source must be one of {sorted(SOURCES)}")
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
        return jsonify({"id": page_id, "created": created, "new_visit": new_visit})

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
            abort(404)
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


def _refresh_page_stats(conn, page_id):
    conn.execute(
        """UPDATE pages SET
             visit_count   = (SELECT COUNT(*) FROM visits WHERE page_id = ?),
             total_dwell_s = (SELECT COALESCE(SUM(dwell_s), 0) FROM visits WHERE page_id = ?),
             last_seen     = (SELECT MAX(seen_at) FROM visits WHERE page_id = ?)
           WHERE id = ?""",
        (page_id, page_id, page_id, page_id),
    )


def _browser_name(ua: str) -> str:
    ua = ua or ""
    for name in ("Firefox", "Edg", "OPR", "Brave", "Vivaldi", "Arc", "Chrome", "Safari"):
        if name in ua:
            return {"Edg": "Edge", "OPR": "Opera"}.get(name, name)
    return ua[:40]


if __name__ == "__main__":
    application = create_app()
    application.run(
        host=os.environ.get("AUTODIDACT_HOST", "127.0.0.1"),
        port=int(os.environ.get("AUTODIDACT_PORT", "8765")),
        debug=os.environ.get("AUTODIDACT_DEBUG") == "1",
    )
