import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module  # noqa: E402
import db  # noqa: E402
from app import create_app  # noqa: E402

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
TEXT = "Write-ahead logging changes how durability works in SQLite. " * 6
# Exactly what Waterfox sent on 2026-10-19: JSON whose text holds half an emoji, as an ASCII escape.
RAW = ('{"url": "https://example.com/emoji", "title": "t", "visit_id": "v1", "browser": "Firefox",'
       ' "text": "%s broken \\ud83d emoji"}' % TEXT).encode()


@pytest.fixture
def client(tmp_path):
    a = create_app(db_path=str(tmp_path / "t.db"), token=TOKEN)
    a.config["TESTING"] = True
    a.config["PROPAGATE_EXCEPTIONS"] = False
    with a.test_client() as c:
        yield c


@pytest.fixture
def broken_store(monkeypatch):
    """Make storing fail after the pages row is written, like the real bugs did."""
    real = app_module._refresh_page_stats

    def boom(conn, page_id):
        raise RuntimeError("simulated bug")
    monkeypatch.setattr(app_module, "_refresh_page_stats", boom)
    return lambda: monkeypatch.setattr(app_module, "_refresh_page_stats", real)


def test_unexpected_error_is_dead_lettered_and_rolled_back(client, broken_store):
    r = client.post("/ingest", data=RAW, content_type="application/json",
                    headers={**HEADERS, "User-Agent": "Mozilla/5.0 (Macintosh) Firefox/153.0", "X-Autodidact-Device": "Waterfox"})
    assert r.status_code == 202 and r.json["dead_lettered"] == 1 and "simulated bug" in r.json["error"]
    assert client.get("/stats").json["pages"] == 0, "half-written page must be rolled back"
    assert client.get("/stats").json["dead_letters"] == 1
    listed = client.get("/debug/dlq").json["dead_letters"]
    assert len(listed) == 1
    dl = listed[0]
    assert (dl["path"], dl["url"], dl["device"], dl["browser"]) == ("/ingest", "https://example.com/emoji", "Waterfox", "Firefox 153")
    assert dl["bytes"] == len(RAW) and "RuntimeError: simulated bug" in dl["error"]
    # The stored body is byte-for-byte what arrived.
    assert client.get("/debug/dlq/1?raw=1").data == RAW
    assert "Traceback" in client.get("/debug/dlq/1").json["traceback"]
    # The request log says what happened.
    note = [q for q in client.get("/debug/requests").json["requests"] if q["path"] == "/ingest"][0]["note"]
    assert note.startswith("dead-lettered #1")


def test_replay_after_fix(client, broken_store):
    client.post("/ingest", data=RAW, content_type="application/json", headers=HEADERS)
    assert client.post("/dlq/1/replay").status_code == 401          # admin only
    # Still broken: the replay fails, is reported, and does not queue a second copy.
    r = client.post("/dlq/1/replay", headers=HEADERS)
    assert r.status_code == 500 and not r.json["ok"]
    assert client.get("/stats").json["dead_letters"] == 1
    assert len(client.get("/debug/dlq?all=1").json["dead_letters"]) == 1
    broken_store()  # deploy the fix
    r = client.post("/dlq/1/replay", headers=HEADERS)
    assert r.status_code == 200 and r.json["ok"] and r.json["result"]["created"]
    assert client.get("/stats").json == {**client.get("/stats").json, "pages": 1, "dead_letters": 0}
    page = client.get(f"/page/{r.json['result']['id']}").json
    assert "broken � emoji" in page["text"] and page["visits"][0]["browser"] == "Firefox"
    assert client.post("/dlq/1/replay", headers=HEADERS).status_code == 409   # once only
    assert client.get("/debug/dlq").json["dead_letters"] == []                 # pending list is empty
    assert client.get("/debug/dlq?all=1").json["dead_letters"][0]["replayed_at"]


def test_replay_all_and_delete(client, broken_store):
    for i in range(3):
        client.post("/ingest", json={"url": f"https://example.com/{i}", "text": TEXT, "title": "t"}, headers=HEADERS)
    client.post("/dwell", json={"visit_id": "x", "dwell_s": 5}, headers=HEADERS)  # 404: a client error, not dead-lettered
    assert client.get("/stats").json["dead_letters"] == 3
    assert client.delete("/dlq/3").status_code == 401
    assert client.delete("/dlq/3", headers=HEADERS).json == {"deleted": 3}
    broken_store()
    r = client.post("/dlq/replay", headers=HEADERS).json
    assert (r["replayed"], r["failed"]) == (2, 0)
    assert client.get("/stats").json["pages"] == 2


def test_client_errors_are_not_dead_lettered(client):
    assert client.post("/ingest", json={"url": "nope", "text": TEXT}, headers=HEADERS).status_code == 400
    assert client.post("/ingest", json={"url": "https://e.com/x", "text": TEXT}).status_code == 401
    assert client.get("/stats").json["dead_letters"] == 0


def test_dwell_errors_are_dead_lettered_too(client, monkeypatch):
    client.post("/ingest", json={"url": "https://e.com/x", "text": TEXT, "visit_id": "v"}, headers=HEADERS)
    monkeypatch.setattr(app_module, "_refresh_page_stats", lambda c, p: 1 / 0)
    r = client.post("/dwell", json={"visit_id": "v", "dwell_s": 30}, headers=HEADERS)
    assert r.status_code == 202 and "ZeroDivisionError" in r.json["error"]
    assert client.get("/debug/dlq").json["dead_letters"][0]["path"] == "/dwell"


def test_dlq_write_failure_means_retry_later(client, broken_store, monkeypatch):
    # If even the dead letter cannot be written (disk full, DB locked), answer 503 so the client retries.
    monkeypatch.setattr(app_module.traceback, "format_exc", lambda: (_ for _ in ()).throw(OSError("disk full")))
    r = client.post("/ingest", json={"url": "https://e.com/y", "text": TEXT}, headers=HEADERS)
    assert r.status_code == 503
