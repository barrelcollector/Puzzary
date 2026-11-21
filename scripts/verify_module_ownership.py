"""Prevent puzzle implementations and canonical content from returning to core."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUZZLE_SLUGS = {
    "cross_equations", "crossword", "maze", "minesweeper", "nonogram",
    "spell_it", "sudoku", "takuzu", "word_pyramid", "word_search",
}

assert {
    path.stem for path in (ROOT / "app" / "puzzle_types").glob("*.py")
    if path.stem != "__init__"
} == set()
for directory in (
    ROOT / "app" / "templates" / "puzzle_types",
    ROOT / "app" / "templates" / "puzzle_solutions",
    ROOT / "app" / "templates" / "puzzle_print",
):
    assert not directory.exists() or not any(directory.rglob("*")), directory

for filename in (
    "cross-equations.js", "crossword.js", "maze.js", "minesweeper.js",
    "nonogram.js", "nonogram-editor.js", "spell-it.js", "spell-it-editor.js",
    "sudoku.js", "takuzu.js", "takuzu-editor.js", "word-pyramid.js", "word-search.js",
):
    assert not (ROOT / "app" / "static" / filename).exists(), filename

assert not (ROOT / "content_packs" / "english-wordnet-5000-v1").exists()
assert not (ROOT / "docs" / "spell-it.md").exists()

catalog = json.loads((ROOT / "app" / "official_plugins.json").read_text(encoding="utf-8"))
assert {
    entry["slug"] for entry in catalog["plugins"]
    if entry.get("package_type", "puzzle") == "puzzle"
} == PUZZLE_SLUGS
assert {
    entry["slug"] for entry in catalog["plugins"]
    if entry.get("package_type", "puzzle") == "content"
} == {"dictionary_content"}

with tempfile.TemporaryDirectory(prefix="puzzary-empty-plugins-") as empty:
    environment = os.environ.copy()
    environment.update({"PYTHONPATH": str(ROOT), "PUZZARY_PLUGIN_DIR": empty})
    result = subprocess.run(
        [sys.executable, "-c", "from app.puzzles import list_puzzle_types; assert list_puzzle_types() == []"],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    assert result.returncode == 0

puzzle_template = (ROOT / "app" / "templates" / "puzzle.html").read_text(encoding="utf-8")
assert "Puzzle module unavailable." in puzzle_template
assert "saved puzzle and progress data have been preserved" in puzzle_template

print("PASS: core owns no puzzle implementation or dictionary pack; missing packages stay explicit and data-safe")
