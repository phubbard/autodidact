"""POST /ingest-url and extract.py against a local fixture site (article, redirect, apple.news-style landing pages)."""

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extract  # noqa: E402
from app import create_app  # noqa: E402

TOKEN = "t"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
BODY_P = "".join(
    f"<p>Paragraph {i}: SQLite write-ahead logging on a Raspberry Pi. The checkpoint interval, synchronous=NORMAL "
    f"and the page size all matter on SD cards, and readers never block writers in WAL mode.</p>" for i in range(1, 6))

ARTICLE = f"""<!doctype html><html lang="en"><head><title>WAL on a Pi | Example</title>
<meta property="og:title" content="WAL tuning on a Raspberry Pi">
<meta name="description" content="Notes on SQLite WAL mode on SD cards.">
<link rel="canonical" href="/wal?ref=canonical">
</head><body><nav>Home About Contact navigation boilerplate</nav>
<article><h1>WAL tuning on a Raspberry Pi</h1>{BODY_P}</article>
<footer>Footer copyright boilerplate</footer><script>var x = 1;</script></body></html>"""

# What an apple.news landing page does for a story that has a web version.
LANDING_REFRESH = """<html><head><title>Apple News</title>
<meta http-equiv="refresh" content="0; url=/wal"></head><body>Redirecting…</body></html>"""
LANDING_SCRIPT = """<html><head><title>Apple News</title></head>
<body><script>window.location.replace("http://127.0.0.1:{port}/wal");</script></body></html>"""
# A News+ exclusive: no web version, only the landing page's own metadata.
LANDING_EXCLUSIVE = """<html><head><title>Apple News</title>
<meta property="og:title" content="Inside the Watch Workshop — Hodinkee">
<meta property="og:description" content="A visit to a Geneva atelier.">
</head><body><a href="https://apps.apple.com/app/apple-news/id1066498020">Open in Apple News</a></body></html>"""


class Site(BaseHTTPRequestHandler):
    port = 0

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", status=200, extra=None):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/wal":
            self._send(ARTICLE)
        elif path == "/go":
            self._send("", status=302, extra={"Location": "/wal?utm_source=share"})
        elif path == "/landing-refresh":
            self._send(LANDING_REFRESH)
        elif path == "/landing-script":
            self._send(LANDING_SCRIPT.format(port=Site.port))
        elif path == "/landing-exclusive":
            self._send(LANDING_EXCLUSIVE)
        elif path == "/file.pdf":
            self._send("%PDF-1.4", ctype="application/pdf")
        elif path == "/boom":
            self._send("nope", status=500)
        else:
            self._send("not found", status=404)


@pytest.fixture(scope="module")
def site():
    srv = HTTPServer(("127.0.0.1", 0), Site)
    Site.port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), token=TOKEN)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------- extract.py ----------

def test_extract_metadata_and_text():
    out = extract.extract(ARTICLE, "http://example.test/wal")
    assert out["title"] == "WAL tuning on a Raspberry Pi"
    assert out["description"] == "Notes on SQLite WAL mode on SD cards."
    assert out["canonical"] == "http://example.test/wal?ref=canonical"
    assert out["lang"] == "en"
    assert "checkpoint interval" in out["text"]
    assert "navigation boilerplate" not in out["text"]
    assert "var x" not in out["text"]


def test_unwrap_apple_news():
    # Relative refresh targets resolve against the landing URL.
    assert extract.unwrap_apple_news(LANDING_REFRESH, "https://apple.news/Abc") == "https://apple.news/wal"
    page = '<meta http-equiv="refresh" content="0;URL=https://pub.example/story?utm_campaign=apple_news">'
    assert extract.unwrap_apple_news(page, "https://apple.news/Abc") == "https://pub.example/story?utm_campaign=apple_news"
    page = 'window.location.replace("https://pub.example/x")'
    assert extract.unwrap_apple_news(page, "https://apple.news/Abc") == "https://pub.example/x"
    assert extract.unwrap_apple_news(LANDING_EXCLUSIVE, "https://apple.news/Abc") is None
    assert extract.is_apple_news("https://apple.news/AbCdEf") and not extract.is_apple_news("https://example.com/apple.news")


def test_fetch_rejects_non_html(site):
    with pytest.raises(extract.FetchError):
        extract.fetch(site + "/file.pdf")
    with pytest.raises(extract.FetchError):
        extract.fetch(site + "/boom")
    with pytest.raises(extract.FetchError):
        extract.fetch("ftp://example.test/x")


# ---------- /ingest-url ----------

def test_ingest_url_requires_token(client, site):
    assert client.post("/ingest-url", json={"url": site + "/wal"}).status_code == 401


def test_ingest_url_fetches_and_stores(client, site):
    r = client.post("/ingest-url", json={"url": site + "/wal", "client": "iPhone"}, headers=HEADERS)
    assert r.status_code == 200, r.data
    j = r.json
    assert j["created"] and j["source"] == "shared" and j["chars"] > 300
    assert j["title"] == "WAL tuning on a Raspberry Pi"
    assert j["url"] == f"{site}/wal"          # canonical's ?ref= is stripped by normalisation
    page = client.get(f"/page/{j['id']}").json
    assert page["source"] == "shared" and page["lang"] == "en"
    assert "checkpoint interval" in page["text"] and "boilerplate" not in page["text"]
    assert page["visits"][0]["browser"] == "iPhone"
    # Findable.
    assert client.get("/search?q=checkpoint&source=shared").json["results"][0]["id"] == j["id"]


def test_ingest_url_follows_redirect_and_dedups_with_extension_capture(client, site):
    # The extension captured the page first…
    ext = client.post("/ingest", json={"url": site + "/wal", "title": "x", "text": extract.extract(ARTICLE, site)["text"],
                                        "visit_id": "v1"}, headers=HEADERS).json
    # …then the same story arrives via a redirecting share link: same page, second visit.
    r = client.post("/ingest-url", json={"url": site + "/go"}, headers=HEADERS).json
    assert r["id"] == ext["id"] and not r["created"] and r["new_visit"]
    assert client.get(f"/page/{ext['id']}").json["visit_count"] == 2


def test_ingest_url_note_and_title_override(client, site):
    r = client.post("/ingest-url", json={"url": site + "/wal", "title": "My title", "note": "for the Pi project"},
                    headers=HEADERS).json
    page = client.get(f"/page/{r['id']}").json
    assert page["title"] == "My title"
    assert page["text"].startswith("for the Pi project\n\n")
    assert client.get("/search?q=pi project").json["results"]


def test_ingest_url_unwraps_apple_news_landing_pages(client, site, monkeypatch):
    # The script-redirect form is only followed on apple.news hosts; pretend the fixture is one.
    monkeypatch.setattr(extract, "APPLE_NEWS_HOSTS", extract.APPLE_NEWS_HOSTS + ("127.0.0.1",))
    for path in ("/landing-refresh", "/landing-script"):
        r = client.post("/ingest-url", json={"url": site + path, "source": "news"}, headers=HEADERS)
        assert r.status_code == 200, r.data
        j = r.json
        assert j["source"] == "news"
        assert j["url"] == f"{site}/wal", path         # stored under the publisher URL, not the landing page
        assert "landing" in j["via"]
        assert "checkpoint interval" in client.get(f"/page/{j['id']}").json["text"]
    # Both landing pages resolve to one stored page.
    assert client.get("/stats").json["pages"] == 1


def test_ingest_url_news_plus_exclusive_keeps_title(client, site):
    r = client.post("/ingest-url", json={"url": site + "/landing-exclusive", "source": "news"}, headers=HEADERS)
    assert r.status_code == 200, r.data
    page = client.get(f"/page/{r.json['id']}").json
    assert page["title"] == "Inside the Watch Workshop — Hodinkee"
    assert page["description"].startswith("apple news · ")
    assert "No article text could be fetched" in page["text"]
    assert client.get("/search?q=watch workshop").json["results"]
    assert client.get("/search?q=apple news").json["results"]


def test_ingest_url_errors(client, site):
    assert client.post("/ingest-url", json={"url": "notaurl"}, headers=HEADERS).status_code == 400
    assert client.post("/ingest-url", json={"url": site + "/file.pdf"}, headers=HEADERS).status_code == 502
    assert client.post("/ingest-url", json={"url": site + "/boom"}, headers=HEADERS).status_code == 502
    assert client.post("/ingest-url", json={"url": site + "/wal", "source": "bogus"}, headers=HEADERS).status_code == 400
    assert client.get("/stats").json["pages"] == 0
