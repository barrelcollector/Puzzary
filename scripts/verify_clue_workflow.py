"""Clue-tier, full-text, Crossword display, and generic-fallback checks for #18."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

database = Path(tempfile.gettempdir()) / "puzzary-clue-workflow-qa.sqlite"
database.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-clue-workflow-secret"
os.environ["UPLOAD_DIR"] = str(Path(tempfile.gettempdir()) / "puzzary-clue-workflow-uploads")

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from app.clues import definition_clue_tiers, stored_definition_clues, word_clue_tiers
from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, apply_definition_payload, hash_password, serialize_definition
from app.models import DictionaryDefinition, DictionaryWord, Puzzle, User
from app.puzzles import get_puzzle_type
from app.schemas import DictionaryDefinitionPatch


def tag(name: str):
    return SimpleNamespace(name=name, tag_type="Clue")


long_clue = (
    "A complete, deliberately long clue that keeps every qualifying phrase, explanatory detail, "
    "and final distinguishing condition so the player sees the author's full wording without an ellipsis "
    "or a silent character limit at the end of the sentence."
)
long_definition = (
    "A complete definition that is intentionally longer than the historical tag and picker previews, "
    "including enough context to distinguish this sense from neighboring meanings while proving that the "
    "play view, API representation, and progressive clue workflow preserve the entire source text."
)
assert len(long_clue) > 120 and len(long_definition) > 120

first = SimpleNamespace(id=1, definition=long_definition, position=0, archived_at=None,
                        properties={"clues": [long_clue]}, tags=[tag("Short indexed clue")])
second = SimpleNamespace(id=2, definition="A second legitimate sense for the same answer.", position=1,
                         archived_at=None, properties={"clues": ["A separate clue for the second sense."]}, tags=[])
word = SimpleNamespace(id=1, word="CAT", display_text="Cat", clue="Legacy clue", definitions=[first, second])
assert stored_definition_clues(first)[0] == long_clue
assert definition_clue_tiers(first) == [long_clue, "Short indexed clue", f"Definition: {long_definition}"]
all_tiers = word_clue_tiers(word)
assert long_clue in all_tiers and "A separate clue for the second sense." in all_tiers
assert "Definition: A second legitimate sense for the same answer." in all_tiers
generic = SimpleNamespace(definition="A real definition replaces placeholder wording.", position=0, archived_at=None,
                          properties={"clues": ["Weather term starting with C (5 letters)."]}, tags=[])
assert definition_clue_tiers(generic) == ["Definition: A real definition replaces placeholder wording."]
old_preview = SimpleNamespace(definition=long_definition, position=0, archived_at=None,
                              properties={"clue": long_definition[:71].rsplit(" ", 1)[0] + "..."}, tags=[])
assert definition_clue_tiers(old_preview) == [f"Definition: {long_definition}"]

crossword = get_puzzle_type("crossword")
source = crossword.source_builder([{"word": word, "definition": first}], "easy", set())
assert source and source[0]["clues"][0] == long_clue
assert f"Definition: {long_definition}" in source[0]["clues"]

try:
    crossword.manual_builder({"entries": "CAT\nCAR | Road transport\nART | Creative work"})
    raise AssertionError("Manual Crossword accepted a generic clue placeholder")
except ValueError as error:
    assert "needs at least one clue or definition" in str(error)

manual_lines = "\n".join(
    f"{answer} | {long_clue} | Definition: {long_definition}"
    for answer in ("TIP", "PINT", "POINT", "PISTON")
)
generated = crossword.manual_builder({
    "entries": manual_lines,
    "grid_size": "12",
    "more_clues_depth": "multi_step",
    "gimme_count": "0",
    "seed": "full-clue-workflow-qa",
})
assert len(generated.solution_payload["entries"]) == 4
assert all(entry["clue"] == long_clue for entry in generated.solution_payload["entries"])
assert all(entry["clues"][1] == f"Definition: {long_definition}" for entry in generated.solution_payload["entries"])
one_hint = crossword.generator("manual", "easy", [
    {"word": answer, "clues": [long_clue, f"Definition: {long_definition}", "Third tier"]}
    for answer in ("TIP", "PINT", "POINT", "PISTON")
], "one-clue-tier-qa", {"grid_size": "12", "answer_count_min": "4", "answer_count": "4", "more_clues_depth": "one_hint"})
assert all(len(entry["clues"]) == 2 for entry in one_hint.solution_payload["entries"])
assert not any(" entry." in clue.lower() or "word from" in clue.lower()
               for entry in generated.solution_payload["entries"] for clue in entry["clues"])

Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(display_name="Clue Admin", is_admin=True, is_active=True, session_version=1,
                 password_hash=hash_password("Clue-test-123"))
    dictionary_word = DictionaryWord(word="LONGCLUEQA", display_text="Long Clue QA")
    definition = DictionaryDefinition(word=dictionary_word, definition=long_definition, position=0, properties={})
    db.add_all([admin, dictionary_word, definition])
    db.flush()
    apply_definition_payload(db, definition, DictionaryDefinitionPatch(clues=[long_clue]))
    db.add(Puzzle(
        id="clue-workflow-qa", puzzle_type="crossword", title="Full Clue QA", theme="qa", difficulty="easy",
        prompt=generated.prompt, play_payload=generated.play_payload, print_payload=generated.print_payload,
        solution_payload=generated.solution_payload, puzzle_metadata={}, source="test", created_by_id=admin.id,
    ))
    db.commit()
    admin_id = admin.id
    assert not [item for item in definition.tags if item.tag_type == "Clue"], "Long clue was forced into a short tag"
    assert definition.properties["clues"] == [long_clue]
    assert serialize_definition(definition)["clues"] == [long_clue]


def cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id, "session_version": 1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
base = f"http://127.0.0.1:{port}"
log_path = Path(tempfile.gettempdir()) / "puzzary-clue-workflow-qa.log"
with log_path.open("w", encoding="utf-8") as log:
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.add_cookies([{"name": "session", "value": cookie(admin_id), "url": base}])
            for _ in range(100):
                try:
                    if context.request.get(base + "/health").ok:
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError(log_path.read_text(encoding="utf-8"))
            page = context.new_page()
            assert page.goto(base + "/puzzles/clue-workflow-qa").ok
            primary = page.locator(".crossword-clue-text").first
            assert primary.inner_text() == long_clue
            assert primary.evaluate("el => getComputedStyle(el).textOverflow !== 'ellipsis' && getComputedStyle(el).overflow !== 'hidden'")
            page.locator(".more-clue").first.click()
            revealed = page.locator(".clue-steps li:not([hidden])").first
            assert long_definition in revealed.inner_text()
            assert "..." not in primary.inner_text() and "..." not in revealed.inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

engine.dispose()
print("Full clue and definition text, multiple senses, shared clue tiers, generic-placeholder rejection, tier depth, API persistence, and mobile Crossword display passed.")
