"""Embed pages for semantic search with the local gte-small model (see vectors.py). Run from cron:

    AUTODIDACT_DB=/srv/autodidact/data/autodidact.db python vectorize.py

Embeds pages that have no vectors yet, and summaries Axiom has written since; exits quickly when
there is nothing to do. The model lives in <db dir>/models/gte-small unless AUTODIDACT_MODEL_DIR says otherwise.
"""

import argparse
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
    args = ap.parse_args()
    db_path = os.environ.get("AUTODIDACT_DB", "autodidact.db")
    encoder = vectors.load_encoder(model_dir(db_path))
    if encoder is None:
        sys.exit(f"no encoder: install numpy, onnxruntime and tokenizers, and put {list(vectors.MODEL_FILES)} "
                 f"in {model_dir(db_path)}")
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
