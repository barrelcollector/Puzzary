"""Isolated lifecycle, access-control and Group Solve coverage for puzzle series."""
import os
from qa_runtime import qa_directory

qa_root = qa_directory("puzzle-series")
os.environ["DATABASE_URL"] = f"sqlite:///{(qa_root / 'series.sqlite').as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-series-secret"
os.environ["UPLOAD_DIR"] = str(qa_root / "uploads")
os.environ["APP_BASE_URL"] = "http://127.0.0.1:8799"

import base64
from datetime import datetime, timedelta, timezone
import html
import json
import re
import subprocess
import sys
import time

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from sqlalchemy import inspect, select

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import GroupMember, GroupSolve, PermissionAudit, Puzzle, PuzzleSeries, PuzzleSeriesEntry, User
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
solution_grid = [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]]
generated = get_puzzle_type("takuzu").manual_builder({
    "solution_grid": solution_grid, "given_count": 0, "seed": "series-qa",
})
with SessionLocal() as db:
    admin = User(display_name="Series Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Account-test-123"))
    player = User(display_name="Series Player", is_active=True, session_version=1,
                  password_hash=hash_password("Account-test-123"))
    second = User(display_name="Series Second", is_active=True, session_version=1,
                  password_hash=hash_password("Account-test-123"))
    db.add_all([admin, player, second]); db.flush()
    admin_id, player_id, second_id = admin.id, player.id, second.id
    for index in range(1, 4):
        db.add(Puzzle(
            id=f"series-puzzle-{index}", puzzle_type="takuzu", title=f"Series Stage {index}",
            theme="series-qa", difficulty="easy", prompt=f"Stage {index} prompt",
            play_payload=generated.play_payload, print_payload=generated.print_payload,
            solution_payload=generated.solution_payload, puzzle_metadata={}, source="test",
            created_by_id=admin_id,
        ))
    db.commit()

# Simulate an existing installation: startup must add the new tables without touching existing rows.
PuzzleSeriesEntry.__table__.drop(engine)
PuzzleSeries.__table__.drop(engine)
assert "puzzle_series" not in inspect(engine).get_table_names()


def session_cookie(user_id):
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def full_answers():
    return {f"{row},{column}": str(value) for row, values in enumerate(solution_grid) for column, value in enumerate(values)}


base = "http://127.0.0.1:8799"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8799"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin = browser.new_context()
        player = browser.new_context()
        second = browser.new_context()
        guest = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        for context, user_id in ((admin, admin_id), (player, player_id), (second, second_id)):
            context.add_cookies([{"name": "session", "value": session_cookie(user_id), "url": base}])
        for _ in range(100):
            try:
                if player.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("QA server did not start")
        assert {"puzzle_series", "puzzle_series_entries"} <= set(inspect(engine).get_table_names())
        with SessionLocal() as db:
            assert db.get(Puzzle, "series-puzzle-1") is not None

        created = admin.request.post(base + "/admin/series", form={
            "title": "Weekend Sequence", "description": "Three ordered challenges",
            "require_previous": "true", "group_progression": "shared",
        }, max_redirects=0)
        assert created.status == 303, created.text()
        series_url = created.headers["location"]
        series_id = series_url.rsplit("/", 1)[-1]
        for index in range(1, 4):
            added = admin.request.post(base + series_url + "/entries", form={
                "puzzle_id": f"series-puzzle-{index}",
                "completion_message": "Proceed to stage two." if index == 1 else "",
            }, max_redirects=0)
            assert added.status == 303, added.text()
        admin_page = admin.new_page()
        assert admin_page.goto(base + series_url).ok
        assert admin_page.locator("[data-series-stage]").count() == 0
        assert admin_page.locator("text=Ordered stages").count() == 1

        first = player.new_page()
        assert first.goto(base + "/puzzles/series-puzzle-1").ok
        assert first.locator('[data-series="%s"]' % series_id).count() == 1
        assert first.locator('[data-series-stage="2"]').get_attribute("data-series-status") == "locked"
        assert first.evaluate("document.documentElement.scrollWidth <= innerWidth")
        locked_web = player.request.get(base + "/puzzles/series-puzzle-2")
        assert locked_web.status == 423 and "Complete" in locked_web.text()
        assert player.request.get(base + "/api/puzzles/series-puzzle-2").status == 423
        assert player.request.get(base + "/api/puzzles/series-puzzle-2/print-preview").status == 423
        assert player.request.post(base + "/web/puzzles/series-puzzle-2/reset", max_redirects=0).status == 423
        assert admin.request.get(base + "/puzzles/series-puzzle-3").status == 200, "Series owner must bypass locks"

        saved = player.request.post(base + "/api/puzzles/series-puzzle-1/progress", data={"answers": full_answers()})
        assert saved.ok and saved.json()["found_count"] == saved.json()["expected_count"]
        assert player.request.get(base + "/puzzles/series-puzzle-2").status == 200
        first.reload()
        assert "Proceed to stage two." in first.locator('[data-series-stage="1"]').inner_text()
        assert first.locator("#completion-message").inner_text() == "Proceed to stage two."

        link_response = admin.request.post(base + "/admin/puzzles/series-puzzle-2/guest-links", form={}, max_redirects=0)
        match = re.search(r'<input[^>]*value="([^"]+)"[^>]*data-guest-link-url', link_response.text())
        assert match
        share_url = html.unescape(match.group(1))
        activation = guest.request.post(share_url, form={"display_name": "Series Guest"}, max_redirects=0)
        assert activation.status == 303
        assert guest.request.get(base + "/puzzles/series-puzzle-2").status == 423, "A puzzle-specific guest link must not inherit another stage's progress"

        with SessionLocal() as db:
            db.add(GroupSolve(
                id="shared-series-group", code="SERIESA", puzzle_id="series-puzzle-1", host_id=second_id,
                participant_limit=4, allow_guests=False, state={"answers": full_answers()},
                cell_versions={}, puzzle_states={}, expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(GroupMember(group_id="shared-series-group", user_id=second_id, status="active"))
            db.commit()
        moved = second.request.post(base + "/api/groups/shared-series-group/puzzle", data={
            "puzzle_id": "series-puzzle-2", "revision": 0,
        })
        assert moved.ok, moved.text()
        assert moved.json()["puzzle_id"] == "series-puzzle-2"

        update = admin.request.post(base + series_url, form={
            "title": "Weekend Sequence", "description": "Three ordered challenges",
            "require_previous": "true", "group_progression": "individual",
        }, max_redirects=0)
        assert update.status == 303
        with SessionLocal() as db:
            db.add(GroupSolve(
                id="individual-series-group", code="SERIESB", puzzle_id="series-puzzle-1", host_id=second_id,
                participant_limit=4, allow_guests=False, state={"answers": full_answers()},
                cell_versions={}, puzzle_states={}, expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(GroupMember(group_id="individual-series-group", user_id=second_id, status="active"))
            db.commit()
        individual = second.request.post(base + "/api/groups/individual-series-group/puzzle", data={
            "puzzle_id": "series-puzzle-2", "revision": 0,
        })
        assert individual.status == 423, individual.text()

        with SessionLocal() as db:
            series = db.get(PuzzleSeries, series_id)
            entries = list(series.entries)
            assert [entry.position for entry in entries] == [1, 2, 3]
            first_entry = next(entry for entry in entries if entry.puzzle_id == "series-puzzle-1")
            third_entry = next(entry for entry in entries if entry.puzzle_id == "series-puzzle-3")
            first_id, third_id = first_entry.id, third_entry.id
        assert admin.request.post(base + f"{series_url}/entries/{third_id}", form={"action": "up"}, max_redirects=0).status == 303
        with SessionLocal() as db:
            series = db.get(PuzzleSeries, series_id)
            assert [entry.puzzle_id for entry in series.entries] == ["series-puzzle-1", "series-puzzle-3", "series-puzzle-2"]
        assert admin.request.post(base + f"{series_url}/entries/{first_id}", form={"action": "remove"}, max_redirects=0).status == 303
        assert second.request.get(base + "/puzzles/series-puzzle-3").status == 200, "Removing a prior stage must recompute the sequence"

        with SessionLocal() as db:
            db.get(Puzzle, "series-puzzle-3").archived_at = datetime.now(timezone.utc)
            db.commit()
        assert second.request.get(base + "/puzzles/series-puzzle-2").status == 200, "Archived stages must be skipped instead of deadlocking a series"
        with SessionLocal() as db:
            db.get(Puzzle, "series-puzzle-3").archived_at = None
            db.commit()

        update = admin.request.post(base + series_url, form={
            "title": "Weekend Sequence", "description": "Open ordered challenges",
            "group_progression": "shared",
        }, max_redirects=0)
        assert update.status == 303
        assert second.request.get(base + "/puzzles/series-puzzle-2").status == 200, "Prerequisite enforcement is off by default/configuration"

        archived = admin.request.post(base + series_url + "/archive", max_redirects=0)
        assert archived.status == 303
        assert player.request.get(base + "/puzzles/series-puzzle-2").status == 200
        with SessionLocal() as db:
            series = db.get(PuzzleSeries, series_id)
            assert series.archived_at is not None
            assert db.scalar(select(PermissionAudit).where(
                PermissionAudit.subject_id == series_id, PermissionAudit.action == "series.created"))
            assert db.scalar(select(PermissionAudit).where(
                PermissionAudit.subject_id == series_id, PermissionAudit.action == "series.archived"))

        browser.close()
        print("Series creation/editing/order, sequential web/API enforcement, owner bypass, stage messages, guest isolation, shared/individual Group Solve progression, removed/archived-stage handling and series archive behavior passed.")
finally:
    server.terminate()
    server.wait(timeout=15)
