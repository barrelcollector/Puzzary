"""Regression checks for the player-facing solution privacy contract."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from pathlib import Path

db_path = Path(tempfile.gettempdir()) / f"puzzary-solution-privacy-{uuid.uuid4().hex}.sqlite"
os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-solution-privacy-test-secret"
os.environ["UPLOAD_DIR"] = str(db_path.parent / "solution-privacy-uploads")

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, app, hash_password
from app.models import Puzzle, User
from app.puzzles import get_puzzle_type, list_puzzle_types


def session_cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def generated_puzzles():
    result = {}
    for plugin in list_puzzle_types():
        options = {"grid_size": 4} if plugin.slug in ("sudoku", "takuzu") else {}
        if plugin.slug == "cross_equations":
            options = {"grid_size": 3, "operators": ["add"]}
        if plugin.slug == "maze":
            options = {"maze_size": "custom", "custom_width": 5, "custom_height": 5, "allow_loops": "no"}
        if plugin.slug == "word_search":
            item = plugin.manual_builder({
                "grid": "CATXXXXX\nDOGXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX",
                "words": "CAT,DOG",
                "auto_confirm": "no",
                "gimme_count": 1,
            })
        elif plugin.slug == "word_pyramid":
            item = plugin.manual_builder({
                "rows": "TIP|Advice|Useful suggestion\nPINT|A measure|Liquid measure\nPOINT|Sharp end|A location",
                "gimme_count": 0,
            })
        elif plugin.slug == "spell_it":
            item = plugin.manual_builder({
                "entries": [
                    {"answer": "CAT", "definition": "A small feline"},
                    {"answer": "DOG", "definition": "A canine"},
                ],
                "mask_percent": 100,
                "preserve_edges": "no",
                "letter_bank": "off",
                "hint_limit": 2,
            })
        else:
            item = plugin.generator("", "easy", None, "solution-privacy", options)
        result[plugin.slug] = item
    return result


try:
    Base.metadata.create_all(engine)
    items = generated_puzzles()
    with SessionLocal() as db:
        player = User(
            display_name="Privacy Player",
            is_admin=False,
            is_active=True,
            session_version=1,
            password_hash=hash_password("isolated-test-password"),
        )
        db.add(player)
        db.flush()
        player_id = player.id
        for slug, item in items.items():
            play_payload = deepcopy(item.play_payload)
            print_payload = deepcopy(item.print_payload)
            if slug == "word_search":
                # Simulate a puzzle saved before player-safe Gimme coordinates were added.
                play_payload.get("gimmes", {}).pop("cells", None)
                print_payload.get("gimmes", {}).pop("cells", None)
            db.add(Puzzle(
                id=f"privacy-{slug}",
                puzzle_type=slug,
                title=f"Privacy {slug}",
                theme="",
                difficulty="easy",
                prompt=item.prompt,
                play_payload=play_payload,
                print_payload=print_payload,
                solution_payload=item.solution_payload,
                puzzle_metadata={},
                source="test",
            ))
        db.commit()

    base = "http://127.0.0.1:8793"
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8793"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            client = browser.new_context()
            client.add_cookies([{"name": "session", "value": session_cookie(player_id), "url": base}])
            for _ in range(100):
                try:
                    if client.request.get(base + "/health").ok:
                        break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError("Privacy QA server did not start")

            for slug, item in items.items():
                page = client.request.get(base + f"/puzzles/privacy-{slug}")
                assert page.ok, (slug, page.text())
                page_text = page.text()
                html = page_text.lower()
                assert "data-solution=" not in html, slug
                assert "data-placements=" not in html, slug
                assert json.dumps(item.solution_payload, sort_keys=True) not in page_text, slug

                response = client.request.get(base + f"/api/puzzles/privacy-{slug}")
                assert response.ok, (slug, response.text())
                public = response.json()["play_payload"]
                assert public != item.solution_payload, slug
                if slug == "crossword":
                    assert all("answer" not in clue for clue in public.get("clues", []))
                    assert all("letter" not in cell for row in public.get("grid", []) for cell in row)
                if slug == "word_search":
                    assert "placements" not in public
                    assert public["gimmes"]["words"] == ["CAT"]
                    assert public["gimmes"]["cells"] == {"0,0": "C", "1,0": "A", "2,0": "T"}
                    assert html.count('data-gimme="true" class="found-cell"') == 3

                attempt = client.request.post(
                    base + f"/api/puzzles/privacy-{slug}/attempts",
                    data={"answer": "definitely-wrong"},
                )
                assert attempt.ok, (slug, attempt.text())
                assert attempt.json()["expected_count"] is None, slug
                assert attempt.json()["matched_count"] is None, slug

            maze_attempt = client.request.post(
                base + "/api/puzzles/privacy-maze/attempts",
                data={"answer": "done"},
            )
            assert maze_attempt.ok
            assert maze_attempt.json()["is_correct"] is False

            sudoku = items["sudoku"]
            givens = sudoku.solution_payload.get("givens", {})
            key = next(
                f"{x},{y}"
                for y, row in enumerate(sudoku.solution_payload["grid"])
                for x, _ in enumerate(row)
                if f"{x},{y}" not in givens
            )
            x, y = map(int, key.split(","))
            expected = str(sudoku.solution_payload["grid"][y][x])
            wrong = "1" if expected != "1" else "2"
            checked = client.request.post(
                base + "/api/puzzles/privacy-sudoku/actions",
                data={"kind": "check", "changes": {key: wrong}},
            )
            assert checked.ok, checked.text()
            assert checked.json().get("incorrect", []) == []
            assert checked.json().get("completed") is False
            assert checked.json().get("answers", {}).get(key) == wrong

            revealed = client.request.post(
                base + "/api/puzzles/privacy-sudoku/actions",
                data={"kind": "reveal", "changes": {}},
            )
            assert revealed.ok, revealed.text()
            assert revealed.json()["answers"][key] == expected
            assert revealed.json()["completed"] is True

            minesweeper = items["minesweeper"]
            safe = next(iter(minesweeper.solution_payload["counts"]))
            played = client.request.post(
                base + "/api/puzzles/privacy-minesweeper/actions",
                data={"kind": "reveal_cell", "target": safe, "changes": {}},
            )
            assert played.ok, played.text()
            assert played.json()["answers"][safe] == minesweeper.solution_payload["counts"][safe]
            persisted = client.request.get(base + "/api/puzzles/privacy-minesweeper/progress")
            assert persisted.ok and persisted.json()["answers"][safe] == played.json()["answers"][safe]
            forged = client.request.post(
                base + "/api/puzzles/privacy-minesweeper/progress",
                data={"answers": minesweeper.solution_payload["counts"]},
            )
            assert forged.ok and forged.json()["answers"] == {}
            mine = minesweeper.solution_payload["mines"][0]
            lost = client.request.post(
                base + "/api/puzzles/privacy-minesweeper/actions",
                data={"kind": "reveal_cell", "target": mine, "changes": {}},
            )
            assert lost.ok and lost.json()["status"] == "lost" and lost.json()["exploded"] == mine
            reset = client.request.post(base + "/api/puzzles/privacy-minesweeper/reset", data={})
            assert reset.ok and reset.json()["answers"] == {} and reset.json()["found_count"] == 0
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    print("PASS: all ten player pages and APIs keep solutions private; checks, reveals, and Maze validation are server-side")
finally:
    engine.dispose()
    for attempt in range(10):
        try:
            db_path.unlink(missing_ok=True)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.1)
