"""Fetch a URL and turn the page into title / description / text.

Used by freshrss_sync.py (full-article fetch for excerpt-only feeds) and by the
POST /ingest-url endpoint (anything shared from the iOS/macOS share sheet,
including Apple News links). Uses trafilatura when it is installed, otherwise
a stdlib fallback that takes the page's <article>/<main> block.

Apple News: an https://apple.news/A… link is a landing page that forwards
non-Apple clients to the publisher's URL via a meta refresh / script redirect
when the story has a web version. unwrap_apple_news() finds that target so the
caller can fetch the real article. News+ exclusives have no target; the
landing page's own title and description are all there is.
"""

import html as htmllib
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

try:  # optional, much better article extraction
    import trafilatura  # type: ignore
except ImportError:  # pragma: no cover
    trafilatura = None

UA = "Autodidact/0.1 (+https://github.com/phubbard/autodidact)"
MAX_BYTES = 2_000_000
APPLE_NEWS_HOSTS = ("apple.news", "www.apple.news")


class FetchError(Exception):
    """A fetch failed or returned something that is not an HTML page."""


# ---------- HTML → text (stdlib) ----------

class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
             "pre", "tr", "section", "article", "figcaption", "hr", "dd", "dt", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(fragment: str) -> str:
    p = _Text()
    p.feed(fragment or "")
    text = "".join(p.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- metadata ----------

_META_RE = re.compile(
    r'<meta\s+[^>]*?(?:name|property)\s*=\s*["\']([^"\']+)["\'][^>]*?content\s*=\s*["\']([^"\']*)["\']'
    r'|<meta\s+[^>]*?content\s*=\s*["\']([^"\']*)["\'][^>]*?(?:name|property)\s*=\s*["\']([^"\']+)["\']',
    re.I | re.S,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_CANON_RE = re.compile(r'<link\s+[^>]*?rel\s*=\s*["\']canonical["\'][^>]*?href\s*=\s*["\']([^"\']+)["\']', re.I | re.S)
_LANG_RE = re.compile(r"<html[^>]*\slang\s*=\s*[\"']([a-zA-Z-]+)", re.I)


def meta_tags(page: str) -> dict:
    out = {}
    for n1, c1, c2, n2 in _META_RE.findall(page[:200_000]):
        name, content = (n1, c1) if n1 else (n2, c2)
        out.setdefault(name.lower(), htmllib.unescape(content).strip())
    return out


def page_title(page: str, metas: dict) -> str:
    t = metas.get("og:title") or metas.get("twitter:title") or ""
    if not t:
        m = _TITLE_RE.search(page)
        t = htmllib.unescape(m.group(1)) if m else ""
    return re.sub(r"\s+", " ", t).strip()[:500]


def page_description(metas: dict) -> str:
    return (metas.get("description") or metas.get("og:description") or metas.get("twitter:description") or "")[:2000]


# ---------- fetch ----------

def fetch(url: str, timeout: int = 20) -> tuple[str, str]:
    """GET *url* following HTTP redirects. Returns (final_url, html). Raises FetchError."""
    if not url.startswith(("http://", "https://")):
        raise FetchError("url must be http(s)")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "xml" not in ctype:
                raise FetchError(f"not an HTML page ({ctype.split(';')[0] or 'unknown type'})")
            raw = r.read(MAX_BYTES)
            final = r.geturl()
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).hostname}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"fetch failed: {getattr(e, 'reason', e)}") from e
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    else:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    try:
        page = raw.decode(charset, errors="replace")
    except LookupError:
        page = raw.decode("utf-8", errors="replace")
    return final, page


# ---------- Apple News ----------

def is_apple_news(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in APPLE_NEWS_HOSTS


_REFRESH_RE = re.compile(r'<meta\s+[^>]*http-equiv\s*=\s*["\']refresh["\'][^>]*content\s*=\s*["\'][^"\']*?url\s*=\s*([^"\'\s;]+)', re.I | re.S)
_LOCATION_RE = re.compile(r'(?:location\.(?:replace|assign|href)\s*[\(=]\s*|window\.location\s*=\s*)["\'](https?://[^"\']+)["\']', re.I)


def unwrap_apple_news(page: str, page_url: str) -> str | None:
    """The URL a landing page forwards to (meta refresh, location.replace, or an off-site canonical),
    or None when it does not forward anywhere (a News+ exclusive, or Apple changed the page format)."""
    for rx in (_REFRESH_RE, _LOCATION_RE):
        m = rx.search(page)
        if m:
            target = urllib.parse.urljoin(page_url, htmllib.unescape(m.group(1).strip()))
            if target.startswith(("http://", "https://")) and target != page_url:
                return target
    m = _CANON_RE.search(page)
    if m:
        target = urllib.parse.urljoin(page_url, htmllib.unescape(m.group(1)))
        if target.startswith(("http://", "https://")) and target != page_url and not is_apple_news(target):
            return target
    return None


# ---------- extraction ----------

def extract(page: str, url: str) -> dict:
    """{'title', 'description', 'text', 'canonical', 'lang'} for an HTML page. Text may be ''."""
    metas = meta_tags(page)
    out = {
        "title": page_title(page, metas),
        "description": page_description(metas),
        "text": "",
        "canonical": "",
        "lang": "",
    }
    m = _CANON_RE.search(page)
    if m:
        out["canonical"] = urllib.parse.urljoin(url, htmllib.unescape(m.group(1)))
    m = _LANG_RE.search(page)
    if m:
        out["lang"] = m.group(1)[:16]

    if trafilatura:
        try:
            text = trafilatura.extract(page, url=url, include_comments=False, include_tables=True,
                                       favor_recall=True)
        except Exception:  # noqa: BLE001 — never let the optional dependency break ingest
            text = None
        if text and text.strip():
            out["text"] = text.strip()
            return out

    # Fallback: the article/main block if there is one, else the body minus boilerplate tags.
    m = re.search(r"<(article|main)\b.*?</\1>", page, re.S | re.I)
    block = m.group(0) if m else page
    if not m:
        block = re.sub(r"<(nav|header|footer|aside|form)\b.*?</\1>", " ", block, flags=re.S | re.I)
    out["text"] = html_to_text(block)
    return out


def fetch_article(url: str, timeout: int = 20) -> dict:
    """fetch + (apple.news unwrap) + extract. Returns extract()'s dict plus 'url' (final) and 'via' (the
    apple.news link when one was unwrapped, else '')."""
    final, page = fetch(url, timeout)
    via = ""
    # One forwarding hop: apple.news landing pages (script or meta redirect), or any page that
    # uses a meta refresh, which a browser would follow too.
    if is_apple_news(final) or _REFRESH_RE.search(page):
        target = unwrap_apple_news(page, final)
        if target:
            via = final
            final, page = fetch(target, timeout)
    out = extract(page, final)
    out["url"] = final
    out["via"] = via
    return out
