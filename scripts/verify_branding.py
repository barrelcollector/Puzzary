from pathlib import Path
import json
import struct

from app.content import THEMES, words_for_theme
from app.schemas import PuzzleGenerateRequest


root = Path(__file__).resolve().parents[1]
readme = (root / "README.md").read_text(encoding="utf-8")
base = (root / "app/templates/base.html").read_text(encoding="utf-8")
home = (root / "app/templates/home.html").read_text(encoding="utf-8")
admin = (root / "app/templates/admin.html").read_text(encoding="utf-8")
generate = (root / "app/templates/generate.html").read_text(encoding="utf-8")
main = (root / "app/main.py").read_text(encoding="utf-8")
brand_dir = root / "app/static/brand"
manifest = json.loads((root / "app/static/site.webmanifest").read_text(encoding="utf-8"))

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
assert "## Why Puzzary?" in readme
assert "## Puzzle modules" in readme

assert 'value="general" placeholder="General"' in admin
assert 'value="general" placeholder="General"' in generate
assert 'placeholder="daily_stripe"' not in admin
assert PuzzleGenerateRequest().theme == "general"
assert THEMES["daily_stripe"] == THEMES["general"]
assert words_for_theme("unknown-theme", "easy") == THEMES["general"][:5]
assert "An extensible puzzle and game library with a flexible API" in main

assert (brand_dir / "puzzary-mark.svg").is_file()
assert "#0d3b36" in (brand_dir / "puzzary-mark.svg").read_text(encoding="utf-8")
expected_pngs = {
    "puzzary-16.png": (16, 16), "puzzary-32.png": (32, 32),
    "puzzary-48.png": (48, 48), "apple-touch-icon.png": (180, 180),
    "puzzary-192.png": (192, 192), "puzzary-512.png": (512, 512),
    "puzzary-maskable-512.png": (512, 512),
}
for filename, expected_size in expected_pngs.items():
    data = (brand_dir / filename).read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), filename
    assert struct.unpack(">II", data[16:24]) == expected_size, filename
ico = (brand_dir / "favicon.ico").read_bytes()
assert struct.unpack("<HHH", ico[:6]) == (0, 1, 3)
assert [ico[6 + (index * 16)] for index in range(3)] == [16, 32, 48]
assert manifest["name"] == "Puzzary"
assert manifest["display"] == "standalone" and manifest["id"] == "/"
assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])
for reference in (
    'favicon.ico?v=1', 'puzzary-mark.svg?v=1', 'puzzary-32.png?v=1',
    'apple-touch-icon.png?v=1', 'site.webmanifest?v=2',
):
    assert reference in base
assert '<img src="/static/brand/puzzary-mark.svg?v=1"' in base
assert (root / "scripts/export_brand_assets.py").is_file()
assert "python scripts/export_brand_assets.py" in (brand_dir / "README.md").read_text(encoding="utf-8")

print("PASS: product copy and the reproducible Puzzary icon system are integrated with browser and app metadata")
