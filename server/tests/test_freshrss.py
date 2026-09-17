"""Integration test: a fake FreshRSS Google-Reader endpoint → freshrss_sync → live Autodidact server."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.serving import make_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import freshrss_sync as fs  # noqa: E402
from app import create_app  # noqa: E402

TOKEN = "t"
BODY = "<p>SQLite <b>WAL</b> mode on a Raspberry Pi.</p><script>alert(1)</script>" + "<p>More words about checkpoints and durability.</p>" * 12

ITEMS = [
    {"id": "tag:google.com,2005:reader/item/0001", "title": "WAL on a Pi &amp; friends", "published": 1_757_000_000,
     "canonical": [{"href": "https://blog.example/wal?utm_source=rss"}],
     "summary": {"content": BODY}, "origin": {"title": "Example Blog"},
     "categories": ["user/-/state/com.google/read"]},
    {"id": "tag:google.com,2005:reader/item/0002", "title": "Starred one", "published": 1_757_000_100,
     "alternate": [{"href": "https://other.example/post"}],
     "content": {"content": BODY}, "origin": {"title": "Other"},
     "categories": ["user/-/state/com.google/read", "user/-/state/com.google/starred"]},
    {"id": "tag:google.com,2005:reader/item/0003", "title": "Too short", "published": 1_757_000_200,
     "alternate": [{"href": "https://short.example/x"}],
     "summary": {"content": "<p>tiny</p>"}, "origin": {"title": "Short"},
     "categories": ["user/-/state/com.google/read"]},
]


class FakeGReader(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        if self.path.endswith("/accounts/ClientLogin"):
            self.send_response(200); self.end_headers()
            self.wfile.write(b"SID=x\nAuth=paul/abc123\n")
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        FakeGReader.requests.append(self.path)
        assert self.headers.get("Authorization") == "GoogleLogin auth=paul/abc123"
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if "/state/com.google/starred" in u.path:
            items = [i for i in ITEMS if "user/-/state/com.google/starred" in i["categories"]]
            out = {"items": items}
        elif "/state/com.google/reading-list" in u.path:
            assert q.get("xt") == ["user/-/state/com.google/unread"]
            # two pages, to exercise continuation
            if q.get("c") == ["page2"]:
                out = {"items": ITEMS[2:]}
            else:
                out = {"items": ITEMS[:2], "continuation": "page2"}
        else:
            out = {"items": []}
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def servers(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "a.db"), token=TOKEN)
    srv = make_server("127.0.0.1", 0, app)
    t1 = threading.Thread(target=srv.serve_forever, daemon=True); t1.start()
    fake = HTTPServer(("127.0.0.1", 0), FakeGReader)
    t2 = threading.Thread(target=fake.serve_forever, daemon=True); t2.start()
    FakeGReader.requests.clear()

    monkeypatch.setattr(fs, "AUTODIDACT_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(fs, "AUTODIDACT_TOKEN", TOKEN)
    monkeypatch.setattr(fs, "FRESHRSS_URL", f"http://127.0.0.1:{fake.server_port}")
    monkeypatch.setattr(fs, "FRESHRSS_USER", "paul")
    monkeypatch.setattr(fs, "FRESHRSS_API_PASSWORD", "apipw")
    yield app, str(tmp_path / "state.json")
    srv.shutdown(); fake.shutdown()


def run(argv):
    sys.argv = ["freshrss_sync.py", *argv]
    fs.main()


def test_html_to_text_strips_scripts_and_keeps_paragraphs():
    t = fs.html_to_text("<p>One</p><script>x()</script><p>Two &amp; three</p>")
    assert t == "One\n\nTwo & three"


def test_sync_both_modes(servers, capsys):
    app, state = servers
    run(["--state", state, "--no-fetch-full"])
    out = capsys.readouterr().out
    assert "done: 2 sent, 1 skipped, 0 failed" in out

    with app.test_client() as c:
        st = c.get("/stats").json
        assert st["pages"] == 2 and st["by_source"] == {"rss": 2}
        hits = c.get("/search?q=checkpoints&source=rss").json["results"]
        assert {h["url"] for h in hits} == {"https://blog.example/wal", "https://other.example/post"}
        assert all(h["source"] == "rss" for h in hits)
        assert {h["feed"] for h in hits} == {"Example Blog", "Other"}
        assert all(h["published_at"] for h in hits)
        assert c.get("/search?q=checkpoints&source=web").json["results"] == []
        # Starred items are findable by the word "starred", and by feed name.
        assert [h["url"] for h in c.get("/search?q=starred").json["results"]] == ["https://other.example/post"]
        assert c.get("/search?q=example blog").json["results"][0]["url"] == "https://blog.example/wal"
        wal = next(h for h in hits if h["domain"] == "blog.example")
        page = c.get(f"/page/{wal['id']}").json
        assert "alert(1)" not in page["text"]
        assert page["title"] == "WAL on a Pi & friends"

    # Second run: everything already seen, nothing sent.
    run(["--state", state, "--no-fetch-full"])
    assert "done: 0 sent, 0 skipped, 0 failed" in capsys.readouterr().out
    assert len(json.load(open(state))["seen"]) == 3


def test_sync_starred_only(servers, capsys):
    app, state = servers
    run(["--state", state, "--mode", "starred", "--no-fetch-full"])
    assert "done: 1 sent" in capsys.readouterr().out
    assert not any("reading-list" in r for r in FakeGReader.requests)


def test_migration_adds_columns(tmp_path):
    path = str(tmp_path / "old.db")
    conn = db.connect(path)
    conn.execute("ALTER TABLE pages DROP COLUMN feed")
    conn.execute("ALTER TABLE pages DROP COLUMN published_at")
    conn.commit(); conn.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pages)")}
    assert {"source", "feed", "published_at"} <= cols
