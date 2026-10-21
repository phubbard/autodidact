import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import transcripts  # noqa: E402
from app import create_app  # noqa: E402

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
COMMENTS = "Great video! Thanks for sharing. First! " * 10


def setup(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), token=TOKEN)
    app.config["TESTING"] = True
    c = app.test_client()

    def ingest(url, text=COMMENTS, title="Zero SR/S review - YouTube"):
        return c.post("/ingest", json={"url": url, "title": title, "text": text}, headers=HEADERS).json["id"]
    return c, ingest, db.connect(str(tmp_path / "t.db"))


def test_video_id():
    assert transcripts.video_id("https://youtube.com/watch?v=abc123&t=10") == "abc123"
    assert transcripts.video_id("https://youtu.be/abc123") == "abc123"
    assert transcripts.video_id("https://www.youtube.com/live/xyz") == "xyz"
    assert transcripts.video_id("https://youtube.com/@channel") is None
    assert transcripts.video_id("https://example.com/watch?v=abc") is None


def test_transcript_prepended_and_page_requeued(tmp_path):
    client, ingest, conn = setup(tmp_path)
    a = ingest("https://www.youtube.com/watch?v=vid1")
    a2 = ingest("https://www.youtube.com/watch?v=vid1", text=COMMENTS + " More comments.")   # second version
    b = ingest("https://www.youtube.com/watch?v=vid2")
    ingest("https://example.com/not-youtube")
    conn.execute("UPDATE pages SET summary = 'old summary' WHERE id = ?", (a,))
    conn.execute("INSERT INTO chunk_vectors (page_id, chunk, model, vector) VALUES (?, 0, 'gte-small', x'00')", (a,))
    conn.commit()
    asked = []

    def fake(vid):
        asked.append(vid)
        if vid == "vid2":
            raise transcripts.NoTranscript("TranscriptsDisabled")
        return "today we ride the zero sr slash s through the canyon", "en", True

    counts = transcripts.run(conn, limit=10, fetch=fake, pause=0)
    assert counts == {"ok": 1, "none": 1, "stopped": None}
    assert sorted(asked) == ["vid1", "vid2"]                       # one fetch per video, not per page
    for pid in (a, a2):
        page = client.get(f"/page/{pid}").json
        assert page["text"].startswith("Transcript (en, auto-generated):\ntoday we ride")
        assert page["summary"] is None
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors WHERE page_id = ?", (a,)).fetchone()[0] == 0
    # Searchable by what was said, through the FTS update trigger.
    assert {r["id"] for r in client.get("/search?q=canyon").json["results"]} == {a, a2}
    # Recorded, so neither video is asked again.
    assert transcripts.run(conn, limit=10, fetch=fake, pause=0) == {"ok": 0, "none": 0, "stopped": None}
    assert conn.execute("SELECT status FROM transcripts WHERE page_id = ?", (b,)).fetchone()[0] == "none"
    # A later capture of the same page (without the transcript) still dedups against the stored row.
    r = client.post("/ingest", json={"url": "https://www.youtube.com/watch?v=vid1", "title": "t", "text": COMMENTS},
                    headers=HEADERS).json
    assert r["id"] == a and not r["created"]
    # A new version captured later reuses the stored transcript without asking YouTube again.
    a3 = ingest("https://www.youtube.com/watch?v=vid1", text=COMMENTS + " Even more comments.")
    asked.clear()
    assert transcripts.run(conn, limit=10, fetch=fake, pause=0)["ok"] == 1 and asked == []
    assert client.get(f"/page/{a3}").json["text"].startswith("Transcript (en, auto-generated):\ntoday we ride")


def test_blocked_stops_the_run(tmp_path):
    _, ingest, conn = setup(tmp_path)
    ingest("https://www.youtube.com/watch?v=v1")
    ingest("https://www.youtube.com/watch?v=v2")

    def blocked(vid):
        raise transcripts.TryLater("IpBlocked")
    counts = transcripts.run(conn, limit=10, fetch=blocked, pause=0)
    assert counts["stopped"] == "IpBlocked" and counts["ok"] == 0
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0   # nothing recorded; retried later
