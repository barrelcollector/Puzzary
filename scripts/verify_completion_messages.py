"""Isolated custom completion-message, portability, safety, and Group Solve checks."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

database = Path(tempfile.gettempdir()) / "puzzary-completion-message-qa.sqlite"
artifacts = Path(os.environ.get("QA_ARTIFACT_DIR", Path(tempfile.gettempdir()) / "puzzary-completion-message-artifacts"))
artifacts.mkdir(parents=True, exist_ok=True)
database.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-completion-message-test-secret"
os.environ["UPLOAD_DIR"] = str(Path(tempfile.gettempdir()) / "puzzary-completion-message-uploads")

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import inspect, text

from app.database import Base, SessionLocal, engine
from app.schema_migrations import upgrade_database
from app.main import SESSION_SECRET, hash_password
from app.models import Puzzle, User
from app.puzzles import list_puzzle_types


Base.metadata.create_all(engine)
with engine.begin() as connection:
    connection.execute(text("ALTER TABLE puzzles DROP COLUMN completion_message"))
upgrade_database()
assert "completion_message" in {column["name"] for column in inspect(engine).get_columns("puzzles")}

message = "Brilliant solve <together> & safely done!"
generated = {}
for plugin in list_puzzle_types():
    options = {"grid_size": 4} if plugin.slug in ("sudoku", "takuzu") else {}
    if plugin.slug == "cross_equations":
        options = {"grid_size": 3, "operators": ["add"]}
    if plugin.slug == "maze":
        options = {"maze_size": "custom", "custom_width": 5, "custom_height": 5, "allow_loops": "no"}
    if plugin.slug == "word_search":
        item = plugin.manual_builder({"grid": "CATXXXXX\nDOGXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX", "words": "CAT,DOG", "auto_confirm": "no"})
    elif plugin.slug == "word_pyramid":
        item = plugin.manual_builder({"rows": "TIP|Advice|Useful suggestion\nPINT|A measure|Liquid measure\nPOINT|Sharp end|A location", "gimme_count": 0})
    elif plugin.slug == "spell_it":
        item = plugin.manual_builder({"entries": [{"answer": "CAT", "definition": "A small feline"}], "mask_percent": 100, "preserve_edges": "no", "letter_bank": "off", "hint_limit": 2})
    else:
        item = plugin.generator("", "easy", None, "completion-message-qa", options)
    generated[plugin.slug] = item

with SessionLocal() as db:
    admin = User(display_name="Completion Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Completion-test-123"))
    db.add(admin)
    db.flush()
    admin_id = admin.id
    for kind, item in generated.items():
        db.add(Puzzle(
            id=f"completion-{kind}", puzzle_type=kind, title=f"Completion {kind}", theme="",
            difficulty="easy", prompt=item.prompt, completion_message=message,
            play_payload=item.play_payload, print_payload=item.print_payload,
            solution_payload=item.solution_payload, puzzle_metadata={}, source="test", created_by_id=admin_id,
        ))
    blank = generated["takuzu"]
    db.add(Puzzle(
        id="completion-default", puzzle_type="takuzu", title="Default completion", theme="",
        difficulty="easy", prompt=blank.prompt, completion_message="", play_payload=blank.play_payload,
        print_payload=blank.print_payload, solution_payload=blank.solution_payload,
        puzzle_metadata={}, source="test", created_by_id=admin_id,
    ))
    db.commit()


def cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


base = "http://127.0.0.1:8794"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8794"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_cookies([{"name": "session", "value": cookie(admin_id), "url": base}])
        for _ in range(100):
            try:
                if context.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("QA server failed to start")

        summary = context.request.get(base + "/api/puzzles/completion-sudoku")
        assert summary.ok and summary.json()["completion_message"] == message
        exported = context.request.get(base + "/api/puzzles/completion-sudoku/export")
        assert exported.ok and exported.json()["completion_message"] == message
        imported = context.request.post(
            base + "/api/puzzles/import",
            data={
                "source_text": json.dumps(exported.json()),
                "dry_run": False,
                "duplicate_policy": "copy",
            },
        )
        assert imported.ok, imported.text()
        imported_id = imported.json()["items"][0]["local_id"]
        imported_summary = context.request.get(base + f"/api/puzzles/{imported_id}")
        assert imported_summary.ok and imported_summary.json()["completion_message"] == message

        for kind in generated:
            page = context.new_page()
            page.goto(base + f"/puzzles/completion-{kind}")
            assert page.locator("#completion-message").inner_text() == message
            assert page.locator("#completion-celebration").is_hidden()
            page.evaluate("window.PuzzaryCelebrate.complete()")
            assert page.locator("#completion-celebration").is_visible()
            assert page.locator("#completion-message").inner_text() == message
            assert page.locator("#completion-message img").count() == 0
            page.close()

        fallback = context.new_page()
        fallback.goto(base + "/puzzles/completion-default")
        assert fallback.locator("#completion-message").inner_text().startswith("Your progress is saved")

        editor = context.new_page()
        editor.goto(base + "/admin/puzzles/completion-sudoku/edit")
        field = editor.locator('[name="completion_message"]')
        assert field.input_value() == message
        field.fill("A fresh preview message")
        assert editor.locator("[data-completion-message-preview-text]").inner_text() == "A fresh preview message"
        editor.screenshot(path=str(artifacts / "completion-message-editor.png"), full_page=True)

        created = context.request.post(base + "/api/puzzles/completion-sudoku/groups", data={})
        assert created.ok, created.text()
        group = created.json()
        assert group["completion_message"] == message
        group_page = context.new_page()
        group_page.goto(base + group["url"])
        assert group_page.locator("#completion-message").inner_text() == message
        assert group_page.locator("#completion-celebration h2").inner_text() == "Great solve, team."
        group_page.evaluate("window.PuzzaryCelebrate.complete()")
        group_page.screenshot(path=str(artifacts / "completion-message-group.png"), full_page=True)

        too_long = context.request.post(
            base + "/admin/puzzles/completion-sudoku/metadata",
            form={"category_id": "0", "completion_message": "x" * 501},
            max_redirects=0,
        )
        assert too_long.status == 422
        updated = context.request.post(
            base + "/admin/puzzles/completion-sudoku/metadata",
            form={"category_id": "0", "completion_message": "Saved from puzzle metadata"},
            max_redirects=0,
        )
        assert updated.status == 303
        assert context.request.get(base + "/api/puzzles/completion-sudoku").json()["completion_message"] == "Saved from puzzle metadata"
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    engine.dispose()
    try:
        database.unlink(missing_ok=True)
    except PermissionError:
        # Windows can release the stopped server's SQLite handle just after this process exits.
        pass

print("Completion message migration, all puzzle pages, safe text rendering, preview, API/export, defaults and Group Solve passed.")
