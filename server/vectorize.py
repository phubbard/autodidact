"""Embed pages for semantic search with the local gte-small model (see vectors.py). From cron on the Pi:

    AUTODIDACT_DB=/srv/autodidact/data/autodidact.db python vectorize.py

Embeds pages that have no vectors yet, and summaries Axiom has written since; exits quickly when
there is nothing to do. The model lives in <db dir>/models/gte-small unless AUTODIDACT_MODEL_DIR says otherwise.

A large backlog (first install, or re-embedding after a model change) is slow on the Pi's CPU, about
one 300-word chunk a second. Offload it to a faster machine with the same model files and code:

    python vectorize.py --export work.jsonl            # on the Pi: what still needs embedding
    python vectorize.py --encode work.jsonl vecs.jsonl --model-dir ~/models/gte-small   # anywhere
    python vectorize.py --import vecs.jsonl            # on the Pi

Use the same model.onnx and tokenizer.json files on both machines (copy them from the Pi), never a
different build of "gte-small": query vectors are made on the Pi and must be comparable.
"""

import argparse
import base64
import json
import os
import sys
import time

import db
import vectors


def model_dir(db_path: str) -> str:
    return os.environ.get("AUTODIDACT_MODEL_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(db_path)), "models", vectors.MODEL)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="stop after this many pages (0 = until done)")
    ap.add_argument("--export", metavar="WORK", help="write all pending work as JSONL and exit")
    ap.add_argument("--encode", nargs=2, metavar=("WORK", "VECTORS"), help="embed a work file (no database needed)")
    ap.add_argument("--import", dest="import_", metavar="VECTORS", help="store an encoded vectors file")
    ap.add_argument("--model-dir", help="for --encode: where model.onnx and tokenizer.json are")
    ap.add_argument("--threads", type=int, default=2, help="CPU threads for the model (2 suits the Pi)")
    args = ap.parse_args()
    db_path = os.environ.get("AUTODIDACT_DB", "autodidact.db")

    if args.export:
        work = vectors.pending_work(db.connect(db_path), limit=10**9)
        with open(args.export, "w") as f:
            for pid, ch, text in work:
                f.write(json.dumps({"page_id": pid, "chunk": ch, "text": text}) + "\n")
        print(f"exported {len(work)} chunks from {len({w[0] for w in work})} pages to {args.export}")
        return
    if args.import_:
        conn, n, buf = db.connect(db_path), 0, []
        with open(args.import_) as f:
            for line in f:
                d = json.loads(line)
                buf.append((d["page_id"], d["chunk"], base64.b64decode(d["vector"])))
                if len(buf) == 500:
                    n += vectors.store(conn, buf)
                    buf = []
        n += vectors.store(conn, buf) if buf else 0
        print(f"imported {n} vectors")
        return

    encoder = vectors.load_encoder(args.model_dir or model_dir(db_path), args.threads)
    if encoder is None:
        sys.exit(f"no encoder: install numpy, onnxruntime and tokenizers, and put {list(vectors.MODEL_FILES)} "
                 f"in {args.model_dir or model_dir(db_path)}")
    if args.encode:
        src, dst = args.encode
        work = [json.loads(line) for line in open(src)]
        if os.path.exists(dst):  # resume: skip chunks already encoded
            done = {(d["page_id"], d["chunk"]) for d in map(json.loads, open(dst))}
            work = [w for w in work if (w["page_id"], w["chunk"]) not in done]
            print(f"resuming: {len(done)} already encoded, {len(work)} to go", flush=True)
        t0 = time.time()
        with open(dst, "a") as f:
            for i, (pid, ch, v) in enumerate(vectors.encode_work(encoder, [(w["page_id"], w["chunk"], w["text"]) for w in work], 16)):
                f.write(json.dumps({"page_id": pid, "chunk": ch, "vector": base64.b64encode(v.tobytes()).decode()}) + "\n")
                if (i + 1) % 1000 == 0:
                    print(f"{i + 1}/{len(work)} chunks, {time.time() - t0:.0f}s", flush=True)
        print(f"encoded {len(work)} chunks in {time.time() - t0:.0f}s to {dst}")
        return
    conn = db.connect(db_path)
    total, t0 = 0, time.time()
    while True:
        step = 200 if not args.limit else min(200, args.limit - total)
        n = vectors.vectorize(conn, encoder, limit=step)
        if not n:
            break
        total += step
        print(f"{n} vectors, {total} pages so far, {time.time() - t0:.0f}s", flush=True)
        if args.limit and total >= args.limit:
            break
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
