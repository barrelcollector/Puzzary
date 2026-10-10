"""Isolated API/browser regression coverage for public and unlisted puzzle visibility."""
import os

os.environ["DATABASE_URL"] = "sqlite:////tmp/puzzary-unlisted-qa.sqlite"
os.environ["SESSION_SECRET"] = "isolated-unlisted-secret"
os.environ["UPLOAD_DIR"] = "/tmp/puzzary-unlisted-uploads"

import base64
from datetime import datetime, timedelta, timezone
import json
import subprocess
import time

from fastapi import HTTPException
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password, import_portable_puzzles, portable_puzzle_item
from app.models import Puzzle, User
from app.publishing import validate_puzzle
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(display_name="Visibility Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Account-test-123"))
    player = User(display_name="Visibility Player", is_admin=False, is_active=True, session_version=1,
                  password_hash=hash_password("Account-test-123"))
    db.add_all([admin, player])
    db.flush()
    admin_id, player_id = admin.id, player.id
    generated = get_puzzle_type("takuzu").manual_builder({
        "solution_grid": [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
        "given_count": 0,
        "seed": "unlisted-qa",
    })
    older = datetime.now(timezone.utc) - timedelta(minutes=1)
    for puzzle_id, title, metadata, created_at in [
        ("visibility-public", "Discoverable Zebra Puzzle", {"visibility": "public"}, older),
        ("visibility-unlisted", "Unlisted Quasar Puzzle", {"visibility": "unlisted"}, datetime.now(timezone.utc)),
    ]:
        db.add(Puzzle(
            id=puzzle_id, puzzle_type="takuzu", title=title, theme="visibility-qa", difficulty="easy",
            prompt=generated.prompt, play_payload=generated.play_payload, print_payload=generated.print_payload,
            solution_payload=generated.solution_payload, puzzle_metadata=metadata, source="test",
            created_by_id=admin_id, created_at=created_at,
        ))
    db.commit()


def session_cookie(user_id):
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


base = "http://127.0.0.1:8796"
server = subprocess.Popen(
    ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8796"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin = browser.new_context(viewport={"width": 1280, "height": 900})
        player = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        anonymous = browser.new_context()
        admin.add_cookies([{"name": "session", "value": session_cookie(admin_id), "url": base}])
        player.add_cookies([{"name": "session", "value": session_cookie(player_id), "url": base}])
        for _ in range(100):
            try:
                if anonymous.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("QA server did not start")

        library = player.request.get(base + "/api/puzzles").json()
        serialized = json.dumps(library)
        assert "Discoverable Zebra Puzzle" in serialized
        assert "Unlisted Quasar Puzzle" not in serialized and "visibility-unlisted" not in serialized
        assert "Unlisted Quasar Puzzle" not in player.request.get(base + "/api/puzzles?q=quasar").text()
        latest = player.request.get(base + "/api/puzzles/latest").json()
        assert latest["id"] == "visibility-public", latest

        direct = player.request.get(base + "/api/puzzles/visibility-unlisted")
        assert direct.ok and direct.json()["visibility"] == "unlisted"
        page = player.new_page()
        assert page.goto(base + "/puzzles/visibility-unlisted").ok
        assert "Unlisted Quasar Puzzle" in page.locator("body").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        anon_direct = anonymous.request.get(base + "/puzzles/visibility-unlisted", max_redirects=0)
        assert anon_direct.status in (302, 303)
        anonymous_qr = anonymous.request.get(base + "/puzzles/visibility-unlisted/qr.svg")
        assert anonymous_qr.status == 404, (anonymous_qr.status, anonymous_qr.text())
        assert player.request.get(base + "/puzzles/visibility-unlisted/qr.svg").ok
        assert player.request.get(base + "/puzzles/visibility-unlisted/qr.svg").headers["cache-control"].startswith("private")
        anonymous_short = anonymous.request.get(base + "/q/visibility-unlisted", max_redirects=0)
        assert anonymous_short.status == 404, (anonymous_short.status, anonymous_short.text())

        admin_page = admin.new_page()
        admin_page.goto(base + "/admin/puzzles")
        unlisted_panel = admin_page.locator(".admin-puzzle", has_text="Unlisted Quasar Puzzle")
        assert unlisted_panel.locator(".status-pill", has_text="Unlisted").count() == 1
        assert unlisted_panel.locator("select[name=visibility]").input_value() == "unlisted"
        assert admin_page.locator("body").inner_text().find("Unlisted Quasar Puzzle") >= 0

        group = admin.request.post(base + "/api/puzzles/visibility-unlisted/groups", data={
            "allow_guests": True, "public_lobby": True,
        })
        assert group.ok, group.text()
        snapshot = group.json()
        assert snapshot["allow_guests"] is False and snapshot["public_lobby"] is True
        assert snapshot["id"] not in json.dumps(anonymous.request.get(base + "/api/groups/lobby").json())
        guest = anonymous.request.post(base + "/api/groups/" + snapshot["id"] + "/guests", data={"name": "No Access"})
        assert guest.status == 403

        export = admin.request.get(base + "/api/puzzles/visibility-unlisted/export").json()
        assert export["metadata"]["visibility"] == "unlisted"
        with SessionLocal() as db:
            copied = dict(export)
            copied["origin"] = {"id": "visibility-import-origin"}
            result = import_portable_puzzles(db, [copied], creator=db.get(User, admin_id), dry_run=False, duplicate_policy="copy")
            assert result["imported"] == 1, result
            imported_id = result["items"][0]["local_id"]
            assert db.get(Puzzle, imported_id).puzzle_metadata["visibility"] == "unlisted"
            try:
                validate_puzzle(db, "visibility-unlisted", publishing=True)
                raise AssertionError("Publishing accepted an unlisted puzzle")
            except HTTPException as error:
                assert error.status_code == 422 and "public" in str(error.detail).lower()
            assert portable_puzzle_item(db.get(Puzzle, "visibility-unlisted"), db)["metadata"]["visibility"] == "unlisted"

        update = admin.request.post(base + "/admin/puzzles/visibility-unlisted/metadata", form={
            "category_id": 0, "completion_message": "", "visibility": "public",
        }, max_redirects=0)
        assert update.status == 303
        assert "visibility-unlisted" in json.dumps(player.request.get(base + "/api/puzzles?q=quasar").json())
        restore = admin.request.post(base + "/admin/puzzles/visibility-unlisted/metadata", form={
            "category_id": 0, "completion_message": "", "visibility": "unlisted",
        }, max_redirects=0)
        assert restore.status == 303, restore.text()
        assert "visibility-unlisted" not in json.dumps(player.request.get(base + "/api/puzzles?q=quasar").json())

        browser.close()
        print("Unlisted discovery exclusion, signed-in direct access, private QR, admin controls, Group Solve guest/lobby isolation, portable metadata and publishing rejection passed.")
finally:
    server.terminate()
    server.wait(timeout=15)
