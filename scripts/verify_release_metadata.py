"""Verify the public-beta version, packaging, and release-document contract."""
from __future__ import annotations

import os
import re
import sys
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.version import __version__


version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version), version
assert __version__ == version

env_values = {}
for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env_values[key] = value
assert env_values["PUZZARY_VERSION"] == version

dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
assert "COPY VERSION ./VERSION" in dockerfile
assert "ARG PUZZARY_VERSION" in dockerfile
assert 'org.opencontainers.image.version="${PUZZARY_VERSION}"' in dockerfile
assert 'PUZZARY_VERSION: "${PUZZARY_VERSION:?' in compose

dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
for excluded in (
    ".git", ".env", ".venv", "data", "uploads", "*.db", "*.sqlite", "*.key",
    "*.dump", "backups", "artifacts", "test-results", ".pytest_cache", "sources",
):
    assert excluded in dockerignore, excluded

expected = {
    "cross_equations", "crossword", "maze", "minesweeper", "nonogram",
    "spell_it", "sudoku", "takuzu", "word_pyramid", "word_search",
}
bundled = {
    path.stem for path in (ROOT / "app" / "puzzle_types").glob("*.py")
    if path.stem != "__init__"
}
catalog = json.loads((ROOT / "app" / "official_plugins.json").read_text(encoding="utf-8"))
official_puzzles = {
    plugin["slug"] for plugin in catalog["plugins"]
    if plugin.get("package_type", "puzzle") == "puzzle"
}
official_content = {
    plugin["slug"] for plugin in catalog["plugins"]
    if plugin.get("package_type", "puzzle") == "content"
}
assert bundled == set(), bundled
assert official_puzzles == expected, official_puzzles
assert official_content == {"dictionary_content"}, official_content

changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
release_notes = (ROOT / "docs" / "releases" / f"v{version}.md").read_text(encoding="utf-8")
deployment = (ROOT / "docs" / "docker-deployment.md").read_text(encoding="utf-8")
assert f"[{version}]" in changelog
for marker in ("SHA-256", "backup", "upgrade", "tagged commit"):
    assert marker.lower() in release_notes.lower(), marker
for marker in ("pg_dump", "pg_restore --list", "rollback"):
    assert marker in deployment, marker

print(f"PASS: {version} is synchronized across the API source, Compose build, image label, and beta release documentation")
