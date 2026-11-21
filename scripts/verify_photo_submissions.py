"""Isolated upload validation, authorization, review, and cleanup checks for issue #23."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-photo-submissions-qa"
QA_DB = QA_ROOT / "photos.sqlite"
QA_UPLOADS = QA_ROOT / "uploads"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)

os.environ["DATABASE_URL"] = f"sqlite:///{QA_DB.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-photo-submission-secret"
os.environ["UPLOAD_DIR"] = str(QA_UPLOADS)

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import inspect, select, text

from app.database import Base, SessionLocal, engine
from app.schema_migrations import upgrade_database
from app.main import PHOTO_UPLOAD_MAX_BYTES, SESSION_SECRET, hash_password
from app.models import PermissionRole, Puzzle, PuzzleActivityEvent, PuzzleAttempt, PuzzlePhoto, User
from app.permissions import initialize_permissions


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with engine.begin() as connection:
    connection.execute(text("DROP TABLE puzzle_photos"))
    connection.execute(text("""
        CREATE TABLE puzzle_photos (
            id INTEGER NOT NULL PRIMARY KEY,
            puzzle_id VARCHAR NOT NULL REFERENCES puzzles(id),
            user_id INTEGER REFERENCES users(id),
            file_path VARCHAR(500) NOT NULL,
            original_filename VARCHAR(255) NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
assert "attempt_id" not in {column["name"] for column in inspect(engine).get_columns("puzzle_photos")}
upgrade_database()
assert "attempt_id" in {column["name"] for column in inspect(engine).get_columns("puzzle_photos")}
with SessionLocal() as db:
    admin = User(
        display_name="Photo Admin", is_admin=True, is_active=True, session_version=1,
        password_hash=hash_password("Photo-admin-test-123"),
    )
    db.add(admin)
    db.commit()
    initialize_permissions(db)
    player_role = db.scalar(select(PermissionRole).where(PermissionRole.name == "Player"))
    player = User(display_name="Photo Player", is_active=True, session_version=1,
                  roles=[player_role], permissions_initialized=True)
    other = User(display_name="Other Player", is_active=True, session_version=1,
                 roles=[player_role], permissions_initialized=True)
    db.add_all([player, other])
    db.flush()
    ids = {"admin": admin.id, "player": player.id, "other": other.id}
    for puzzle_id in ("photo-one", "photo-delete"):
        db.add(Puzzle(
            id=puzzle_id, puzzle_type="photo_test", title=f"Photo fixture {puzzle_id}",
            theme="", difficulty="easy", prompt="Upload completed work.",
            print_payload={}, play_payload={}, solution_payload={"answer": "done"},
            puzzle_metadata={}, source="test",
            created_by_id=admin.id,
        ))
    db.flush()
    attempt = PuzzleAttempt(
        puzzle_id="photo-one", user_id=player.id, answer="paper copy", is_correct=True,
        attempt_metadata={"source": "photo-qa"},
    )
    db.add(attempt)
    db.flush()
    attempt_id = attempt.id
    db.commit()


def session_cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
BASE = "http://127.0.0.1:8807"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8807"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        contexts = {name: browser.new_context() for name in ids}
        anonymous = browser.new_context()
        for name, user_id in ids.items():
            contexts[name].add_cookies([{"name": "session", "value": session_cookie(user_id), "url": BASE}])
        admin, player, other = (contexts[name] for name in ("admin", "player", "other"))
        for _ in range(100):
            try:
                if anonymous.request.get(BASE + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Photo QA server did not start")

        invalid = player.request.post(BASE + "/api/puzzles/photo-one/photos", multipart={
            "notes": "not really a picture",
            "file": {"name": "fake.jpg", "mimeType": "image/jpeg", "buffer": b"plain text"},
        })
        assert invalid.status == 415, (invalid.status, invalid.text())
        oversized = player.request.post(BASE + "/api/puzzles/photo-one/photos", multipart={
            "file": {
                "name": "large.png", "mimeType": "image/png",
                "buffer": b"\x89PNG\r\n\x1a\n" + b"0" * (PHOTO_UPLOAD_MAX_BYTES + 1),
            },
        })
        assert oversized.status == 413, (oversized.status, oversized.text())
        assert not list(QA_UPLOADS.rglob("*.upload"))
        wrong_attempt = other.request.post(BASE + "/api/puzzles/photo-one/photos", multipart={
            "attempt_id": str(attempt_id),
            "file": {"name": "wrong-owner.png", "mimeType": "image/png", "buffer": PNG},
        })
        assert wrong_attempt.status == 422, (wrong_attempt.status, wrong_attempt.text())

        uploaded = player.request.post(BASE + "/api/puzzles/photo-one/photos", multipart={
            "notes": "Finished on paper",
            "attempt_id": str(attempt_id),
            "file": {"name": "..\\finished-work.png", "mimeType": "application/octet-stream", "buffer": PNG},
        })
        assert uploaded.ok, uploaded.text()
        photo_id = uploaded.json()["id"]
        with SessionLocal() as db:
            photo = db.get(PuzzlePhoto, photo_id)
            assert photo and photo.user_id == ids["player"] and photo.puzzle_id == "photo-one"
            assert photo.attempt_id == attempt_id and photo.attempt.answer == "paper copy"
            assert photo.original_filename == "finished-work.png" and photo.notes == "Finished on paper"
            stored = Path(photo.file_path)
            assert stored.is_file() and stored.suffix == ".png" and stored.parent == QA_UPLOADS / "photo-one"
            event = db.scalar(select(PuzzleActivityEvent).where(
                PuzzleActivityEvent.event_type == "photo_upload",
                PuzzleActivityEvent.user_id == ids["player"],
            ))
            assert event and event.event_metadata["photo_id"] == photo_id

        content_url = f"{BASE}/web/puzzles/photo-one/photos/{photo_id}/content"
        own_content = player.request.get(content_url)
        assert own_content.ok and own_content.body() == PNG
        assert own_content.headers["content-type"].startswith("image/png")
        assert own_content.headers["x-content-type-options"] == "nosniff"
        assert other.request.get(content_url).status == 403
        assert anonymous.request.get(content_url, max_redirects=0).status == 303

        puzzle_page = player.new_page()
        puzzle_page.set_viewport_size({"width": 390, "height": 844})
        assert puzzle_page.goto(BASE + "/puzzles/photo-one", wait_until="domcontentloaded").ok
        assert puzzle_page.locator(".photo-submission-card img").count() == 1
        assert "JPEG, PNG, or WebP" in puzzle_page.locator("form[action$='/photos']").inner_text()
        assert puzzle_page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        puzzle_page.close()

        activity = admin.new_page()
        activity.set_viewport_size({"width": 390, "height": 844})
        assert activity.goto(BASE + "/admin/activity", wait_until="domcontentloaded").ok
        card = activity.locator(f'[data-photo-submission="{photo_id}"]')
        assert card.count() == 1
        assert "Photo Player" in card.inner_text() and "Finished on paper" in card.inner_text()
        assert activity.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        activity.close()

        # Archiving keeps the submission available to authorized activity reviewers.
        assert admin.request.post(BASE + "/admin/puzzles/photo-one/archive").ok
        assert admin.request.get(content_url).ok
        with SessionLocal() as db:
            photo = db.get(PuzzlePhoto, photo_id)
            assert photo and Path(photo.file_path).is_file()

        # A player can delete their own photo and both record and file disappear.
        with SessionLocal() as db:
            db.get(Puzzle, "photo-one").archived_at = None
            photo_path = Path(db.get(PuzzlePhoto, photo_id).file_path)
            db.commit()
        deleted = player.request.post(
            f"{BASE}/web/puzzles/photo-one/photos/{photo_id}/delete", max_redirects=0,
        )
        assert deleted.status == 303 and not photo_path.exists()
        with SessionLocal() as db:
            assert db.get(PuzzlePhoto, photo_id) is None

        reviewed = player.request.post(BASE + "/api/puzzles/photo-one/photos", multipart={
            "file": {"name": "admin-delete.png", "mimeType": "image/png", "buffer": PNG},
        })
        assert reviewed.ok, reviewed.text()
        reviewed_id = reviewed.json()["id"]
        with SessionLocal() as db:
            reviewed_path = Path(db.get(PuzzlePhoto, reviewed_id).file_path)
        assert admin.request.post(
            f"{BASE}/admin/activity/photos/{reviewed_id}/delete", max_redirects=0,
        ).status == 303
        assert not reviewed_path.exists()
        with SessionLocal() as db:
            assert db.get(PuzzlePhoto, reviewed_id) is None

        # Permanent puzzle deletion removes every associated photo file and directory.
        second = player.request.post(BASE + "/api/puzzles/photo-delete/photos", multipart={
            "file": {"name": "delete-me.png", "mimeType": "image/png", "buffer": PNG},
        })
        assert second.ok, second.text()
        delete_dir = QA_UPLOADS / "photo-delete"
        assert delete_dir.is_dir()
        assert admin.request.post(BASE + "/admin/puzzles/photo-delete/delete", max_redirects=0).status == 303
        assert not delete_dir.exists()
        with SessionLocal() as db:
            assert db.get(Puzzle, "photo-delete") is None
            assert db.get(PuzzlePhoto, second.json()["id"]) is None

        browser.close()
    print("Photo validation, private preview, admin review, archive retention, and deletion cleanup passed.")
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    shutil.rmtree(QA_ROOT, ignore_errors=True)
