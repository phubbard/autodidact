"""SQLite schema and helpers for Autodidact."""

import hashlib
import os
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pages (
  id            INTEGER PRIMARY KEY,
  url           TEXT NOT NULL,
  url_hash      TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  title         TEXT,
  description   TEXT,
  text          TEXT NOT NULL,
  lang          TEXT,
  domain        TEXT NOT NULL,
  first_seen    INTEGER NOT NULL,
  last_seen     INTEGER NOT NULL,
  visit_count   INTEGER NOT NULL DEFAULT 1,
  total_dwell_s INTEGER NOT NULL DEFAULT 0,
  summary       TEXT,
  tags          TEXT,
  source        TEXT NOT NULL DEFAULT 'web',   -- 'web' (extension) or 'rss' (FreshRSS)
  feed          TEXT,                          -- feed title for rss items
  published_at  INTEGER,                       -- item publish time for rss items
  UNIQUE(url_hash, content_hash)
);
CREATE INDEX IF NOT EXISTS pages_source ON pages(source);
CREATE INDEX IF NOT EXISTS pages_url_hash ON pages(url_hash);
CREATE INDEX IF NOT EXISTS pages_last_seen ON pages(last_seen);
CREATE INDEX IF NOT EXISTS pages_domain ON pages(domain);

CREATE TABLE IF NOT EXISTS visits (
  id       INTEGER PRIMARY KEY,
  visit_id TEXT UNIQUE,
  page_id  INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  seen_at  INTEGER NOT NULL,
  dwell_s  INTEGER NOT NULL DEFAULT 0,
  browser  TEXT
);
CREATE INDEX IF NOT EXISTS visits_page ON visits(page_id);
CREATE INDEX IF NOT EXISTS visits_seen ON visits(seen_at);

CREATE TABLE IF NOT EXISTS embeddings (
  page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
  model   TEXT NOT NULL,
  dim     INTEGER NOT NULL,
  vector  BLOB NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
  title, description, text, summary, tags, domain,
  content='pages', content_rowid='id',
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
  INSERT INTO pages_fts(rowid, title, description, text, summary, tags, domain)
  VALUES (new.id, new.title, new.description, new.text, new.summary, new.tags, new.domain);
END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, title, description, text, summary, tags, domain)
  VALUES ('delete', old.id, old.title, old.description, old.text, old.summary, old.tags, old.domain);
END;
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE OF title, description, text, summary, tags, domain ON pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, title, description, text, summary, tags, domain)
  VALUES ('delete', old.id, old.title, old.description, old.text, old.summary, old.tags, old.domain);
  INSERT INTO pages_fts(rowid, title, description, text, summary, tags, domain)
  VALUES (new.id, new.title, new.description, new.text, new.summary, new.tags, new.domain);
END;
"""

TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|gclsrc|dclid|msclkid|mc_cid|mc_eid|yclid|_ga|_gl|ref|ref_src|"
    r"igshid|si|s_kwcid|sscid|cmpid|source|trk|trkCampaign|vero_id|_hsenc|_hsmi|hsCtaTracking)$",
    re.I,
)


# Columns added after the first release, applied to existing databases on open.
MIGRATIONS = [
    ("pages", "source", "TEXT NOT NULL DEFAULT 'web'"),
    ("pages", "feed", "TEXT"),
    ("pages", "published_at", "INTEGER"),
]


def connect(path: str) -> sqlite3.Connection:
    """Open (and initialise) the database at *path*."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages'").fetchone()
    if exists:
        _migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate(conn):
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def normalize_url(url: str) -> str:
    """Strip fragments, tracking params, default ports and trailing slashes.

    Two URLs that normalise to the same string are the same page for dedup.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query, doseq=True), ""))


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host
