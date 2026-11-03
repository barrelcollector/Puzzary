"""Isolated migration, access, lifecycle, UI and restart coverage for issue #83."""
import os

os.environ["DATABASE_URL"] = "sqlite:///C:/Temp/puzzary-scheduled-qa.sqlite"
os.environ["SESSION_SECRET"] = "isolated-scheduled-secret"
os.environ["UPLOAD_DIR"] = "C:/Temp/puzzary-scheduled-uploads"
os.environ["PUZZARY_TIMEZONE"] = "America/New_York"

import base64
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import time

from fastapi import HTTPException
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import inspect, select, text

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password, normalize_publish_at, parse_local_datetime
from app.models import PermissionAudit, Puzzle, User
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
generated = get_puzzle_type("takuzu").manual_builder({
    "solution_grid": [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
    "given_count": 0,
    "seed": "scheduled-qa",
})
with SessionLocal() as db:
    admin = User(display_name="Schedule Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Account-test-123"))
    player = User(display_name="Schedule Player", is_active=True, session_version=1,
                  password_hash=hash_password("Account-test-123"))
    db.add_all([admin, player]); db.flush()
    admin_id, player_id = admin.id, player.id
    db.add(Puzzle(
        id="legacy-before-schedule", puzzle_type="takuzu", title="Legacy Puzzle",
        theme="migration", difficulty="easy", prompt=generated.prompt,
        play_payload=generated.play_payload, print_payload=generated.print_payload,
        solution_payload=generated.solution_payload, puzzle_metadata={}, source="test",
        created_by_id=admin_id,
    ))
    db.commit()

# Simulate an installation created before #83. Startup must add the column in place.
with engine.begin() as connection:
    connection.execute(text("DROP INDEX ix_puzzles_publish_at"))
    connection.execute(text("ALTER TABLE puzzles DROP COLUMN publish_at"))
assert "publish_at" not in {column["name"] for column in inspect(engine).get_columns("puzzles")}


def session_cookie(user_id):
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def start_server():
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8803"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_for_server(context):
    for _ in range(120):
        try:
            if context.request.get(base + "/health").ok:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("QA server did not start")


base = "http://127.0.0.1:8803"
server = start_server()
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin_context = browser.new_context(viewport={"width": 1280, "height": 900})
        player_context = browser.new_context()
        anonymous = browser.new_context()
        admin_context.add_cookies([{"name": "session", "value": session_cookie(admin_id), "url": base}])
        player_context.add_cookies([{"name": "session", "value": session_cookie(player_id), "url": base}])
        wait_for_server(anonymous)

        assert "publish_at" in {column["name"] for column in inspect(engine).get_columns("puzzles")}
        with SessionLocal() as db:
            assert db.get(Puzzle, "legacy-before-schedule") is not None

        release = datetime.now(timezone.utc) + timedelta(days=3)
        created = admin_context.request.post(base + "/api/puzzles/generate", data={
            "puzzle_type": "takuzu", "theme": "scheduled", "difficulty": "easy",
            "seed": "scheduled-public", "publish_at": release.isoformat(),
        })
        assert created.ok, created.text()
        scheduled_id = created.json()["id"]
        assert created.json()["publication_state"] == "scheduled"
        assert created.json()["publish_at"].startswith(release.isoformat()[:16])

        player_library = json.dumps(player_context.request.get(base + "/api/puzzles").json())
        assert scheduled_id not in player_library
        assert player_context.request.get(base + f"/api/puzzles/{scheduled_id}").status == 403
        assert player_context.request.get(base + f"/puzzles/{scheduled_id}").status == 403
        assert anonymous.request.get(base + f"/puzzles/{scheduled_id}/qr.svg").status == 404
        assert admin_context.request.get(base + f"/puzzles/{scheduled_id}").status == 200

        preview = admin_context.new_page()
        preview.goto(base + f"/puzzles/{scheduled_id}")
        assert "Scheduled preview" in preview.locator("body").inner_text()
        admin_page = admin_context.new_page()
        admin_page.goto(base + "/admin/puzzles")
        panel = admin_page.locator(".admin-puzzle", has=admin_page.locator(f"text={scheduled_id}"))
        assert panel.locator(".status-pill", has_text="Scheduled").count() == 1
        assert panel.locator('input[name="publish_at"]').count() == 1
        assert "America/New_York" in panel.inner_text()
        for path in ("/generate", "/admin/generate", "/admin/puzzles/manual"):
            page = admin_context.new_page()
            assert page.goto(base + path).ok
            assert page.locator('input[name="publish_at"]').count() == 1
            assert "America/New_York" in page.locator('input[name="publish_at"]').get_attribute("data-timezone")
            page.close()

        protected = admin_context.request.post(base + "/api/puzzles/generate", data={
            "puzzle_type": "takuzu", "theme": "scheduled-private", "difficulty": "easy",
            "seed": "scheduled-private", "publish_at": release.isoformat(),
            "visibility": "unlisted", "access_password": "Schedule-Test-123",
        })
        assert protected.ok, protected.text()
        protected_id = protected.json()["id"]
        assert protected_id not in json.dumps(player_context.request.get(base + "/api/puzzles").json())

        series = admin_context.request.post(base + "/admin/series", form={
            "title": "Scheduled sequence", "require_previous": "true", "group_progression": "shared",
        }, max_redirects=0)
        assert series.status == 303
        series_path = series.headers["location"]
        for puzzle_id in (scheduled_id, "legacy-before-schedule"):
            added = admin_context.request.post(base + series_path + "/entries", form={"puzzle_id": puzzle_id}, max_redirects=0)
            assert added.status == 303, added.text()
        assert player_context.request.get(base + "/puzzles/legacy-before-schedule").status == 423

        group = admin_context.request.post(base + f"/api/puzzles/{scheduled_id}/groups", data={})
        assert group.status == 409 and "publish" in group.text().lower()
        guest_link = admin_context.request.post(base + f"/admin/puzzles/{scheduled_id}/guest-links", form={}, max_redirects=0)
        assert guest_link.status == 409
        issue = admin_context.request.post(base + "/api/issues", data={
            "title": "Scheduled leak check",
            "modules": [{"kind": "puzzle", "puzzle_id": scheduled_id}],
        })
        assert issue.status == 201, issue.text()
        rejected_issue = admin_context.request.post(base + f"/api/issues/{issue.json()['id']}/publish")
        assert rejected_issue.status == 422

        later = release + timedelta(days=1)
        rescheduled = admin_context.request.post(base + f"/admin/puzzles/{scheduled_id}/metadata", form={
            "category_id": "0", "completion_message": "", "visibility": "public",
            "publish_at": later.astimezone().strftime("%Y-%m-%dT%H:%M"),
        }, max_redirects=0)
        assert rescheduled.status == 303, rescheduled.text()
        canceled = admin_context.request.post(base + f"/admin/puzzles/{scheduled_id}/metadata", form={
            "category_id": "0", "completion_message": "", "visibility": "public", "publish_at": "",
        }, max_redirects=0)
        assert canceled.status == 303
        with SessionLocal() as db:
            puzzle = db.get(Puzzle, scheduled_id)
            assert puzzle.puzzle_metadata["draft"] is True and puzzle.publish_at is None
            actions = db.scalars(select(PermissionAudit.action).where(PermissionAudit.subject_id == scheduled_id)).all()
            assert {"puzzle.scheduled", "puzzle.rescheduled", "puzzle.schedule_canceled"} <= set(actions)

        scheduled_again = admin_context.request.post(base + f"/admin/puzzles/{scheduled_id}/metadata", form={
            "category_id": "0", "completion_message": "", "visibility": "public",
            "publish_at": release.astimezone().strftime("%Y-%m-%dT%H:%M"),
        }, max_redirects=0)
        assert scheduled_again.status == 303
        with SessionLocal() as db:
            puzzle = db.get(Puzzle, scheduled_id)
            puzzle.publish_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            private_puzzle = db.get(Puzzle, protected_id)
            private_puzzle.publish_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()

        # No job or browser action publishes it: crossing the timestamp changes effective state.
        assert scheduled_id in json.dumps(player_context.request.get(base + "/api/puzzles").json())
        assert player_context.request.get(base + f"/puzzles/{scheduled_id}").status == 200
        assert admin_context.request.post(base + f"/api/puzzles/{scheduled_id}/groups", data={}).ok
        assert protected_id not in json.dumps(player_context.request.get(base + "/api/puzzles").json())
        assert player_context.request.get(base + f"/puzzles/{protected_id}").status == 423
        allowed_link = admin_context.request.post(base + f"/admin/puzzles/{scheduled_id}/guest-links", form={}, max_redirects=0)
        assert allowed_link.status == 201

        # An overdue schedule remains published after a full application restart.
        server.terminate(); server.wait(timeout=15)
        server = start_server(); wait_for_server(anonymous)
        assert player_context.request.get(base + f"/puzzles/{scheduled_id}").status == 200

        # Offset-bearing timestamps distinguish both sides of the repeated fall-back hour.
        first = normalize_publish_at(datetime.fromisoformat("2026-11-01T01:30:00-04:00"))
        second = normalize_publish_at(datetime.fromisoformat("2026-11-01T01:30:00-05:00"))
        assert first == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        assert second == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
        try:
            parse_local_datetime("2026-03-08T02:30", "Publication date/time")
            raise AssertionError("DST gap was accepted")
        except HTTPException as error:
            assert error.status_code == 422 and "daylight-saving" in str(error.detail)

        browser.close()
        print("Scheduled publication migration, permissions, discovery isolation, UI, audit, automatic release, integrations, restart and DST checks passed.")
finally:
    if server.poll() is None:
        server.terminate()
        server.wait(timeout=15)
