import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import policy  # noqa: E402
from app import create_app  # noqa: E402

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
TEXT = "Write-ahead logging changes how durability works in SQLite. " * 6
VECTORS = json.load(open(os.path.join(os.path.dirname(__file__), "policy_vectors.json")))


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c[0])
def test_shared_vectors(case):
    # The same file drives extension/test/unit.mjs, so background.js must agree with these.
    url, expected, page = (case + [None])[:3]
    assert policy.matches(url, VECTORS["rules"], VECTORS["skip_front_pages"], VECTORS["allow"],
                          VECTORS["allow_platforms"], page) == expected


def test_parse_directives_comments_and_version():
    p = policy.Policy("# comment\n\n!skip-front-pages\n  axiom.phfactor.net  \n!unknown-directive\n!allow *.substack.com\n!allow \n"
                      "!allow-platform WordPress, substack ghost\n")
    assert p.skip_front_pages and p.rules == ["axiom.phfactor.net"] and p.allow == ["*.substack.com"]
    assert p.allow_platforms == ["wordpress", "substack", "ghost"]
    assert p.as_json()["allow"] == ["*.substack.com"]
    assert p.as_json()["version"] == policy.Policy(p.text).version != policy.Policy("x").version


def test_default_policy_file_is_valid():
    p = policy.Policy(open(policy.DEFAULT_FILE).read())
    assert p.skip_front_pages
    assert p.blocks("https://www.youtube.com/shorts/abc") and not p.blocks("https://www.youtube.com/watch?v=1")
    assert p.blocks("https://nas.phfactor.net/") and p.blocks("https://axiom.phfactor.net/v1/models")


@pytest.fixture
def setup(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), token=TOKEN)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, tmp_path / "policy.txt"


def capture(url, **extra):
    return {"url": url, "title": "t", "text": TEXT, "visit_id": extra.pop("visit_id", None), **extra}


def test_policy_file_seeded_served_and_reloaded(setup):
    client, path = setup
    j = client.get("/policy").json            # first use copies the default next to the DB
    assert path.exists() and j["skip_front_pages"] and "axiom.phfactor.net" in j["rules"]
    path.write_text("example.org\n")
    os.utime(path, (time.time() + 5, time.time() + 5))   # make sure the mtime moves
    j2 = client.get("/policy").json
    assert j2 == {"version": j2["version"], "skip_front_pages": False, "rules": ["example.org"], "allow": [],
                  "allow_platforms": []}
    assert j2["version"] != j["version"]


def test_ingest_enforces_policy_for_browser_captures_only(setup):
    client, _ = setup
    r = client.post("/ingest", json=capture("https://news.ycombinator.com/"), headers=HEADERS)
    assert r.status_code == 200 and r.json == {"skipped": "front page"}
    r = client.post("/ingest", json=capture("https://www.youtube.com/shorts/abc"), headers=HEADERS)
    assert r.json == {"skipped": "*youtube.com/shorts/"}
    assert client.post("/ingest", json=capture("https://www.youtube.com/watch?v=1"), headers=HEADERS).json["created"]
    # RSS items are articles chosen in FreshRSS; the browser policy does not apply to them.
    assert client.post("/ingest", json=capture("https://daringfireball.net/", source="rss"), headers=HEADERS).json["created"]
    assert client.get("/stats").json["pages"] == 2
    # The debug log says why.
    notes = [r["note"] for r in client.get("/debug/requests").json["requests"] if r["path"] == "/ingest"]
    assert "skipped by policy: front page" in notes


def test_ingest_keeps_blog_front_pages_by_platform(setup):
    client, path = setup
    path.write_text("!skip-front-pages\n!allow-platform wordpress substack\n")
    blog = capture("https://www.someblog.net/", platform="wordpress", blog_front=True)
    news = capture("https://www.bignews.com/", platform="wordpress", blog_front=False)
    old = capture("https://www.oldclient.net/")   # extensions before 0.1.6 send no platform
    assert client.post("/ingest", json=blog, headers=HEADERS).json["created"]
    assert client.post("/ingest", json=news, headers=HEADERS).json == {"skipped": "front page"}
    assert client.post("/ingest", json=old, headers=HEADERS).json == {"skipped": "front page"}
