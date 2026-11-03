from pathlib import Path

from app.content import THEMES, words_for_theme
from app.schemas import PuzzleGenerateRequest


root = Path(__file__).resolve().parents[1]
readme = (root / "README.md").read_text(encoding="utf-8")
base = (root / "app/templates/base.html").read_text(encoding="utf-8")
home = (root / "app/templates/home.html").read_text(encoding="utf-8")
admin = (root / "app/templates/admin.html").read_text(encoding="utf-8")
generate = (root / "app/templates/generate.html").read_text(encoding="utf-8")
main = (root / "app/main.py").read_text(encoding="utf-8")

assert "An extensible puzzle and game library" in base
assert "A puzzle library for Daily Stripe" not in base
assert "Puzzle &amp; Game Library" in home
assert "Create, organize, play, and share puzzles and games." in home

opening = "\n".join(readme.splitlines()[:35])
assert "self-hosted, extensible puzzle and game library" in opening
assert "flexible API" in opening
assert "modular plugins" in opening
assert "library and answer portal for Daily Stripe" not in readme
assert "product's defining purpose" not in readme
assert "primary purpose" not in readme
assert "docs/puzzle-modules.md" in readme
assert "Architecture and integrations" in readme

assert 'value="general" placeholder="General"' in admin
assert 'value="general" placeholder="General"' in generate
assert 'placeholder="daily_stripe"' not in admin
assert PuzzleGenerateRequest().theme == "general"
assert THEMES["daily_stripe"] == THEMES["general"]
assert words_for_theme("unknown-theme", "easy") == THEMES["general"][:5]
assert "An extensible puzzle and game library with a flexible API" in main

print("PASS: product copy leads with Puzzary's puzzle/game platform, API, and plugin system while preserving Daily Stripe compatibility")
