"""Isolated progressive Sudoku server, solo, Group Solve, privacy, and mobile QA."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

database = Path(tempfile.gettempdir()) / "puzzary-sudoku-progressive-qa.sqlite"
artifacts = Path(os.environ.get("QA_ARTIFACT_DIR", Path(tempfile.gettempdir()) / "puzzary-sudoku-progressive-artifacts"))
artifacts.mkdir(parents=True, exist_ok=True)
database.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-sudoku-progressive-secret"
os.environ["UPLOAD_DIR"] = str(Path(tempfile.gettempdir()) / "puzzary-sudoku-progressive-uploads")

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import Puzzle, User
import app.puzzles  # Load installed packages before compatibility imports.
from app.puzzle_types.sudoku import build_manual_puzzle, sudoku_collaboration


solution = "1234\n3412\n2143\n4321"
givens = "0,0,1\n1,1,4"
auto_options = {
    "solution_grid": solution,
    "givens": givens,
    "grid_size": "4",
    "progressive_solve": "yes",
    "progressive_units": ["row", "column", "box"],
    "progressive_validation": "automatic",
    "progressive_mark": "yes",
    "progressive_lock": "yes",
    "progressive_order": "any",
}
sequence_options = {
    **auto_options,
    "progressive_units": ["row"],
    "progressive_validation": "manual",
    "progressive_lock": "no",
    "progressive_order": "sequence",
    "progressive_sequence": "row:2,row:1,row:3,row:4",
}
auto = build_manual_puzzle(auto_options)
sequence = build_manual_puzzle(sequence_options)
assert auto.play_payload["settings"]["progressive"] == {
    "enabled": True,
    "units": ["row", "column", "box"],
    "validation": "automatic",
    "mark_completed": True,
    "lock_completed": True,
    "order": "any",
    "sequence": [],
}
assert sequence.play_payload["settings"]["progressive"]["sequence"] == ["row:2", "row:1", "row:3", "row:4"]
try:
    build_manual_puzzle({**sequence_options, "progressive_sequence": "row:1,row:1"})
    raise AssertionError("Invalid sequence accepted")
except ValueError as error:
    assert "every selected unit" in str(error) or "duplicate" in str(error)


def patch(state: dict, item, changes: dict[str, str], actor: int = 7) -> dict:
    return sudoku_collaboration(state, {"kind": "patch", "changes": changes, "_actor": actor}, item.solution_payload)


# A full wrong unit is silent and blocked from repeated full-grid probing until made incomplete.
state = patch({}, auto, {"1,0": "3", "2,0": "2", "3,0": "4"})
assert state["progressive_completed"] == [] and "row:1" in state["progressive_blocked"]
assert state["incorrect"] == [] and "matched_count" not in state
state = patch(state, auto, {"1,0": "2", "2,0": "3"})
assert "row:1" not in state["progressive_completed"], "A full blocked unit was re-tested as an oracle"
state = patch(state, auto, {"1,0": ""})
assert "row:1" not in state["progressive_blocked"]
state = patch(state, auto, {"1,0": "2"})
assert "row:1" in state["progressive_completed"] and state["progressive_authors"]["row:1"] == 7
try:
    patch(state, auto, {"2,0": "4"})
    raise AssertionError("Locked unit accepted an edit")
except ValueError as error:
    assert "locked" in str(error).lower()

# Overlapping rows, columns, and boxes all complete from the same authoritative grid.
remaining = {
    f"{x},{y}": str(value)
    for y, row in enumerate(auto.solution_payload["grid"])
    for x, value in enumerate(row)
    if f"{x},{y}" not in auto.solution_payload["givens"] and y != 0
}
state = patch(state, auto, remaining, actor=8)
assert state["completed"] and len(state["progressive_completed"]) == 12
assert {"row:4", "column:4", "box:4"} <= set(state["progressive_completed"])

# A manual ordered puzzle ignores a correct later unit until the active unit is ready.
ordered = patch({}, sequence, {"1,0": "2", "2,0": "3", "3,0": "4"})
ordered = sudoku_collaboration(ordered, {"kind": "progressive_check", "changes": {}, "_actor": 7}, sequence.solution_payload)
assert ordered["progressive_completed"] == [] and ordered["progressive_active"] == "row:2"
ordered = patch(ordered, sequence, {"0,1": "3", "2,1": "1", "3,1": "2"})
ordered = sudoku_collaboration(ordered, {"kind": "progressive_check", "changes": {}, "_actor": 8}, sequence.solution_payload)
assert ordered["progressive_completed"][:2] == ["row:1", "row:2"]
assert ordered["progressive_active"] == "row:3"

Base.metadata.create_all(engine)
with SessionLocal() as db:
    host = User(display_name="Progress Host", is_admin=True, is_active=True, session_version=1,
                password_hash=hash_password("Progressive-test-123"))
    player = User(display_name="Progress Player", is_active=True, session_version=1,
                  password_hash=hash_password("Progressive-test-123"))
    db.add_all([host, player]); db.flush(); host_id, player_id = host.id, player.id
    for puzzle_id, item in (("progress-auto", auto), ("progress-sequence", sequence), ("progress-group", auto)):
        db.add(Puzzle(
            id=puzzle_id, puzzle_type="sudoku", title=puzzle_id.replace("-", " ").title(), theme="",
            difficulty="easy", prompt=item.prompt, play_payload=item.play_payload,
            print_payload=item.print_payload, solution_payload=item.solution_payload,
            puzzle_metadata={}, source="test", created_by_id=host_id,
        ))
    db.commit()


def cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def group_action(context, endpoint: str, puzzle_id: str, revision: int, changes: dict[str, str]) -> dict:
    response = context.request.post(endpoint + "/actions", data={
        "action_id": str(uuid.uuid4()), "revision": revision, "puzzle_id": puzzle_id,
        "kind": "patch", "target": "", "changes": changes,
    })
    assert response.ok, response.text()
    return response.json()


base = "http://127.0.0.1:8795"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8795"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        host_context = browser.new_context(viewport={"width": 1280, "height": 1000})
        player_context = browser.new_context(viewport={"width": 390, "height": 844})
        host_context.add_cookies([{"name": "session", "value": cookie(host_id), "url": base}])
        player_context.add_cookies([{"name": "session", "value": cookie(player_id), "url": base}])
        for _ in range(100):
            try:
                if host_context.request.get(base + "/health").ok: break
            except Exception: time.sleep(.1)
        else: raise RuntimeError("QA server failed to start")

        public = host_context.request.get(base + "/api/puzzles/progress-auto")
        assert public.ok
        encoded_public = json.dumps(public.json())
        assert "solution_payload" not in encoded_public and '"progressive"' in encoded_public

        solo = host_context.new_page()
        solo.goto(base + "/puzzles/progress-auto")
        assert solo.locator(".sudoku-unit").count() == 12
        for key, value in {"1,0": "3", "2,0": "2", "3,0": "4"}.items(): solo.locator(f'[data-key="{key}"]').fill(value)
        time.sleep(.5)
        assert not solo.locator('[data-unit="row:1"]').get_attribute("class").endswith("complete")
        solo.locator('[data-key="1,0"]').fill("2"); solo.locator('[data-key="2,0"]').fill("3")
        time.sleep(.5)
        assert "complete" not in (solo.locator('[data-unit="row:1"]').get_attribute("class") or "")
        solo.locator('[data-key="1,0"]').fill("")
        time.sleep(.4)
        solo.locator('[data-key="1,0"]').fill("2")
        solo.locator('[data-unit="row:1"]').wait_for(state="visible")
        solo.wait_for_function("document.querySelector('[data-unit=\"row:1\"]').classList.contains('complete')")
        assert solo.locator('[data-key="2,0"]').is_editable() is False
        solo.reload()
        assert "complete" in (solo.locator('[data-unit="row:1"]').get_attribute("class") or "")
        assert solo.locator('[data-key="2,0"]').is_editable() is False

        editor = host_context.new_page()
        editor.goto(base + "/generate")
        editor.locator('[data-puzzle-type]').select_option('sudoku')
        editor.locator('[data-wizard-next]').click()
        editor.locator('[data-wizard-next]').click()
        editor.locator('[name="gen_progressive_solve"]').select_option('yes')
        preview = editor.locator('[data-sudoku-progressive-preview]')
        assert preview.is_visible() and "Progressive solve preview" in preview.inner_text()
        editor.screenshot(path=str(artifacts / "sudoku-progressive-editor.png"), full_page=True)

        created = host_context.request.post(base + "/api/puzzles/progress-group/groups", data={})
        assert created.ok, created.text()
        group = created.json(); endpoint = base + "/api/groups/" + group["id"]
        assert player_context.request.post(base + "/api/groups/join", data={"code": group["code"]}).ok
        first = group_action(host_context, endpoint, "progress-group", group["revision"], {"1,0": "2"})
        second = group_action(player_context, endpoint, "progress-group", first["revision"], {"2,0": "3", "3,0": "4"})
        assert second["state"]["progressive_completed"] == ["row:1"]
        assert second["state"]["progressive_authors"]["row:1"] == player_id
        assert second["state"]["incorrect"] == []
        assert "grid" not in second and "solution" not in json.dumps(second).lower()

        host_page = host_context.new_page(); player_page = player_context.new_page()
        host_page.goto(base + group["url"]); player_page.goto(base + group["url"])
        for page in (host_page, player_page):
            page.wait_for_function("document.querySelector('[data-unit=\"row:1\"]').classList.contains('complete')")
            assert "Progress Player" in page.locator('[data-unit="row:1"]').inner_text()
        player_page.reload()
        player_page.wait_for_function("document.querySelector('[data-unit=\"row:1\"]').classList.contains('complete')")
        assert player_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert player_page.locator('[data-key="2,0"]').get_attribute("aria-label") == "Row 1, column 3"
        player_page.screenshot(path=str(artifacts / "sudoku-progressive-mobile-group.png"), full_page=True)
        browser.close()
finally:
    server.terminate()
    try: server.wait(timeout=5)
    except subprocess.TimeoutExpired: server.kill()
    engine.dispose()
    try: database.unlink(missing_ok=True)
    except PermissionError: pass

print("Progressive Sudoku configuration, sequencing, anti-oracle checks, locking, overlap, solo persistence, Group Solve attribution, reconnect, author preview and mobile accessibility passed.")
