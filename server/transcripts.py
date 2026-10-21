"""Fetch YouTube transcripts for captured videos, so they are searchable by what was said.

From cron on the Pi (install.sh adds it, every 30 minutes under flock):

    AUTODIDACT_DB=/srv/autodidact/data/autodidact.db python transcripts.py --limit 40

What the extension captures from a watch page is the title, description and comments; the talk itself
is in the transcript. For each captured video without one, this fetches the English transcript (or
the original language when there is no English one), puts it at the top of every stored version of
that page's text, and clears the page's vectors and summary so vectorize.py and embed.py redo them.
The stored content_hash is left alone: it identifies what the extension captured, so a later capture
of the same page still dedups against it. Videos with no transcript are recorded and not retried; if
YouTube throttles or blocks (it does block cloud IPs; the Pi is on a home line), the run stops.
"""

import argparse
import os
import sys
import time
from urllib.parse import parse_qs, urlsplit

import db

try:  # optional, like trafilatura
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import _errors as yt_errors
except ImportError:  # pragma: no cover
    YouTubeTranscriptApi = yt_errors = None

MAX_TEXT_CHARS = 250_000   # same cap as /ingest
HEADER = "Transcript"      # stored text starts with "Transcript (en, auto-generated):"
PAUSE_S = 2.0              # between videos, to stay polite


class NoTranscript(Exception):
    """This video has no usable transcript; don't ask again."""


class TryLater(Exception):
    """YouTube refused or failed; stop this run."""


def video_id(url: str):
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        return u.path.strip("/").split("/")[0] or None
    if host.endswith("youtube.com"):
        if u.path == "/watch":
            return (parse_qs(u.query).get("v") or [None])[0]
        parts = u.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("live", "embed", "v"):
            return parts[1]
    return None


def fetch_transcript(vid: str):
    """(text, language_code, auto_generated). Raises NoTranscript or TryLater."""
    if YouTubeTranscriptApi is None:
        raise TryLater("youtube-transcript-api is not installed")
    api = YouTubeTranscriptApi()
    permanent = tuple(getattr(yt_errors, n) for n in (
        "TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "VideoUnplayable", "AgeRestricted",
        "InvalidVideoId", "NotTranslatable") if hasattr(yt_errors, n))
    try:
        try:
            fetched = api.fetch(vid, languages=["en", "en-US", "en-GB"])
        except yt_errors.NoTranscriptFound:
            first = next(iter(api.list(vid)))      # no English: take the original language
            fetched = first.fetch()
    except permanent as e:
        raise NoTranscript(type(e).__name__) from e
    except StopIteration as e:
        raise NoTranscript("no transcripts listed") from e
    except Exception as e:  # noqa: BLE001 — IpBlocked, RequestBlocked, network trouble: try another day
        raise TryLater(f"{type(e).__name__}: {str(e)[:200]}") from e
    text = " ".join(s.text.replace("\n", " ").strip() for s in fetched.snippets if s.text.strip())
    if not text:
        raise NoTranscript("empty transcript")
    return text, fetched.language_code, bool(fetched.is_generated)


def pending(conn, limit: int):
    """Captured YouTube watch pages with no transcripts row yet, grouped by video id (newest first)."""
    rows = conn.execute(
        """SELECT p.id, p.url, p.text FROM pages p
           WHERE p.domain IN ('youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be')
             AND NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.page_id = p.id)
           ORDER BY p.last_seen DESC""").fetchall()
    videos = {}
    for r in rows:
        vid = video_id(r["url"])
        if vid:
            videos.setdefault(vid, []).append(r)
        if len(videos) > limit:
            videos.pop(vid)
            break
    return videos


def record(conn, page_ids, vid, status, lang=None, chars=None, error=None):
    conn.executemany(
        "INSERT OR REPLACE INTO transcripts (page_id, video_id, status, lang, chars, error, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(pid, vid, status, lang, chars, error, int(time.time())) for pid in page_ids])


def known(conn, vid):
    """('ok', block) or ('none', error) when this video was handled before (another version of the page), else None."""
    row = conn.execute(
        """SELECT t.status, t.error, p.text FROM transcripts t LEFT JOIN pages p ON p.id = t.page_id
           WHERE t.video_id = ? ORDER BY t.status = 'ok' DESC LIMIT 1""", (vid,)).fetchone()
    if row is None:
        return None
    if row["status"] == "ok" and row["text"] and row["text"].startswith(HEADER + " ("):
        return "ok", row["text"].split("\n\n", 1)[0]
    return ("none", row["error"]) if row["status"] == "none" else None


def apply(conn, rows, vid, text, lang, generated, block=None):
    """Put the transcript at the top of each version's text and queue the page for re-embedding."""
    block = block or f"{HEADER} ({lang}{', auto-generated' if generated else ''}):\n{text}"
    for r in rows:
        if r["text"].startswith(HEADER + " ("):
            continue
        new_text = (block + "\n\n" + r["text"])[:MAX_TEXT_CHARS]
        # summary/tags are rewritten from the new text by embed.py; vectors by vectorize.py.
        conn.execute("UPDATE pages SET text = ?, summary = NULL, tags = NULL WHERE id = ?", (new_text, r["id"]))
        conn.execute("DELETE FROM chunk_vectors WHERE page_id = ?", (r["id"],))
    record(conn, [r["id"] for r in rows], vid, "ok", lang, len(text) if text else len(block))


def run(conn, limit: int, fetch=fetch_transcript, pause: float = PAUSE_S) -> dict:
    counts = {"ok": 0, "none": 0, "stopped": None}
    for i, (vid, rows) in enumerate(pending(conn, limit).items()):
        seen = known(conn, vid)  # a newer capture of a video already handled: no need to ask YouTube
        if seen and seen[0] == "ok":
            apply(conn, rows, vid, None, None, None, block=seen[1])
            conn.commit()
            counts["ok"] += 1
            continue
        if seen and seen[0] == "none":
            record(conn, [r["id"] for r in rows], vid, "none", error=seen[1])
            conn.commit()
            counts["none"] += 1
            continue
        if i and pause:
            time.sleep(pause)
        try:
            text, lang, generated = fetch(vid)
        except NoTranscript as e:
            record(conn, [r["id"] for r in rows], vid, "none", error=str(e))
            conn.commit()
            counts["none"] += 1
            continue
        except TryLater as e:
            counts["stopped"] = str(e)
            break
        apply(conn, rows, vid, text, lang, generated)
        conn.commit()
        counts["ok"] += 1
        print(f"transcript {vid} ({lang}, {len(text)} chars) -> pages {[r['id'] for r in rows]}", flush=True)
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=40, help="videos per run")
    args = ap.parse_args()
    if YouTubeTranscriptApi is None:
        sys.exit("youtube-transcript-api is not installed")
    conn = db.connect(os.environ.get("AUTODIDACT_DB", "autodidact.db"))
    counts = run(conn, args.limit)
    print(f"done: {counts['ok']} transcripts, {counts['none']} without one"
          + (f"; stopped: {counts['stopped']}" if counts["stopped"] else ""))


if __name__ == "__main__":
    main()
