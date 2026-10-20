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


def _glob(pattern: str) -> re.Pattern:
    esc = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(f"^{esc}$", re.I)


class Policy:
    def __init__(self, text: str):
        self.text = text
        self.rules: list[str] = []
        self.skip_front_pages = False
        for line in text.splitlines():
            rule = line.strip()
            if not rule or rule.startswith("#"):
                continue
            if rule == SKIP_FRONT_PAGES:
                self.skip_front_pages = True
            elif not rule.startswith("!"):
                self.rules.append(rule)
        self.version = hashlib.sha256(text.encode()).hexdigest()[:12]

    def as_json(self) -> dict:
        return {"version": self.version, "skip_front_pages": self.skip_front_pages, "rules": self.rules}

    def blocks(self, url: str):
        """The rule that blocks *url* ("front page" for the directive), or None."""
        return matches(url, self.rules, self.skip_front_pages)


def matches(url: str, rules, skip_front_pages: bool = False):
    try:
        u = urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "not http(s)"
    host = u.hostname
    if skip_front_pages and u.path in ("", "/"):
        return "front page"
    path_and_query = (u.path or "/") + (f"?{u.query}" if u.query else "")
    for rule in rules:
        if rule.startswith(("http://", "https://")):
            if url.startswith(rule):
                return rule
        elif "/" in rule:
            host_glob, path_prefix = rule.split("/", 1)
            if _glob(host_glob).match(host) and path_and_query.startswith("/" + path_prefix):
                return rule
        elif _glob(rule).match(host):
            return rule
    return None


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
