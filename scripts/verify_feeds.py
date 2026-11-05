"""Isolated browser, XML, lifecycle, privacy, and log-redaction checks for #62."""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET


QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-feeds-qa"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)
QA_DB = QA_ROOT / "feeds.sqlite"
BASE = "http://127.0.0.1:8813"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{QA_DB.as_posix()}",
    "SESSION_SECRET": "isolated-feed-test-secret",
    "UPLOAD_DIR": str(QA_ROOT / "uploads"),
    "APP_BASE_URL": BASE,
    "PUZZARY_HOST": "127.0.0.1",
    "PUZZARY_PORT": "8813",
    "PUZZARY_FEED_RATE_LIMIT": "4",
    "PUZZARY_FEED_RATE_WINDOW_SECONDS": "300",
})

from playwright.sync_api import sync_playwright
from sqlalchemy import inspect, select

from app.database import Base, SessionLocal, engine
from app.main import hash_password
from app.models import Puzzle, PuzzleFeedToken, PuzzleSeries, PuzzleSeriesEntry, PuzzleTag, User
from app.permissions import initialize_permissions
from app.puzzles import get_puzzle_type
from app.server import FeedTokenLogFilter


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
assert "puzzle_feed_tokens" in inspect(engine).get_table_names()

generated = get_puzzle_type("takuzu").manual_builder({
    "solution_grid": [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
    "given_count": 0,
    "seed": "feed-qa",
})


def puzzle(puzzle_id: str, title: str, creator_id: int, **overrides) -> Puzzle:
    values = {
        "id": puzzle_id,
        "puzzle_type": "takuzu",
        "title": title,
        "theme": "Space & Stars",
        "difficulty": "easy",
        "prompt": "SPOILER PROMPT must never enter feeds",
        "play_payload": generated.play_payload,
        "print_payload": generated.print_payload,
        "solution_payload": generated.solution_payload,
        "puzzle_metadata": {},
        "source": "test",
        "created_by_id": creator_id,
        "publish_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    values.update(overrides)
    return Puzzle(**values)


with SessionLocal() as db:
    creator = User(display_name="Feed Creator", is_admin=True, is_active=True)
    owner = User(
        display_name="Feed Owner", is_admin=False, is_active=True,
        password_hash=hash_password("Feed-test-123"), session_version=1,
    )
    db.add_all([creator, owner])
    db.commit()
    initialize_permissions(db)
    general_tag = PuzzleTag(name="Astronomy", tag_type="General", slug="astronomy")
    clue_tag = PuzzleTag(name="SECRET CLUE TAG", tag_type="Clue", slug="secret-clue")
    db.add_all([general_tag, clue_tag])
    db.flush()
    public = puzzle("feed-public", "Planets & Comets", creator.id)
    public.tags = [general_tag, clue_tag]
    first = puzzle("feed-series-first", "Series First", creator.id)
    first.tags = [general_tag]
    locked = puzzle("feed-series-locked", "LOCKED SERIES SECRET", creator.id)
    locked.tags = [general_tag]
    fixtures = [
        public,
        first,
        locked,
        puzzle("feed-other-theme", "Wrong theme", creator.id, theme="Ocean"),
        puzzle("feed-draft", "DRAFT SECRET", creator.id, puzzle_metadata={"draft": True}),
        puzzle("feed-future", "FUTURE SECRET", creator.id, publish_at=datetime.now(timezone.utc) + timedelta(days=1)),
        puzzle("feed-unlisted", "UNLISTED SECRET", creator.id, puzzle_metadata={"visibility": "unlisted"}),
        puzzle("feed-archived", "ARCHIVED SECRET", creator.id, archived_at=datetime.now(timezone.utc)),
        puzzle("feed-password", "PASSWORD SECRET", creator.id, access_password_hash=hash_password("Puzzle-123")),
    ]
    db.add_all(fixtures)
    series = PuzzleSeries(id="feed-series", title="Ordered", require_previous=True, created_by_id=creator.id)
    db.add(series)
    db.flush()
    db.add_all([
        PuzzleSeriesEntry(series_id=series.id, puzzle_id=first.id, position=1),
        PuzzleSeriesEntry(series_id=series.id, puzzle_id=locked.id, position=2),
    ])
    db.commit()
    owner_id, creator_id, tag_id = owner.id, creator.id, general_tag.id


assert FeedTokenLogFilter.redact("GET /feeds/abcDEF_123-" + "x" * 28 + ".atom HTTP/1.1") == (
    "GET /feeds/[REDACTED].atom HTTP/1.1"
)


def extract_url(body: str, feed_format: str) -> str:
    match = re.search(rf'<input value="([^"]+\.{feed_format})" readonly data-feed-url>', body)
    assert match, body[:2000]
    return html.unescape(match.group(1))


def wait_ready(request) -> None:
    for _ in range(120):
        try:
            if request.get(BASE + "/health").ok:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Feed QA server did not start")


log_path = QA_ROOT / "server.log"
with log_path.open("w", encoding="utf-8") as server_log:
    server = subprocess.Popen(
        [sys.executable, "-m", "app.server"],
        env=os.environ.copy(), stdout=server_log, stderr=subprocess.STDOUT,
    )
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = context.new_page()
        wait_ready(context.request)
        page.goto(BASE + "/login")
        page.locator('input[name="username"]').fill("Feed Owner")
        page.locator('input[name="password"]').fill("Feed-test-123")
        page.locator('button[type="submit"]').click()
        page.wait_for_url("**/library")
        page.goto(BASE + "/account/feeds")
        assert page.locator("h1").inner_text() == "Puzzle feeds"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert "configure HTTPS" in page.locator("body").inner_text()

        created = context.request.post(BASE + "/account/feeds", form={
            "name": "Space feed",
            "puzzle_types": "takuzu",
            "themes": "Space & Stars",
            "tag_ids": str(tag_id),
            "creator_ids": str(creator_id),
            "entry_limit": "10",
        })
        assert created.status == 201, created.text()
        body = created.text()
        atom_url = extract_url(body, "atom")
        rss_url = extract_url(body, "rss")
        secret = atom_url.rsplit("/", 1)[-1].removesuffix(".atom")
        with SessionLocal() as db:
            token = db.scalar(select(PuzzleFeedToken).where(PuzzleFeedToken.name == "Space feed"))
            assert token and secret not in token.token_hash and token.token_hash != secret
            assert token.filters == {
                "puzzle_types": ["takuzu"], "themes": ["Space & Stars"],
                "tag_ids": [tag_id], "creator_ids": [creator_id], "limit": 10,
            }
            token_id = token.id
        management = context.request.get(BASE + "/account/feeds").text()
        assert secret not in management and "Space feed" in management

        atom_response = context.request.get(atom_url)
        rss_response = context.request.get(rss_url)
        assert atom_response.status == 200 and "application/atom+xml" in atom_response.headers["content-type"]
        assert rss_response.status == 200 and "application/rss+xml" in rss_response.headers["content-type"]
        for response in (atom_response, rss_response):
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["x-robots-tag"] == "noindex, nofollow"
            text = response.text()
            for forbidden in (
                "SPOILER PROMPT", "SECRET CLUE TAG", "LOCKED SERIES SECRET", "DRAFT SECRET",
                "FUTURE SECRET", "UNLISTED SECRET", "ARCHIVED SECRET", "PASSWORD SECRET", "Wrong theme",
            ):
                assert forbidden not in text, forbidden

        atom = ET.fromstring(atom_response.body())
        atom_ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = atom.findall("a:entry", atom_ns)
        assert {entry.findtext("a:title", namespaces=atom_ns) for entry in entries} == {"Planets & Comets", "Series First"}
        public_entry = next(entry for entry in entries if entry.findtext("a:title", namespaces=atom_ns) == "Planets & Comets")
        atom_id = public_entry.findtext("a:id", namespaces=atom_ns)
        assert atom_id == "urn:puzzary:puzzle:feed-public"
        assert public_entry.find("a:link", atom_ns).attrib["href"] == BASE + "/puzzles/feed-public"
        assert public_entry.findtext("a:author/a:name", namespaces=atom_ns) == "Feed Creator"
        assert public_entry.findtext("a:published", namespaces=atom_ns).endswith("Z")
        assert "puzzle" in public_entry.findtext("a:summary", namespaces=atom_ns).lower()
        assert "Space & Stars" in {node.attrib["term"] for node in public_entry.findall("a:category", atom_ns)}

        rss = ET.fromstring(rss_response.body())
        rss_item = next(item for item in rss.findall("./channel/item") if item.findtext("title") == "Planets & Comets")
        assert rss_item.findtext("guid") == atom_id
        assert rss_item.findtext("link") == BASE + "/puzzles/feed-public"
        assert rss_item.findtext("{http://purl.org/dc/elements/1.1/}creator") == "Feed Creator"
        assert rss_item.findtext("pubDate").endswith("GMT")

        rotated = context.request.post(BASE + f"/account/feeds/{token_id}/rotate")
        assert rotated.status == 200
        new_atom_url = extract_url(rotated.text(), "atom")
        new_secret = new_atom_url.rsplit("/", 1)[-1].removesuffix(".atom")
        assert new_secret != secret
        assert context.request.get(atom_url).status == 404
        rotated_feed = context.request.get(new_atom_url)
        assert rotated_feed.status == 200
        rotated_xml = ET.fromstring(rotated_feed.body())
        assert rotated_xml.findtext("a:id", namespaces=atom_ns) == atom.findtext("a:id", namespaces=atom_ns)
        assert context.request.post(BASE + f"/account/feeds/{token_id}/revoke", max_redirects=0).status == 303
        assert context.request.get(new_atom_url).status == 404

        rate_created = context.request.post(BASE + "/account/feeds", form={"name": "Rate feed", "entry_limit": "25"})
        rate_url = extract_url(rate_created.text(), "atom")
        for _ in range(4):
            assert context.request.get(rate_url).status == 200
        limited = context.request.get(rate_url)
        assert limited.status == 429 and limited.headers["retry-after"]

        disabled_created = context.request.post(BASE + "/account/feeds", form={"name": "Disabled feed"})
        disabled_url = extract_url(disabled_created.text(), "rss")
        with SessionLocal() as db:
            db.get(User, owner_id).is_active = False
            db.commit()
        assert context.request.get(disabled_url).status == 404
        browser.close()
finally:
    server.terminate()
    server.wait(timeout=15)

logs = log_path.read_text(encoding="utf-8")
for feed_secret in (secret, new_secret):
    assert feed_secret not in logs
assert "/feeds/[REDACTED].atom" in logs or "/feeds/[REDACTED].rss" in logs
print("Feed UI, filtering, spoiler exclusion, valid RSS/Atom metadata, rotation, revocation, account disablement, rate limiting, mobile layout, migration, and access-log redaction passed.")
