"""Enrichment job: an LLM summary and tags for each page, from LM Studio on Axiom (OpenAI-compatible).

Run from cron on the Pi (install.sh adds it when AUTODIDACT_LLM_URL is set; a flock keeps runs apart):

    AUTODIDACT_DB=/srv/autodidact/data/autodidact.db \
    AUTODIDACT_LLM_URL=http://axiom.phfactor.net:1234/v1 AUTODIDACT_CHAT_MODEL=openai/gpt-oss-120b \
    python3 embed.py --batch 60

Summaries and tags go straight into the FTS index (through the pages_au trigger), and vectorize.py
embeds each new summary for semantic search. Page vectors are made locally on the Pi (vectors.py), not
here, so search never waits on Axiom. Idempotent: only rows with summary IS NULL are touched, and a
page that fails is retried next run. About 5-8 s a page with gpt-oss-120b.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import db

LLM_URL = os.environ.get("AUTODIDACT_LLM_URL", "http://localhost:1234/v1")
CHAT_MODEL = os.environ.get("AUTODIDACT_CHAT_MODEL", "")       # empty = whatever LM Studio has loaded
MAX_INPUT_CHARS = 12_000

PROMPT = """You are indexing a web page someone read so they can find it again months from now.
Write a two-sentence summary of what the page is about and what is distinctive about it, then
give five short lowercase tags (topics, technologies, names). Reply with JSON only:
{{"summary": "...", "tags": ["...", "...", "...", "...", "..."]}}

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


def summarize(page, max_chars=MAX_INPUT_CHARS):
    model = CHAT_MODEL or default_model()
    prompt = PROMPT.format(title=page["title"] or "", url=page["url"], text=page["text"][:max_chars])
    resp = _post("/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 600,  # gpt-oss reasons first; leave room for the answer
    })
    content = resp["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`").split("\n", 1)[1] if "\n" in content else content.strip("`")
        content = content.rsplit("```", 1)[0]
    start, end = content.find("{"), content.rfind("}")
    data = json.loads(content[start:end + 1])
    tags = [str(t).strip().lower() for t in data.get("tags", [])][:8]
    return str(data.get("summary", "")).strip(), tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50)
    args = ap.parse_args()

    conn = db.connect(os.environ.get("AUTODIDACT_DB", "autodidact.db"))
    done = 0

    rows = conn.execute(
        "SELECT id, url, title, text FROM pages WHERE summary IS NULL ORDER BY last_seen DESC LIMIT ?",
        (args.batch,),
    ).fetchall()
    for row in rows:
        try:
            try:
                summary, tags = summarize(row)
            except urllib.error.HTTPError as e:
                if not 400 <= e.code < 500:
                    raise
                # LM Studio rejected this request (seen: 400 Bad Request on one page, 2026-10-20),
                # usually something about the page's text. Try once with much less of it.
                print(f"HTTP {e.code} for {row['id']}; retrying with shorter text", file=sys.stderr)
                summary, tags = summarize(row, max_chars=3000)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                print(f"LLM server error, stopping: {e}", file=sys.stderr)
                break
            print(f"summary failed for {row['id']} {row['url']}: {e}", file=sys.stderr)
            summary, tags = "", []
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # Axiom asleep or LM Studio down: stop; the next run tries again.
            print(f"LLM unreachable, stopping: {e}", file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001 — an unusable answer for this page
            # Mark it done with an empty summary so one bad page cannot hold a batch slot forever.
            print(f"summary failed for {row['id']} {row['url']}: {e}", file=sys.stderr)
            summary, tags = "", []
        conn.execute("UPDATE pages SET summary = ?, tags = ? WHERE id = ?",
                     (summary, json.dumps(tags), row["id"]))
        conn.commit()
        done += 1
        print(f"summarized {row['id']} {row['url'][:80]}")

    print(f"done: {done} updates")


if __name__ == "__main__":
    main()
