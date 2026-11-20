"""Regression coverage for Word Search penalties (#70) and intentional decoys (#71)."""
import os
from qa_runtime import qa_directory

qa_root = qa_directory("word-search-decoys")
os.environ["DATABASE_URL"] = f"sqlite:///{(qa_root / 'word-search.sqlite').as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-word-search-decoy-secret"
os.environ["UPLOAD_DIR"] = str(qa_root / "uploads")

import base64
from copy import deepcopy
import json
import subprocess
import sys
import time
import uuid

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import Puzzle, User
from app.puzzles import get_puzzle_type
from app.puzzle_types.word_search import placement_cells, placement_signature, word_occurrences


plugin = get_puzzle_type("word_search")
source_words = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL", "INDIA", "JULIET"]
generated = plugin.generator("decoys", "hard", source_words, "decoy-generation-qa", {
    "grid_size": "14", "word_count_min": "4", "word_count_limit": "4",
    "directions": "all", "false_word_count": "2", "near_miss_count": "2",
    "decoy_difficulty": "hard", "decoy_placement": "crossings",
    "penalty_mode": "score_deduction", "penalty_value": "7", "penalty_scope": "group",
})
assert len(generated.solution_payload["decoys"]) == 4
assert "decoys" not in generated.play_payload
assert generated.play_payload["settings"]["decoy_count"] == 4
for placement in generated.solution_payload["placements"]:
    occurrences = word_occurrences(generated.play_payload["grid"], placement["word"])
    assert {placement_signature(item) for item in occurrences} == {placement_signature(placement)}
for decoy in generated.solution_payload["decoys"]:
    occurrences = word_occurrences(generated.play_payload["grid"], decoy["text"])
    assert len(occurrences) == 1
    if decoy["kind"] == "near_miss":
        target = decoy["target_word"]
        assert len(decoy["text"]) == len(target) - 1
        assert target[:decoy["omitted_index"]] + target[decoy["omitted_index"] + 1:] == decoy["text"]

manual = plugin.manual_builder({
    "grid": "ALPHAXX\nBRAVOXX\nDELTAXX\nALHAXXX\nXXXXXXX\nXXXXXXX\nXXXXXXX",
    "words": "ALPHA\nBRAVO", "false_words": "DELTA", "near_misses": "ALPHA -> ALHA",
    "auto_confirm": "yes", "penalty_mode": "limited_attempts", "attempt_limit": "2",
    "penalty_scope": "individual",
})
assert {item["kind"] for item in manual.solution_payload["decoys"]} == {"false_word", "near_miss"}
reverse_diagonal = plugin.manual_builder({
    "grid": "ALPHAXX\nATLEDXX\nAXXXXXX\nXLXXXXX\nXXHXXXX\nXXXAXXX\nXXXXXXX",
    "words": "ALPHA", "false_words": "DELTA", "near_misses": "ALPHA -> ALHA",
})
reverse_false = next(item for item in reverse_diagonal.solution_payload["decoys"] if item["kind"] == "false_word")
diagonal_near = next(item for item in reverse_diagonal.solution_payload["decoys"] if item["kind"] == "near_miss")
assert reverse_false["direction"] == [-1, 0]
assert diagonal_near["direction"] == [1, 1]
overlapping = plugin.manual_builder({
    "grid": "ALPHAXX\nXXLXXXX\nXXAXXXX\nXXNXXXX\nXXEXXXX\nXXXXXXX\nXXXXXXX",
    "words": "ALPHA", "false_words": "PLANE",
})
assert placement_cells(overlapping.solution_payload["placements"][0]) & placement_cells(
    overlapping.solution_payload["decoys"][0], "text"
) == {(2, 0)}
try:
    plugin.manual_builder({
        "grid": "ALPHAXX\nBRAVOXX\nALPHOXX\nXXXXXXX\nXXXXXXX\nXXXXXXX\nXXXXXXX",
        "words": "ALPHA\nBRAVO", "near_misses": "ALPHA -> ALPHO",
    })
    raise AssertionError("A substitution was accepted as an omission")
except ValueError as error:
    assert "omit exactly one" in str(error)


def guess_key(item):
    x, y = item["start"]
    dx, dy = item["direction"]
    length = len(item.get("text", item.get("word", "")))
    ends = sorted((f"{x},{y}", f"{x + dx * (length - 1)},{y + dy * (length - 1)}"))
    return f"guess:{ends[0]}:{ends[1]}"


solution = manual.solution_payload
false_decoy, near_decoy = solution["decoys"]
state = plugin.collaboration({}, {
    "kind": "patch", "changes": {guess_key(false_decoy): "pending"}, "_actor": 10, "participants": [10, 11],
}, solution)
assert state["player_penalties"]["10"]["attempts_remaining"] == 1
state = plugin.collaboration(state, {
    "kind": "patch", "changes": {guess_key(near_decoy): "pending"}, "_actor": 10, "participants": [10, 11],
}, solution)
assert state["player_penalties"]["10"]["locked"] is True
try:
    plugin.collaboration(state, {
        "kind": "patch", "changes": {"guess:0,4:4,4": "pending"}, "_actor": 10, "participants": [10, 11],
    }, solution)
    raise AssertionError("Limited attempts could be bypassed after reconnect/state reuse")
except ValueError as error:
    assert "No incorrect-selection attempts remain" in str(error)
accidental = plugin.collaboration({}, {
    "kind": "patch", "changes": {"guess:0,4:0,4": "pending"}, "_actor": 11, "participants": [10, 11],
}, solution)
assert not accidental.get("player_penalties")
assert "no penalty" in accidental["last_selection_feedback"].lower()

for mode, field, expected in (
    ("mistake_counter", "mistakes", 1),
    ("score_deduction", "score_deduction", 5),
    ("time_penalty", "time_penalty_seconds", 5),
):
    mode_solution = deepcopy(solution)
    mode_solution["settings"].update({"penalty_mode": mode, "penalty_value": 5, "penalty_scope": "group"})
    checked = plugin.collaboration({}, {
        "kind": "patch", "changes": {guess_key(false_decoy): "pending"}, "_actor": 10, "participants": [10, 11],
    }, mode_solution)
    assert checked["penalty"][field] == expected

no_penalty_solution = deepcopy(solution)
no_penalty_solution["settings"]["penalty_mode"] = "none"
no_penalty = plugin.collaboration({}, {
    "kind": "patch", "changes": {guess_key(false_decoy): "pending"}, "_actor": 10,
}, no_penalty_solution)
assert "penalty" not in no_penalty and "Decoy" in no_penalty["last_selection_feedback"]


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(display_name="Decoy Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Account-test-123"))
    player = User(display_name="Decoy Player", is_active=True, session_version=1,
                  password_hash=hash_password("Account-test-123"))
    db.add_all([admin, player]); db.flush()
    admin_id, player_id = admin.id, player.id
    db.add(Puzzle(
        id="word-decoy-qa", puzzle_type="word_search", title="Word Search Decoy QA", theme="qa", difficulty="hard",
        prompt=manual.prompt, play_payload=manual.play_payload, print_payload=manual.print_payload,
        solution_payload=manual.solution_payload, puzzle_metadata={"manual_editor_options": {
            "false_words": "DELTA", "near_misses": "ALPHA -> ALHA",
        }}, source="test", created_by_id=admin_id,
    ))
    db.commit()


def cookie(user_id):
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


base = "http://127.0.0.1:8804"
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8804"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin_context = browser.new_context(viewport={"width": 1280, "height": 900})
        player_context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        admin_context.add_cookies([{"name": "session", "value": cookie(admin_id), "url": base}])
        player_context.add_cookies([{"name": "session", "value": cookie(player_id), "url": base}])
        for _ in range(120):
            try:
                if admin_context.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("QA server did not start")

        public = player_context.request.get(base + "/api/puzzles/word-decoy-qa").json()
        assert "decoys" not in public["play_payload"]
        assert "manual_editor_options" not in public["metadata"]
        assert public["play_payload"]["settings"]["decoy_count"] == 2

        def drag(page, x1, y1, x2, y2):
            first = page.locator(f'button[data-x="{x1}"][data-y="{y1}"]').bounding_box()
            last = page.locator(f'button[data-x="{x2}"][data-y="{y2}"]').bounding_box()
            page.mouse.move(first["x"] + first["width"] / 2, first["y"] + first["height"] / 2)
            page.mouse.down()
            page.mouse.move(last["x"] + last["width"] / 2, last["y"] + last["height"] / 2, steps=8)
            page.mouse.up()

        solo = player_context.new_page()
        assert solo.goto(base + "/puzzles/word-decoy-qa").ok
        assert "Decoys are active" in solo.locator("body").inner_text()
        # A tap is ignored locally and never reaches the server as a penalized guess.
        solo.locator('button[data-x="0"][data-y="4"]').click()
        assert "ignored without a penalty" in solo.locator("#word-search-status").inner_text()
        drag(solo, 0, 2, 4, 2)
        solo.wait_for_function('document.querySelector("#word-search-penalty").textContent.includes("remaining: 1")')
        assert "Decoy selected" in solo.locator("#word-search-penalty").inner_text()
        solo.reload()
        assert "remaining: 1" in solo.locator("#word-search-penalty").inner_text()
        drag(solo, 0, 3, 3, 3)
        solo.wait_for_function('document.querySelector("#word-search-penalty").textContent.includes("remaining: 0")')
        assert solo.locator(".word-grid").evaluate("node => node.classList.contains('penalty-locked')")
        assert solo.evaluate("document.documentElement.scrollWidth <= innerWidth")

        group = admin_context.request.post(base + "/api/puzzles/word-decoy-qa/groups", data={}).json()
        endpoint = base + "/api/groups/" + group["id"]
        assert player_context.request.post(base + "/api/groups/join", data={"code": group["code"]}).ok
        host_wrong = admin_context.request.post(endpoint + "/actions", data={
            "action_id": str(uuid.uuid4()), "revision": 0, "puzzle_id": "word-decoy-qa",
            "kind": "patch", "target": "", "changes": {guess_key(false_decoy): "pending"},
        }).json()
        assert host_wrong["state"]["current_penalty"]["attempts_remaining"] == 1
        assert "player_penalties" not in host_wrong["state"]
        player_view = player_context.request.get(endpoint).json()
        assert player_view["state"]["current_penalty"] == {}
        player_wrong = player_context.request.post(endpoint + "/actions", data={
            "action_id": str(uuid.uuid4()), "revision": player_view["revision"], "puzzle_id": "word-decoy-qa",
            "kind": "patch", "target": "", "changes": {guess_key(near_decoy): "pending"},
        }).json()
        assert player_wrong["state"]["current_penalty"]["attempts_remaining"] == 1
        assert admin_context.request.get(endpoint).json()["state"]["current_penalty"]["attempts_remaining"] == 1

        solution_page = admin_context.new_page()
        solution_page.goto(base + "/puzzles/word-decoy-qa")
        solution_page.locator("summary", has_text="Admin Solution").click()
        assert "Intentional decoys" in solution_page.locator("body").inner_text()
        assert "Near miss for ALPHA" in solution_page.locator("body").inner_text()
        browser.close()
        print("Word Search generated/manual decoys, exact omissions, duplicate prevention, all penalty modes, accidental-touch protection, persistence, privacy and per-player Group Solve state passed.")
finally:
    if server.poll() is None:
        server.terminate()
        server.wait(timeout=15)
