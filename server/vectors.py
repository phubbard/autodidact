"""Local embeddings for semantic search: gte-small (384-d) via ONNX Runtime on the Pi.

Each page gets up to MAX_CHUNKS vectors over its text (the first prefixed with the title and
description) plus, once Axiom has written one, a vector of its LLM summary (chunk = SUMMARY_CHUNK).
vectorize.py fills chunk_vectors from cron; VectorIndex keeps them in memory per gunicorn worker and
answers nearest-page queries. Everything here is optional: without numpy/onnxruntime/tokenizers or
the model files, load_encoder() returns None and search stays keyword-only.

The query and the pages must be embedded by the same model and code path, so do not mix in vectors
from another runtime (e.g. LM Studio on Axiom), even for "the same" model.
"""

import os
import re
import threading
import time

try:  # optional dependencies, like trafilatura in extract.py
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
try:  # only the encoder needs these; the index and tests need just numpy
    import onnxruntime as ort
    from tokenizers import Tokenizer
except ImportError:  # pragma: no cover
    ort = Tokenizer = None

MODEL = "gte-small"
DIM = 384
MAX_TOKENS = 512
CHUNK_WORDS = 300          # ~400 tokens, inside gte-small's 512-token window
MAX_CHUNKS = 8             # long pages: the first ~2,400 words are indexed
SUMMARY_CHUNK = -1         # chunk number used for the LLM summary's vector
MODEL_FILES = {            # downloaded by deploy/install.sh into <data>/models/gte-small/
    "model.onnx": "https://huggingface.co/thenlper/gte-small/resolve/main/onnx/model.onnx",
    "tokenizer.json": "https://huggingface.co/thenlper/gte-small/resolve/main/tokenizer.json",
}


def available() -> bool:
    return np is not None and ort is not None and Tokenizer is not None


class Encoder:
    """gte-small: mean-pooled last hidden state, L2-normalised. encode() returns float32 (n, DIM)."""

    def __init__(self, model_dir: str, threads: int = 2):
        self.tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(os.path.join(model_dir, "model.onnx"), opts,
                                            providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts):
        enc = self.tokenizer.encode_batch(list(texts))
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]                      # (n, tokens, DIM)
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        return (pooled / np.linalg.norm(pooled, axis=1, keepdims=True)).astype(np.float32)


def load_encoder(model_dir: str, threads: int = 2):
    """An Encoder, or None when the packages or model files are missing (search stays keyword-only)."""
    if not available() or not all(os.path.exists(os.path.join(model_dir, f)) for f in MODEL_FILES):
        return None
    return Encoder(model_dir, threads)


_WS = re.compile(r"\s+")


def chunks(title: str, description: str, text: str) -> list[str]:
    words = _WS.sub(" ", text or "").strip().split(" ")
    head = " ".join(p for p in (title, description) if p).strip()
    out = []
    for i in range(0, max(len(words), 1), CHUNK_WORDS):
        body = " ".join(words[i:i + CHUNK_WORDS]).strip()
        out.append(f"{head}\n{body}".strip() if i == 0 else body)
        if len(out) == MAX_CHUNKS:
            break
    return [c for c in out if c]


def chunk_text(page, chunk: int) -> str:
    """The text a stored vector was made from (for snippets of vector-only hits)."""
    if chunk == SUMMARY_CHUNK:
        return page["summary"] or ""
    parts = chunks(page["title"] or "", page["description"] or "", page["text"] or "")
    return parts[chunk] if 0 <= chunk < len(parts) else ""


def pending(conn, limit: int):
    """Pages with no text vectors yet, or with a summary that has no vector yet."""
    return conn.execute(
        """SELECT p.id, p.title, p.description, p.text, p.summary,
                  EXISTS(SELECT 1 FROM chunk_vectors c WHERE c.page_id = p.id AND c.chunk >= 0) AS has_text,
                  EXISTS(SELECT 1 FROM chunk_vectors c WHERE c.page_id = p.id AND c.chunk = ?) AS has_summary
           FROM pages p
           WHERE NOT EXISTS(SELECT 1 FROM chunk_vectors c WHERE c.page_id = p.id AND c.chunk >= 0)
              OR (p.summary IS NOT NULL AND p.summary != ''
                  AND NOT EXISTS(SELECT 1 FROM chunk_vectors c WHERE c.page_id = p.id AND c.chunk = ?))
           ORDER BY p.id DESC LIMIT ?""", (SUMMARY_CHUNK, SUMMARY_CHUNK, limit)).fetchall()


def pending_work(conn, limit: int):
    """[(page_id, chunk, text)] still to embed for up to *limit* pending pages."""
    work = []
    for r in pending(conn, limit):
        if not r["has_text"]:
            work += [(r["id"], i, t) for i, t in enumerate(chunks(r["title"] or "", r["description"] or "", r["text"] or ""))]
        if r["summary"] and not r["has_summary"]:
            work.append((r["id"], SUMMARY_CHUNK, f"{r['title'] or ''}\n{r['summary']}"))
    return work


def encode_work(encoder, work, batch: int = 8):
    """Yield (page_id, chunk, vector) for *work*. Similar lengths are batched together, since every
    text in a batch is padded to the longest; small batches keep memory low on the Pi."""
    order = sorted(range(len(work)), key=lambda i: len(work[i][2]))
    for s in range(0, len(order), batch):
        part = [work[i] for i in order[s:s + batch]]
        for (pid, ch, _), v in zip(part, encoder.encode([t for _, _, t in part])):
            yield pid, ch, v


def store(conn, items):
    """Insert (page_id, chunk, vector) rows; vector is float32 (ndarray or bytes). Skips pages deleted meanwhile."""
    rows = [(pid, ch, MODEL, v if isinstance(v, bytes) else v.astype(np.float32).tobytes()) for pid, ch, v in items]
    conn.executemany("INSERT OR REPLACE INTO chunk_vectors (page_id, chunk, model, vector) "
                     "SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM pages WHERE id = ?)",
                     [(*r, r[0]) for r in rows])
    conn.commit()
    return len(rows)


def vectorize(conn, encoder, limit: int = 200, batch: int = 8) -> int:
    """Embed up to *limit* pending pages. Returns the number of vectors written."""
    work = pending_work(conn, limit)
    done, buf = 0, []
    for item in encode_work(encoder, work, batch):
        buf.append(item)
        if len(buf) >= 64:
            done += store(conn, buf)
            buf = []
    return done + (store(conn, buf) if buf else 0)


class VectorIndex:
    """All chunk vectors in one float32 matrix, reloaded when the table changes (checked every 30 s)."""

    CHECK_EVERY_S = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._stamp = None
        self._checked = 0.0
        self.page_ids = self.chunk_ids = self.matrix = None

    def refresh(self, conn):
        now = time.monotonic()
        if self.matrix is not None and now - self._checked < self.CHECK_EVERY_S:
            return
        with self._lock:
            self._checked = now
            stamp = tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM chunk_vectors WHERE model = ?",
                                       (MODEL,)).fetchone())
            if stamp == self._stamp and self.matrix is not None:
                return
            rows = conn.execute("SELECT page_id, chunk, vector FROM chunk_vectors WHERE model = ?", (MODEL,)).fetchall()
            self.page_ids = np.array([r[0] for r in rows], dtype=np.int64)
            self.chunk_ids = np.array([r[1] for r in rows], dtype=np.int64)
            self.matrix = (np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(-1, DIM)
                           if rows else np.zeros((0, DIM), dtype=np.float32))
            self._stamp = stamp

    def search(self, qvec, k: int = 100, allowed=None):
        """[(page_id, best_chunk, cosine)] for the k nearest pages, best first. *allowed*: optional set of page ids."""
        if self.matrix is None or not len(self.matrix):
            return []
        sims = self.matrix @ qvec
        if allowed is not None:
            sims = np.where(np.isin(self.page_ids, np.fromiter(allowed, dtype=np.int64)), sims, -np.inf)
        order = np.argsort(-sims)
        out, seen = [], set()
        for i in order:
            if not np.isfinite(sims[i]):
                break
            pid = int(self.page_ids[i])
            if pid in seen:
                continue
            seen.add(pid)
            out.append((pid, int(self.chunk_ids[i]), float(sims[i])))
            if len(out) == k:
                break
        return out
