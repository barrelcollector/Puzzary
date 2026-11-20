"""Isolated security and UX coverage for per-puzzle access passwords."""
import os
from qa_runtime import qa_directory

qa_root = qa_directory("puzzle-passwords")
os.environ["DATABASE_URL"] = f"sqlite:///{(qa_root / 'puzzle-passwords.sqlite').as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-puzzle-password-secret"
os.environ["UPLOAD_DIR"] = str(qa_root / "uploads")

import base64
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import time

from fastapi import HTTPException
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import inspect

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import Puzzle, User
from app.publishing import validate_puzzle
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
assert "access_password_hash" in {column["name"] for column in inspect(engine).get_columns("puzzles")}
with SessionLocal() as db:
    admin = User(display_name="Password Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Account-test-123"))
    player = User(display_name="Password Player", is_admin=False, is_active=True, session_version=1,
                  password_hash=hash_password("Account-test-123"))
    db.add_all([admin, player])
    db.flush()
    admin_id, player_id = admin.id, player.id
    generated = get_puzzle_type("takuzu").manual_builder({
        "solution_grid": [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
        "given_count": 4,
        "seed": "puzzle-password-qa",
    })
    for puzzle_id, title, password_hash, created_at in [
        ("password-public", "Older Public Puzzle", None, datetime.now(timezone.utc) - timedelta(minutes=2)),
        ("password-locked", "Locked Quasar Puzzle", hash_password("Puzzle-Alpha-123"), datetime.now(timezone.utc)),
        ("password-rate", "Rate Limited Puzzle", hash_password("Puzzle-Rate-123"), datetime.now(timezone.utc) - timedelta(minutes=1)),
    ]:
        db.add(Puzzle(
            id=puzzle_id, puzzle_type="takuzu", title=title, theme="password-qa", difficulty="easy",
            prompt="Sensitive prompt", play_payload=generated.play_payload, print_payload=generated.print_payload,
            solution_payload=generated.solution_payload, puzzle_metadata={}, source="test", created_by_id=admin_id,
            access_password_hash=password_hash, created_at=created_at,
        ))
    db.commit()


def session_cookie(user_id):
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


base = "http://127.0.0.1:8797"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8797"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin = browser.new_context()
        player = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        locked_api = browser.new_context()
        anonymous = browser.new_context()
        limiter = browser.new_context()
        admin.add_cookies([{"name": "session", "value": session_cookie(admin_id), "url": base}])
        player.add_cookies([{"name": "session", "value": session_cookie(player_id), "url": base}])
        locked_api.add_cookies([{"name": "session", "value": session_cookie(player_id), "url": base}])
        limiter.add_cookies([{"name": "session", "value": session_cookie(player_id), "url": base}])
        for _ in range(100):
            try:
                if anonymous.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("QA server did not start")

        library = locked_api.request.get(base + "/api/puzzles?q=quasar").json()
        assert library["items"][0]["password_protected"] is True
        assert library["items"][0]["preview"]["kind"] == "locked"
        assert "solution_grid" not in json.dumps(library) and "Sensitive prompt" not in json.dumps(library)
        latest = locked_api.request.get(base + "/api/puzzles/latest").json()
        assert latest["id"] == "password-public"

        locked = locked_api.request.get(base + "/api/puzzles/password-locked")
        assert locked.status == 423 and "solution_grid" not in locked.text()
        wrong = locked_api.request.get(base + "/api/puzzles/password-locked", headers={"X-Puzzle-Password": "wrong"})
        assert wrong.status == 423 and "Alpha" not in wrong.text()
        unlocked = locked_api.request.get(base + "/api/puzzles/password-locked", headers={"X-Puzzle-Password": "Puzzle-Alpha-123"})
        assert unlocked.ok and unlocked.json()["password_protected"] is True
        assert locked_api.request.get(base + "/api/puzzles/password-locked/progress").ok
        assert locked_api.request.get(base + "/api/puzzles/password-locked/print-preview").ok

        page = player.new_page()
        response = page.goto(base + "/puzzles/password-locked")
        assert response.status == 423 and page.locator("form[action$='/unlock']").count() == 1
        assert "Sensitive prompt" not in page.locator("body").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("input[name=password]").fill("not-it")
        page.locator("form[action$='/unlock'] button[type=submit]").click()
        assert "not accepted" in page.locator("[role=alert]").inner_text()
        page.locator("input[name=password]").fill("Puzzle-Alpha-123")
        page.locator("form[action$='/unlock'] button[type=submit]").click()
        page.wait_for_url("**/puzzles/password-locked")
        assert "Sensitive prompt" in page.locator("body").inner_text()
        assert "Locked Quasar Puzzle" in page.locator("body").inner_text()

        protected_hash = None
        with SessionLocal() as db:
            protected_hash = db.get(Puzzle, "password-locked").access_password_hash
            assert protected_hash and "Puzzle-Alpha-123" not in protected_hash
            try:
                validate_puzzle(db, "password-locked", publishing=True)
                raise AssertionError("Publishing accepted a protected puzzle")
            except HTTPException as error:
                assert error.status_code == 422 and "password" in str(error.detail).lower()
        exported = admin.request.get(base + "/api/puzzles/password-locked/export").json()
        assert protected_hash not in json.dumps(exported) and "access_password_hash" not in json.dumps(exported)

        group = admin.request.post(
            base + "/api/puzzles/password-locked/groups",
            headers={"X-Puzzle-Password": "Puzzle-Alpha-123"},
            data={"allow_guests": True, "password": "Session-Beta-123", "public_lobby": True},
        )
        assert group.ok, group.text()
        group_data = group.json()
        lobby = anonymous.request.get(base + "/api/groups/lobby").json()["groups"]
        listed = next(item for item in lobby if item["id"] == group_data["id"])
        assert listed["password_protected"] and listed["puzzle_password_protected"]
        guest_page = anonymous.new_page()
        guest_page.goto(base + group_data["invite_url"])
        assert guest_page.locator("input[name=password]").count() == 1
        assert guest_page.locator("input[name=puzzle_password]").count() == 1
        bad_puzzle = anonymous.request.post(base + f"/api/groups/{group_data['id']}/guests", data={
            "name": "Guest One", "password": "Session-Beta-123", "puzzle_password": "wrong",
        })
        assert bad_puzzle.status == 423
        bad_session = anonymous.request.post(base + f"/api/groups/{group_data['id']}/guests", data={
            "name": "Guest One", "password": "wrong", "puzzle_password": "Puzzle-Alpha-123",
        })
        assert bad_session.status == 401
        guest_join = anonymous.request.post(base + f"/api/groups/{group_data['id']}/guests", data={
            "name": "Guest One", "password": "Session-Beta-123", "puzzle_password": "Puzzle-Alpha-123",
        })
        assert guest_join.ok, guest_join.text()
        assert anonymous.request.get(base + f"/api/groups/{group_data['id']}").ok

        rotate = admin.request.post(base + "/admin/puzzles/password-locked/metadata", form={
            "category_id": 0, "completion_message": "", "visibility": "public",
            "access_password": "Puzzle-Gamma-456",
        }, max_redirects=0)
        assert rotate.status == 303
        assert player.request.get(base + "/api/puzzles/password-locked").status == 423
        assert locked_api.request.get(base + "/api/puzzles/password-locked").status == 423
        assert anonymous.request.get(base + f"/api/groups/{group_data['id']}").status == 423
        assert player.request.get(base + "/api/puzzles/password-locked", headers={
            "X-Puzzle-Password": "Puzzle-Gamma-456",
        }).ok

        statuses = []
        for _ in range(6):
            statuses.append(limiter.request.get(base + "/api/puzzles/password-rate", headers={
                "X-Puzzle-Password": "incorrect",
            }).status)
        assert statuses[:5] == [423] * 5 and statuses[5] == 429, statuses

        remove = admin.request.post(base + "/admin/puzzles/password-locked/metadata", form={
            "category_id": 0, "completion_message": "", "visibility": "public",
            "remove_access_password": "true",
        }, max_redirects=0)
        assert remove.status == 303
        assert locked_api.request.get(base + "/api/puzzles/password-locked").ok
        with SessionLocal() as db:
            assert db.get(Puzzle, "password-locked").access_password_hash is None

        browser.close()
        print("Puzzle password hashing, clue-free discovery, web/API unlocking, rate limiting, rotation/removal, print/export/publishing protection and dual-password guest joins passed.")
finally:
    server.terminate()
    server.wait(timeout=15)
