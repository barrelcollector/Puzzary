from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


ROOT = Path(tempfile.gettempdir()) / "puzzary-plugin-package-qa"
shutil.rmtree(ROOT, ignore_errors=True)
PACKAGE = ROOT / "example_clock"
(PACKAGE / "templates" / "puzzle_types").mkdir(parents=True)
(PACKAGE / "static").mkdir()
(PACKAGE / "templates" / "puzzle_types" / "example_clock.html").write_text("<p>Clock</p>\n", encoding="utf-8")
(PACKAGE / "static" / "clock.js").write_text("// fixture\n", encoding="utf-8")
(PACKAGE / "puzzary-plugin.json").write_text(json.dumps({
    "schema": "puzzary.plugin.v1",
    "api_version": "1",
    "slug": "example_clock",
    "name": "Example Clock",
    "version": "1.0.0",
    "entrypoint": "plugin.py",
    "templates": "templates",
    "static": "static",
}), encoding="utf-8")
(PACKAGE / "plugin.py").write_text("""
from app.puzzles import GeneratedPuzzle, PuzzleType, register_puzzle_type
def generate(*args, **kwargs): return GeneratedPuzzle('Clock', '', {}, {}, {}, [], {})
def source(*args, **kwargs): return {}
def check(*args, **kwargs): return False, 1, 0
def normalize(*args, **kwargs): return {'answers': {}}
def count(*args, **kwargs): return 0, 1
register_puzzle_type(PuzzleType('example_clock', 'Example Clock', generate, source, check, normalize, count, play_script='/plugins/example_clock/static/clock.js'))
""", encoding="utf-8")

INVALID = ROOT / "invalid_extra"
INVALID.mkdir()
(INVALID / "puzzary-plugin.json").write_text(json.dumps({
    "schema": "puzzary.plugin.v1",
    "api_version": "1",
    "slug": "invalid_extra",
    "version": "1.0.0",
    "entrypoint": "plugin.py",
}), encoding="utf-8")
(INVALID / "plugin.py").write_text("""
from app.puzzles import PuzzleType, register_puzzle_type
def noop(*args, **kwargs): return {}
register_puzzle_type(PuzzleType('invalid_extra', 'Invalid', noop, noop, noop, noop, noop))
register_puzzle_type(PuzzleType('leaked_extra', 'Leaked', noop, noop, noop, noop, noop))
""", encoding="utf-8")

CONTENT = ROOT / "shared_dictionary"
(CONTENT / "packs" / "example-v1").mkdir(parents=True)
(CONTENT / "packs" / "example-v1" / "manifest.json").write_text(
    '{"schema":"example.content.v1"}\n', encoding="utf-8"
)
(CONTENT / "puzzary-plugin.json").write_text(json.dumps({
    "schema": "puzzary.plugin.v1",
    "api_version": "1",
    "package_type": "content",
    "slug": "shared_dictionary",
    "name": "Shared Dictionary",
    "version": "1.0.0",
    "content_packs": [{
        "id": "example-v1",
        "kind": "dictionary",
        "directory": "packs/example-v1",
        "manifest": "packs/example-v1/manifest.json",
    }],
}), encoding="utf-8")

os.environ["PUZZARY_PLUGIN_DIR"] = str(ROOT)

try:
    from app.plugin_packages import package_status, plugin_content_packs, plugin_static_directories, plugin_template_directories
    from app.main import app, templates
    from app.puzzles import get_puzzle_type

    puzzle_type = get_puzzle_type("example_clock")
    assert puzzle_type is not None and puzzle_type.label == "Example Clock"
    status = package_status()
    assert status["packages"][0]["slug"] == "example_clock"
    assert {package["slug"] for package in status["packages"]} == {"example_clock", "shared_dictionary"}
    assert plugin_content_packs()[0]["id"] == "example-v1"
    assert plugin_content_packs()[0]["package_slug"] == "shared_dictionary"
    assert "invalid_extra" in status["errors"]
    assert get_puzzle_type("invalid_extra") is None
    assert get_puzzle_type("leaked_extra") is None
    assert plugin_template_directories() == [(PACKAGE / "templates").resolve()]
    assert plugin_static_directories()["example_clock"] == (PACKAGE / "static").resolve()
    assert templates.get_template("puzzle_types/example_clock.html")
    assert any(getattr(route, "path", None) == "/plugins/example_clock/static" for route in app.routes)
    assert any(getattr(route, "path", None) == "/api/plugin-packages" for route in app.routes)
    install_route = next(route for route in app.routes if getattr(route, "path", None) == "/api/plugin-packages/install")
    assert "POST" in install_route.methods
    print("External plugin manifest, registration, templates, static assets, and status passed.")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)
