"""Milestone 2 enrichment job: summaries, tags, and embeddings via a local
OpenAI-compatible endpoint (LM Studio on Axiom).

Run from cron on the Pi, e.g. every 15 minutes:

    AUTODIDACT_DB=/srv/autodidact/autodidact.db \
    AUTODIDACT_LLM_URL=http://axiom.phfactor.net:1234/v1 \
    python3 embed.py --batch 50

Summaries and tags go straight into the FTS index (through the pages_au
trigger), so keyword search benefits even before semantic search exists.
Embeddings are stored as float32 blobs for the later semantic layer.
Idempotent: only rows with summary IS NULL (or no embedding) are touched, and
a failed batch just gets retried next run.
"""

import argparse
import array
import json
import os
import sys
import urllib.request

import db

LLM_URL = os.environ.get("AUTODIDACT_LLM_URL", "http://localhost:1234/v1")
CHAT_MODEL = os.environ.get("AUTODIDACT_CHAT_MODEL", "")       # empty = whatever LM Studio has loaded
EMBED_MODEL = os.environ.get("AUTODIDACT_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
MAX_INPUT_CHARS = 12_000

PROMPT = """You are indexing a web page someone read so they can find it again months from now.
Write a two-sentence summary of what the page is about and what is distinctive about it, then
give five short lowercase tags (topics, technologies, names). Reply with JSON only:
{"summary": "...", "tags": ["...", "...", "...", "...", "..."]}

Title: {title}
URL: {url}

{text}"""


def _post(path, payload, timeout=120):
    req = urllib.request.Request(
        LLM_URL.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_model_cache = {}


def default_model():
    """First loaded non-embedding model, the way the `ask` zsh function does it."""
    if "chat" not in _model_cache:
        with urllib.request.urlopen(LLM_URL.rstrip("/") + "/models", timeout=10) as r:
            data = json.load(r)["data"]
        chat = [m["id"] for m in data if "embed" not in m["id"].lower()]
        _model_cache["chat"] = chat[0] if chat else None
    return _model_cache["chat"]


def summarize(page):
    model = CHAT_MODEL or default_model()
    prompt = PROMPT.format(title=page["title"] or "", url=page["url"], text=page["text"][:MAX_INPUT_CHARS])
    resp = _post("/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 300,
    })
    content = resp["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`").split("\n", 1)[1] if "\n" in content else content.strip("`")
        content = content.rsplit("```", 1)[0]
    start, end = content.find("{"), content.rfind("}")
    data = json.loads(content[start:end + 1])
    tags = [str(t).strip().lower() for t in data.get("tags", [])][:8]
    return str(data.get("summary", "")).strip(), tags


def embed(page):
    text = f"{page['title'] or ''}\n{page['summary'] or ''}\n{page['text'][:MAX_INPUT_CHARS]}"
    resp = _post("/embeddings", {"model": EMBED_MODEL, "input": text})
    vec = resp["data"][0]["embedding"]
    return array.array("f", vec).tobytes(), len(vec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--no-embed", action="store_true", help="only summaries and tags")
    ap.add_argument("--no-summary", action="store_true", help="only embeddings")
    args = ap.parse_args()

    conn = db.connect(os.environ.get("AUTODIDACT_DB", "autodidact.db"))
    done = 0

    if not args.no_summary:
        rows = conn.execute(
            "SELECT id, url, title, text FROM pages WHERE summary IS NULL ORDER BY last_seen DESC LIMIT ?",
            (args.batch,),
        ).fetchall()
        for row in rows:
            try:
                summary, tags = summarize(row)
            except Exception as e:  # noqa: BLE001
                print(f"summary failed for {row['id']} {row['url']}: {e}", file=sys.stderr)
                continue
            conn.execute("UPDATE pages SET summary = ?, tags = ? WHERE id = ?",
                         (summary, json.dumps(tags), row["id"]))
            conn.commit()
            done += 1
            print(f"summarized {row['id']} {row['url'][:80]}")

    if not args.no_embed:
        rows = conn.execute(
            """SELECT p.id, p.url, p.title, p.summary, p.text FROM pages p
               LEFT JOIN embeddings e ON e.page_id = p.id
               WHERE e.page_id IS NULL ORDER BY p.last_seen DESC LIMIT ?""",
            (args.batch,),
        ).fetchall()
        for row in rows:
            try:
                blob, dim = embed(row)
            except Exception as e:  # noqa: BLE001
                print(f"embedding failed for {row['id']} {row['url']}: {e}", file=sys.stderr)
                continue
            conn.execute("INSERT OR REPLACE INTO embeddings (page_id, model, dim, vector) VALUES (?, ?, ?, ?)",
                         (row["id"], EMBED_MODEL, dim, blob))
            conn.commit()
            done += 1
            print(f"embedded {row['id']} ({dim}d)")

    print(f"done: {done} updates")


if __name__ == "__main__":
    main()
