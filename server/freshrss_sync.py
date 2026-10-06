"""Pull articles you read or starred in FreshRSS into Autodidact.

FreshRSS knows what you read on every client (laptop, phone) because the
clients sync read state back to it. This script asks FreshRSS's Google Reader
compatible API for items that are read and/or starred, converts each to text,
optionally fetches the full article when the feed only carried an excerpt, and
POSTs it to the Autodidact server's /ingest endpoint like the extension does.

Environment:
  FRESHRSS_URL           e.g. https://rss.phfactor.net  (no trailing slash)
  FRESHRSS_USER          FreshRSS username
  FRESHRSS_API_PASSWORD  the API password set in FreshRSS → Profile (not the login password)
  AUTODIDACT_URL         Autodidact server, default http://127.0.0.1:8765
  AUTODIDACT_TOKEN       bearer token

Enable the API in FreshRSS: Administration → Authentication → "Allow API access",
then set an API password on your profile page.

Usage:
  freshrss_sync.py [--mode both|read|starred] [--days 7] [--state PATH] [--no-fetch-full] [--dry-run]

Run from cron hourly. Idempotent: the server dedups on URL + content, and a
local state file remembers which item ids were already sent.

Caveat: FreshRSS cannot tell "read because I opened it" from "swept away with
mark-all-as-read". --mode starred is the noise-free option; --mode both (the
default) trusts your read state and marks starred items as such.
"""

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FRESHRSS_URL = os.environ.get("FRESHRSS_URL", "").rstrip("/")
FRESHRSS_USER = os.environ.get("FRESHRSS_USER", "")
FRESHRSS_API_PASSWORD = os.environ.get("FRESHRSS_API_PASSWORD", "")
AUTODIDACT_URL = os.environ.get("AUTODIDACT_URL", "http://127.0.0.1:8765").rstrip("/")
AUTODIDACT_TOKEN = os.environ.get("AUTODIDACT_TOKEN", "")

READ = "user/-/state/com.google/read"
UNREAD = "user/-/state/com.google/unread"
STARRED = "user/-/state/com.google/starred"
READING_LIST = "user/-/state/com.google/reading-list"
PAGE_SIZE = 200
MIN_TEXT_CHARS = 100          # same floor as the server
FULL_FETCH_BELOW = 700        # feed excerpts shorter than this trigger a full fetch
STATE_MAX_IDS = 20_000
UA = "Autodidact/0.1 (+https://github.com/phfactor/autodidact)"

from extract import FetchError, html_to_text  # noqa: E402  (shared with /ingest-url)
import extract


# ---------- FreshRSS (Google Reader API) ----------

class FreshRSS:
    def __init__(self, base, user, api_password):
        if not (base and user and api_password):
            sys.exit("FRESHRSS_URL, FRESHRSS_USER and FRESHRSS_API_PASSWORD are required")
        self.api = base + "/api/greader.php"
        self.auth = self._login(user, api_password)

    def _login(self, user, pw):
        data = urllib.parse.urlencode({"Email": user, "Passwd": pw}).encode()
        req = urllib.request.Request(self.api + "/accounts/ClientLogin", data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
        for line in body.splitlines():
            if line.startswith("Auth="):
                return line[5:].strip()
        sys.exit("FreshRSS login failed: no Auth token in response")

    def _get(self, path, params):
        url = f"{self.api}/reader/api/0/{path}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"GoogleLogin auth={self.auth}", "User-Agent": UA,
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def stream(self, stream_id, since, exclude=None):
        """Yield items in *stream_id* newer than *since* (unix seconds), oldest first."""
        params = {"n": PAGE_SIZE, "r": "o", "ot": int(since), "output": "json"}
        if exclude:
            params["xt"] = exclude
        continuation = None
        while True:
            if continuation:
                params["c"] = continuation
            data = self._get("stream/contents/" + urllib.parse.quote(stream_id, safe="/"), params)
            for item in data.get("items", []):
                yield item
            continuation = data.get("continuation")
            if not continuation:
                return


def item_url(item):
    for key in ("canonical", "alternate"):
        for link in item.get(key) or []:
            href = link.get("href")
            if href and href.startswith(("http://", "https://")):
                return href
    return None


def item_html(item):
    for key in ("content", "summary"):
        v = item.get(key)
        if isinstance(v, dict) and v.get("content"):
            return v["content"]
        if isinstance(v, str) and v:
            return v
    return ""


# ---------- full-text fetch ----------

def fetch_full_text(url):
    """Article text for *url* via extract.py (trafilatura if installed, else <article>/<main>)."""
    try:
        return extract.fetch_article(url)["text"]
    except FetchError:
        return ""


# ---------- Autodidact ----------

def ingest(payload, dry_run=False):
    if dry_run:
        print(f"[dry-run] {payload['source']}/{payload.get('feed', '')!r} {payload['url']} ({len(payload['text'])} chars)")
        return True
    req = urllib.request.Request(
        AUTODIDACT_URL + "/ingest",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {AUTODIDACT_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
        flag = "new" if res.get("created") else "dup"
        print(f"{flag} {payload['url'][:90]}")
        return True
    except urllib.error.HTTPError as e:
        print(f"server rejected {payload['url'][:90]}: HTTP {e.code} {e.read()[:120]!r}", file=sys.stderr)
        return e.code < 500   # 4xx: do not retry; 5xx: retry next run
    except Exception as e:  # noqa: BLE001
        print(f"server unreachable: {e}", file=sys.stderr)
        return False


# ---------- main ----------

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen": [], "last_run": 0}


def save_state(path, state):
    state["seen"] = state["seen"][-STATE_MAX_IDS:]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["both", "read", "starred"], default="both")
    ap.add_argument("--days", type=float, default=7, help="look back this many days of item publish dates")
    ap.add_argument("--state", default=os.environ.get("FRESHRSS_STATE", "freshrss_state.json"))
    ap.add_argument("--no-fetch-full", action="store_true", help="never fetch the article URL for full text")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many ingests (0 = no limit)")
    args = ap.parse_args()

    if not AUTODIDACT_TOKEN and not args.dry_run:
        sys.exit("AUTODIDACT_TOKEN is required")

    rss = FreshRSS(FRESHRSS_URL, FRESHRSS_USER, FRESHRSS_API_PASSWORD)
    state = load_state(args.state)
    seen_list = list(state["seen"])
    seen = set(seen_list)

    def mark_seen(iid):
        if iid not in seen:
            seen.add(iid)
            seen_list.append(iid)
    since = time.time() - args.days * 86400

    streams = []
    if args.mode in ("both", "starred"):
        streams.append(("starred", STARRED, None))
    if args.mode in ("both", "read"):
        streams.append(("read", READING_LIST, UNREAD))   # reading-list minus unread = read

    sent = skipped = failed = 0
    now = int(time.time())
    for label, stream_id, exclude in streams:
        for item in rss.stream(stream_id, since, exclude):
            iid = item.get("id")
            if not iid or iid in seen:
                continue
            url = item_url(item)
            if not url:
                mark_seen(iid); skipped += 1
                continue
            cats = set(item.get("categories") or [])
            starred = STARRED in cats or label == "starred"
            text = html_to_text(item_html(item))
            if not args.no_fetch_full and len(text) < FULL_FETCH_BELOW:
                try:
                    full = fetch_full_text(url)
                    if len(full) > len(text):
                        text = full
                except Exception as e:  # noqa: BLE001
                    print(f"full fetch failed for {url[:90]}: {e}", file=sys.stderr)
            if len(text) < MIN_TEXT_CHARS:
                mark_seen(iid); skipped += 1
                continue

            origin = item.get("origin") or {}
            payload = {
                "source": "rss",
                "visit_id": "freshrss:" + iid,
                "url": url,
                "title": html.unescape(item.get("title") or "")[:500],
                "description": ("starred · " if starred else "") + (origin.get("title") or ""),
                "feed": origin.get("title") or "",
                "text": text,
                "published_at": int(item.get("published") or 0) or None,
                "captured_at": now,
                "dwell_s": 0,
                "browser": "FreshRSS",
            }
            ok = ingest(payload, args.dry_run)
            if ok:
                mark_seen(iid); sent += 1
            else:
                failed += 1
            if args.limit and sent >= args.limit:
                break

    state["seen"] = seen_list
    state["last_run"] = now
    if not args.dry_run:
        save_state(args.state, state)
    print(f"done: {sent} sent, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
