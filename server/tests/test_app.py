import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
from app import create_app, fts_query  # noqa: E402

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
LOREM = ("SQLite write-ahead logging, or WAL mode, changes how durability works. Instead of a rollback "
         "journal, changes are appended to a separate WAL file and checkpointed into the main database "
         "later. Readers do not block writers and writers do not block readers. ") * 3


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), token=TOKEN)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def capture(url, text=LOREM, **extra):
    body = {"url": url, "title": "WAL mode explained", "text": text, "visit_id": extra.pop("visit_id", None),
            "dwell_s": 9, "captured_at": int(time.time()), "browser": "Mozilla/5.0 Chrome/128"}
    body.update(extra)
    return body


# ---------- URL normalisation ----------

@pytest.mark.parametrize("a,b", [
    ("https://www.example.com/post/?utm_source=x&utm_medium=y", "https://example.com/post"),
    ("https://example.com/post#section-2", "https://example.com/post"),
    ("HTTPS://Example.COM:443/post", "https://example.com/post"),
    ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
    ("https://example.com/", "https://example.com/"),
    ("http://example.com:8080/x/", "http://example.com:8080/x"),
])
def test_normalize_url(a, b):
    assert db.normalize_url(a) == b


def test_fts_query_is_safe():
    assert fts_query('sqlite "wal mode" OR NOT (') == '"sqlite" AND "wal mode" AND "OR" AND "NOT"*'
    assert fts_query("durability") == '"durability"*'
    assert fts_query("") == ""
    assert fts_query("a b", "OR") == '"a" OR "b"'


# ---------- ingest ----------

def test_ingest_requires_token(client):
    r = client.post("/ingest", json=capture("https://example.com/p"))
    assert r.status_code == 401
    r = client.post("/ingest", json=capture("https://example.com/p"), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_ingest_rejects_short_text(client):
    r = client.post("/ingest", json=capture("https://example.com/p", text="too short"), headers=HEADERS)
    assert r.status_code == 400


def test_ingest_creates_then_dedups(client):
    r1 = client.post("/ingest", json=capture("https://example.com/p?utm_source=a", visit_id="v1"), headers=HEADERS)
    assert r1.status_code == 200 and r1.json["created"] and r1.json["new_visit"]
    r2 = client.post("/ingest", json=capture("https://www.example.com/p/#top", visit_id="v2"), headers=HEADERS)
    assert r2.json["id"] == r1.json["id"]
    assert not r2.json["created"] and r2.json["new_visit"]
    # Retry of the same visit (queue replay) does not add a visit.
    r3 = client.post("/ingest", json=capture("https://example.com/p", visit_id="v2"), headers=HEADERS)
    assert r3.json["id"] == r1.json["id"] and not r3.json["new_visit"]

    page = client.get(f"/page/{r1.json['id']}").json
    assert page["visit_count"] == 2
    assert page["total_dwell_s"] == 18
    assert page["url"] == "https://example.com/p"


def test_changed_content_is_new_row(client):
    r1 = client.post("/ingest", json=capture("https://example.com/p"), headers=HEADERS)
    r2 = client.post("/ingest", json=capture("https://example.com/p", text=LOREM + " Updated paragraph."), headers=HEADERS)
    assert r2.json["created"] and r2.json["id"] != r1.json["id"]


def test_canonical_url_same_domain_only(client):
    r = client.post("/ingest", json=capture("https://example.com/p?x=1", canonical_url="https://example.com/p"),
                    headers=HEADERS)
    assert client.get(f"/page/{r.json['id']}").json["url"] == "https://example.com/p"
    r = client.post("/ingest", json=capture("https://other.com/q", canonical_url="https://example.com/p"),
                    headers=HEADERS)
    assert client.get(f"/page/{r.json['id']}").json["url"] == "https://other.com/q"


def test_dwell_update(client):
    r = client.post("/ingest", json=capture("https://example.com/p", visit_id="v9"), headers=HEADERS)
    assert client.post("/dwell", json={"visit_id": "v9", "dwell_s": 120}, headers=HEADERS).status_code == 200
    assert client.get(f"/page/{r.json['id']}").json["total_dwell_s"] == 120
    # Lower value never decreases it.
    client.post("/dwell", json={"visit_id": "v9", "dwell_s": 5}, headers=HEADERS)
    assert client.get(f"/page/{r.json['id']}").json["total_dwell_s"] == 120
    assert client.post("/dwell", json={"visit_id": "nope", "dwell_s": 5}, headers=HEADERS).status_code == 404


# ---------- search ----------

def test_search_keyword_and_ranking(client):
    client.post("/ingest", json=capture("https://a.com/wal", title="WAL mode explained"), headers=HEADERS)
    client.post("/ingest", json=capture("https://b.com/cats", title="Cats", text="Cats are small carnivorous mammals. " * 20),
                headers=HEADERS)
    r = client.get("/search?q=checkpoint").json
    assert r["mode"] == "all"
    assert [h["domain"] for h in r["results"]] == ["a.com"]
    assert "<mark>" in r["results"][0]["snippet"]
    # Porter stemming: "readers" matches "reader"; "durable" matches "durability".
    assert client.get("/search?q=reader").json["results"]
    assert client.get("/search?q=durable").json["results"]
    # Title matches count.
    assert client.get("/search?q=explained").json["results"][0]["domain"] == "a.com"


def test_search_falls_back_to_any_word(client):
    client.post("/ingest", json=capture("https://a.com/wal"), headers=HEADERS)
    r = client.get("/search?q=checkpoint zebra").json
    assert r["mode"] == "any" and len(r["results"]) == 1
    assert client.get("/search?q=zebra giraffe").json["results"] == []


def test_search_operators_do_not_break(client):
    client.post("/ingest", json=capture("https://a.com/wal"), headers=HEADERS)
    for q in ['"', "AND", "NOT wal", "(wal", "wal*", "-wal", "wal OR", 'wal " mode']:
        assert client.get("/search", query_string={"q": q}).status_code == 200


def test_search_time_and_domain_filters(client):
    old = int(time.mktime((2026, 3, 1, 12, 0, 0, 0, 0, -1)))
    new = int(time.mktime((2026, 9, 1, 12, 0, 0, 0, 0, -1)))
    client.post("/ingest", json=capture("https://old.com/wal", captured_at=old), headers=HEADERS)
    client.post("/ingest", json=capture("https://new.com/wal", captured_at=new), headers=HEADERS)
    q = "/search?q=wal"
    assert {h["domain"] for h in client.get(q).json["results"]} == {"old.com", "new.com"}
    assert [h["domain"] for h in client.get(q + "&after=2026-06-01").json["results"]] == ["new.com"]
    assert [h["domain"] for h in client.get(q + "&before=2026-06-01").json["results"]] == ["old.com"]
    assert [h["domain"] for h in client.get(q + "&after=2026-02-01&before=2026-04-01").json["results"]] == ["old.com"]
    assert [h["domain"] for h in client.get(q + "&domain=new.com").json["results"]] == ["new.com"]
    # Recent listing without q, newest first.
    assert [h["domain"] for h in client.get("/search").json["results"]] == ["new.com", "old.com"]


def test_delete_page_and_domain(client):
    r = client.post("/ingest", json=capture("https://a.com/wal"), headers=HEADERS)
    client.post("/ingest", json=capture("https://sub.a.com/x"), headers=HEADERS)
    assert client.delete(f"/page/{r.json['id']}").status_code == 401
    assert client.delete(f"/page/{r.json['id']}", headers=HEADERS).status_code == 200
    assert client.get(f"/page/{r.json['id']}").status_code == 404
    assert client.get("/search?q=wal").json["results"][0]["domain"] == "sub.a.com"
    assert client.delete("/domain/a.com", headers=HEADERS).json["deleted"] == 1
    assert client.get("/search?q=wal").json["results"] == []
    assert client.get("/stats").json["pages"] == 0


def test_index_and_page_html(client):
    r = client.post("/ingest", json=capture("https://a.com/wal"), headers=HEADERS)
    assert client.get("/").status_code == 200
    html = client.get(f"/page/{r.json['id']}?format=html").data.decode()
    assert "WAL mode explained" in html and "checkpointed" in html
