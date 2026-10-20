import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import embed  # noqa: E402

TEXT = "Write-ahead logging changes how durability works in SQLite. " * 300


def setup(tmp_path, monkeypatch, n=3):
    path = str(tmp_path / "t.db")
    conn = db.connect(path)
    for i in range(n):
        conn.execute("INSERT INTO pages (url, url_hash, content_hash, title, text, domain, first_seen, last_seen)"
                     " VALUES (?, ?, ?, 't', ?, 'e.com', ?, ?)", (f"https://e.com/{i}", f"u{i}", f"c{i}", TEXT, i, i))
    conn.commit()
    monkeypatch.setenv("AUTODIDACT_DB", path)
    monkeypatch.setattr(sys, "argv", ["embed.py", "--batch", "10"])
    return conn


def reply(summary="A page.", tags=("a", "b")):
    return {"choices": [{"message": {"content": json.dumps({"summary": summary, "tags": list(tags)})}}]}


def http_error(code):
    return urllib.error.HTTPError("http://axiom/v1/chat/completions", code, "err", {}, io.BytesIO(b""))


def test_prompt_formats(monkeypatch):
    # The JSON example in PROMPT has braces; str.format used to raise KeyError on it.
    seen = {}
    monkeypatch.setattr(embed, "CHAT_MODEL", "m")
    monkeypatch.setattr(embed, "_post", lambda path, payload, timeout=120: seen.update(payload) or reply())
    assert embed.summarize({"title": "T", "url": "https://e.com", "text": "x"}) == ("A page.", ["a", "b"])
    assert '{"summary": "...", "tags"' in seen["messages"][0]["content"]


def test_400_retries_shorter_then_marks_done(tmp_path, monkeypatch):
    conn = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(embed, "CHAT_MODEL", "m")
    calls = []

    def post(path, payload, timeout=120):
        text = payload["messages"][0]["content"]
        calls.append(len(text))
        if "https://e.com/2" in text:            # newest page: always rejected
            raise http_error(400)
        if "https://e.com/1" in text and len(text) > 5000:   # rejected until shortened
            raise http_error(400)
        return reply()
    monkeypatch.setattr(embed, "_post", post)
    embed.main()
    rows = dict(conn.execute("SELECT url, summary FROM pages").fetchall())
    assert rows == {"https://e.com/0": "A page.", "https://e.com/1": "A page.", "https://e.com/2": ""}


def test_server_down_or_5xx_stops_the_run(tmp_path, monkeypatch):
    conn = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(embed, "CHAT_MODEL", "m")
    for exc in (urllib.error.URLError("connection refused"), http_error(503)):
        monkeypatch.setattr(embed, "_post", lambda *a, exc=exc, **k: (_ for _ in ()).throw(exc))
        embed.main()
        assert conn.execute("SELECT COUNT(*) FROM pages WHERE summary IS NOT NULL").fetchone()[0] == 0
