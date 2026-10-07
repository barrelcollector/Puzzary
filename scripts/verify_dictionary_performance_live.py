"""Opt-in read-only request and browser checks for large dictionary deployments."""
import os
if os.getenv("PUZZARY_DICTIONARY_PERFORMANCE_QA") != "1":
    raise SystemExit("Set PUZZARY_DICTIONARY_PERFORMANCE_QA=1 to run live checks")
import base64
import json
from pathlib import Path
import time
import urllib.request
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import func, select
from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import DictionaryDefinition, DictionaryWord, Puzzle, User, WordBank

base = "http://127.0.0.1:8790"
with SessionLocal() as db:
    user = db.scalar(select(User).where(User.is_admin.is_(True), User.is_active.is_(True)))
    session = TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({
        "user_id": user.id, "session_version": user.session_version}).encode())).decode()
    puzzle = db.scalar(select(Puzzle).where(Puzzle.archived_at.is_(None)).limit(1))
    counts = {"words": db.scalar(select(func.count(DictionaryWord.id))),
              "definitions": db.scalar(select(func.count(DictionaryDefinition.id))),
              "banks": db.scalar(select(func.count(WordBank.id)))}
    assert counts == {"words": 5017, "definitions": 12257, "banks": 100}, counts
paths = ["/health", "/library", "/generate", "/admin", "/admin/dictionary",
         "/admin/dictionary?page=2", "/admin/dictionary?q=cloud", "/admin/word-banks",
         "/admin/tags", "/admin/tags?type=Topic", "/admin/users"]
if puzzle:
    paths.append("/puzzles/" + puzzle.id)
results = []
for path in paths:
    start = time.monotonic()
    request = urllib.request.Request(base + path, headers={"Cookie": "session=" + session})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode()
        elapsed = round(time.monotonic() - start, 3)
        assert response.status == 200
        assert "/login" not in response.url, path
        if path == "/admin/dictionary":
            assert body.count('class="word-row"') == 100 and "5017 words" in body
        if path == "/admin/dictionary?page=2":
            assert "Page 2" in body and body.count('class="word-row"') == 100
        if path == "/admin/dictionary?q=cloud":
            assert 'value="cloud"' in body and "cloud" in body.lower()
        if path == "/library":
            assert len(body) < 2_000_000, "Library still contains the entire clue-tag list"
        if path == "/admin/tags":
            assert body.count('class="tag-card"') <= 200
        results.append({"path": path, "seconds": elapsed, "bytes": len(body)})
        print(json.dumps(results[-1]), flush=True)
errors = []
output = Path("/data/dictionary-performance-qa")
output.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    context.add_cookies([{"name": "session", "value": session, "url": base}])
    for width in (1280, 390):
        page = context.new_page()
        page.set_viewport_size({"width": width, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        for path in ("/library", "/admin/dictionary", "/admin/tags?type=Topic", "/admin/word-banks"):
            assert page.goto(base + path, wait_until="networkidle").ok
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 4"), (path, width)
            if path == "/admin/dictionary":
                assert page.locator(".word-row").count() == 100
                page.get_by_role("link", name="Next", exact=True).click()
                assert "page=2" in page.url
            page.screenshot(path=str(output / (path.split("?")[0].strip("/").replace("/", "-") + f"-{width}.png")))
        page.close()
    browser.close()
assert not errors, errors
print(json.dumps({"counts_preserved": counts, "browser_checks": "desktop/mobile passed", "pages": results}, indent=2))
