"""Capture policy shared by the server and the browser extensions.

The rules live in a plain text file (see policy.default.txt for the syntax). The server enforces them
on browser captures at /ingest and serves them at GET /policy; background.js applies the same rules
before anything is sent. Keep matches() and background.js's policy matcher in step: both are tested
against server/tests/policy_vectors.json.
"""

import hashlib
import os
import re
import shutil
from urllib.parse import urlsplit

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.default.txt")
SKIP_FRONT_PAGES = "!skip-front-pages"
ALLOW_PLATFORM = "!allow-platform"   # "!allow-platform wordpress substack": keep blog front pages
ALLOW = "!allow "   # "!allow <rule>": never block what <rule> matches. Written as a directive so that
                    # extensions older than 0.1.5, which ignore "!" lines, do not read it as a block rule.


def _glob(pattern: str) -> re.Pattern:
    esc = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(f"^{esc}$", re.I)


class Policy:
    def __init__(self, text: str):
        self.text = text
        self.rules: list[str] = []
        self.allow: list[str] = []
        self.allow_platforms: list[str] = []
        self.skip_front_pages = False
        for line in text.splitlines():
            rule = line.strip()
            if not rule or rule.startswith("#"):
                continue
            if rule == SKIP_FRONT_PAGES:
                self.skip_front_pages = True
            elif rule.startswith(ALLOW_PLATFORM):
                self.allow_platforms += [p for p in re.split(r"[\s,]+", rule[len(ALLOW_PLATFORM):].lower()) if p]
            elif rule.startswith(ALLOW) and rule[len(ALLOW):].strip():
                self.allow.append(rule[len(ALLOW):].strip())
            elif not rule.startswith("!"):
                self.rules.append(rule)
        self.version = hashlib.sha256(text.encode()).hexdigest()[:12]

    def as_json(self) -> dict:
        return {"version": self.version, "skip_front_pages": self.skip_front_pages, "rules": self.rules,
                "allow": self.allow, "allow_platforms": self.allow_platforms}

    def blocks(self, url: str, page=None):
        """The rule that blocks *url* ("front page" for the directive), or None. *page*: the capture's
        {"platform", "blog_front"} as detected by the extension."""
        return matches(url, self.rules, self.skip_front_pages, self.allow, self.allow_platforms, page)


def _hits(url: str, host: str, path_and_query: str, rule: str) -> bool:
    if rule.startswith(("http://", "https://")):
        return url.startswith(rule)
    if "/" in rule:
        host_glob, path_prefix = rule.split("/", 1)
        return bool(_glob(host_glob).match(host)) and path_and_query.startswith("/" + path_prefix)
    return bool(_glob(rule).match(host))


def matches(url: str, rules, skip_front_pages: bool = False, allow=(), allow_platforms=(), page=None):
    try:
        u = urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "not http(s)"
    host = u.hostname
    path_and_query = (u.path or "/") + (f"?{u.query}" if u.query else "")
    if any(_hits(url, host, path_and_query, a) for a in allow):
        return None
    blog_front = bool(page and page.get("blog_front") and page.get("platform") in allow_platforms)
    if skip_front_pages and u.path in ("", "/") and not blog_front:
        return "front page"
    return next((rule for rule in rules if _hits(url, host, path_and_query, rule)), None)


class PolicyFile:
    """The live policy file, re-read whenever its mtime changes; created from the default if missing."""

    def __init__(self, path: str):
        self.path = path
        self._mtime = None
        self._policy = Policy("")

    def get(self) -> Policy:
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            shutil.copyfile(DEFAULT_FILE, self.path)
            mtime = os.stat(self.path).st_mtime_ns
        if mtime != self._mtime:
            with open(self.path, encoding="utf-8") as f:
                self._policy = Policy(f.read())
            self._mtime = mtime
        return self._policy
