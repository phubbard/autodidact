import hashlib
import os
import re
import sys

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import vectors  # noqa: E402
from app import create_app  # noqa: E402

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
FILLER = " The rest of the post covers prices, part numbers and the dealer visit in some detail." * 8


class FakeEncoder:
    """Bag of words hashed into 384 dims, with a few synonyms so 'meaning' differs from 'words'."""
    SYNONYMS = {"lighting": "led", "lights": "led", "blinkers": "signals", "forum": "reddit", "bike": "motorcycle"}

    def encode(self, texts):
        out = np.zeros((len(texts), vectors.DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                w = self.SYNONYMS.get(w, w)
                out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % vectors.DIM] += 1
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-9)


@pytest.fixture
def env(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), token=TOKEN)
    app.config.update(TESTING=True, ENCODER=FakeEncoder())
    with app.test_client() as c:
        yield c, db.connect(str(tmp_path / "t.db"))


def ingest(client, url, title, text, **extra):
    r = client.post("/ingest", json={"url": url, "title": title, "text": text + FILLER, **extra}, headers=HEADERS)
    assert r.status_code == 200, r.data
    return r.json["id"]


def test_chunking():
    assert vectors.chunks("T", "D", "") == ["T D"]
    long = " ".join(f"w{i}" for i in range(vectors.CHUNK_WORDS * 20))
    parts = vectors.chunks("Title", "", long)
    assert len(parts) == vectors.MAX_CHUNKS and parts[0].startswith("Title\nw0 ") and parts[1].startswith(f"w{vectors.CHUNK_WORDS} ")


def test_vectorize_is_incremental_and_follows_summaries(env):
    client, conn = env
    pid = ingest(client, "https://old.reddit.com/r/Zero/1", "My SR/S mods part 6", "OEM LED turn signals with smoked lenses.")
    enc = FakeEncoder()
    n1 = vectors.vectorize(conn, enc)
    assert n1 >= 1 and vectors.vectorize(conn, enc) == 0          # nothing left to do
    conn.execute("UPDATE pages SET summary = 'Brighter factory LED blinkers.' WHERE id = ?", (pid,))
    conn.commit()
    assert vectors.vectorize(conn, enc) == 1                        # just the summary vector
    chunks = [r[0] for r in conn.execute("SELECT chunk FROM chunk_vectors WHERE page_id = ? ORDER BY chunk", (pid,))]
    assert chunks[0] == vectors.SUMMARY_CHUNK and 0 in chunks
    client.delete(f"/page/{pid}", headers=HEADERS)                  # vectors go with the page
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0


def test_hybrid_finds_meaning_keyword_search_misses(env):
    client, conn = env
    target = ingest(client, "https://old.reddit.com/r/ZeroMotorcycles/comments/x3gehn/part6",
                    "My SR/S mods part 6 - OEM Zero LED turn signals", "Smoked lens LED signals, brighter and slimmer.")
    ingest(client, "https://www.youtube.com/watch?v=1", "Zero SR/S LA Twisties", "Riding the Zero SR/S in the canyons.")
    ingest(client, "https://example.com/cooking", "Sausage recipes", "How to make sausage at home.")
    vectors.vectorize(conn, FakeEncoder())
    # Neither word appears in any page, so keyword search finds nothing; the meaning still matches.
    assert client.get("/search?q=lighting blinkers").json["results"][0]["id"] == target
    j = client.get("/search?q=lighting blinkers").json
    assert j["mode"] == "hybrid" and j["results"][0]["match"] == "semantic"
    assert j["results"][0]["similarity"] > 0
    # Vector-only hits get a readable snippet from the matching chunk.
    assert all(r["snippet"] for r in j["results"])


def test_filters_apply_to_semantic_hits(env):
    client, conn = env
    ingest(client, "https://old.reddit.com/r/Zero/1", "LED turn signals", "Factory LED signals.")
    ingest(client, "https://example.com/led", "LED strip lighting", "LED strips for the kitchen.")
    vectors.vectorize(conn, FakeEncoder())
    j = client.get("/search?q=lighting&domain=reddit.com").json
    assert {r["domain"] for r in j["results"]} == {"old.reddit.com"}


def test_pagination_and_keyword_fallbacks(env, tmp_path):
    client, conn = env
    for i in range(30):
        ingest(client, f"https://example.com/{i}", f"Motorcycle page {i}", f"Notes about motorcycle {i}.")
    vectors.vectorize(conn, FakeEncoder())
    first = client.get("/search?q=motorcycle&limit=10").json["results"]
    second = client.get("/search?q=motorcycle&limit=10&offset=10").json["results"]
    assert len(first) == len(second) == 10 and not {r["id"] for r in first} & {r["id"] for r in second}
    assert client.get("/search?q=mo").json["mode"] in ("all", "any")   # too short for semantic
    # No model: plain keyword search, as before.
    app2 = create_app(db_path=str(tmp_path / "t.db"), token=TOKEN)
    app2.config["MODEL_DIR"] = str(tmp_path / "nope")
    assert app2.test_client().get("/search?q=motorcycle").json["mode"] == "all"


def test_offload_round_trip(env, tmp_path, monkeypatch):
    """export on the Pi, encode elsewhere, import: same rows as vectorizing in place."""
    import json as _json
    import subprocess
    client, conn = env
    pid = ingest(client, "https://example.com/a", "LED turn signals", "Factory LED signals.")
    work = vectors.pending_work(conn, 10)
    enc = FakeEncoder()
    encoded = list(vectors.encode_work(enc, work))
    vectors.store(conn, [(p, c, v.tobytes()) for p, c, v in encoded])   # what --import does with decoded bytes
    assert vectors.pending_work(conn, 10) == []
    stored = conn.execute("SELECT vector FROM chunk_vectors WHERE page_id = ? AND chunk = 0", (pid,)).fetchone()[0]
    assert np.allclose(np.frombuffer(stored, dtype=np.float32), enc.encode([work[0][2]])[0])
    # A vector for a page deleted in the meantime is skipped, not orphaned.
    vectors.store(conn, [(99999, 0, encoded[0][2])])
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors WHERE page_id = 99999").fetchone()[0] == 0


def test_related_pages(env):
    client, conn = env
    a = ingest(client, "https://old.reddit.com/r/Zero/1", "LED turn signals", "Factory LED signals for the Zero.")
    b = ingest(client, "https://old.reddit.com/r/Zero/2", "LED blinkers", "Smoked LED signals, brighter lighting.")
    a2 = ingest(client, "https://old.reddit.com/r/Zero/1", "LED turn signals", "Factory LED signals for the Zero, edited.")
    ingest(client, "https://example.com/cooking", "Sausage recipes", "How to make sausage at home.")
    vectors.vectorize(conn, FakeEncoder())
    rel = client.get(f"/page/{a}/related").json["related"]
    ids = [r["id"] for r in rel]
    assert ids[0] == b and a2 not in ids and a not in ids        # other versions of the same URL are left out
    assert rel[0]["similarity"] > rel[-1]["similarity"]
    html = client.get(f"/page/{a}?format=html").data.decode()
    assert "Related" in html and "LED blinkers" in html
    assert client.get("/page/99999/related").status_code == 404


def test_ask_answers_from_sources(env):
    client, conn = env
    target = ingest(client, "https://old.reddit.com/r/ZeroMotorcycles/part6", "My SR/S mods part 6 - OEM Zero LED turn signals",
                    "Part number 41-08273 for the smoked LED signals; the dealer changes a setting.")
    ingest(client, "https://example.com/cooking", "Sausage recipes", "How to make sausage at home.")
    vectors.vectorize(conn, FakeEncoder())
    seen = {}

    def fake_llm(messages, timeout):
        seen["messages"] = messages
        return "The part number is 41-08273 【1†L2-L3】.", "fake-model"
    client.application.config["LLM"] = fake_llm
    j = client.get("/ask?q=part number for the LED blinkers").json
    assert j["answer"] == "The part number is 41-08273 [1]." and j["model"] == "fake-model"
    assert j["sources"][0]["id"] == target and j["sources"][0]["n"] == 1
    prompt = seen["messages"][1]["content"]
    assert "[1] My SR/S mods part 6" in prompt and "41-08273" in prompt and "Question: part number" in prompt


def test_ask_without_llm_returns_sources(env, monkeypatch):
    client, conn = env
    ingest(client, "https://example.com/led", "LED turn signals", "Factory LED signals.")
    vectors.vectorize(conn, FakeEncoder())

    def down(messages, timeout):
        raise TimeoutError("timed out")
    client.application.config["LLM"] = down
    j = client.get("/ask?q=LED signals").json
    assert j["answer"] is None and j["sources"] and "did not answer (TimeoutError)" in j["error"]
    client.application.config["LLM"] = None
    monkeypatch.delenv("AUTODIDACT_LLM_URL", raising=False)
    assert "No LLM configured" in client.get("/ask?q=LED signals").json["error"]
    assert client.get("/ask?q=a").status_code == 400


def test_ask_empty_archive(tmp_path):
    app = create_app(db_path=str(tmp_path / "empty.db"), token=TOKEN)
    app.config.update(TESTING=True, ENCODER=FakeEncoder())
    assert app.test_client().get("/ask?q=anything at all").json["error"].startswith("Nothing")
