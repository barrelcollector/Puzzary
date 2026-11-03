"""Verify Word Search separators remain explicit at fractional grid widths."""
from pathlib import Path
from tempfile import gettempdir

from playwright.sync_api import sync_playwright


root = Path(__file__).resolve().parents[1]
styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
output = Path(gettempdir()) / "puzzary-word-search-grid-qa"
output.mkdir(exist_ok=True)
cells = "".join(
    f'<button type="button" data-x="{x}" data-y="{y}" data-letter="A">A</button>'
    for y in range(10) for x in range(10)
)
html = f"""
<style>:root{{--ink:#111;--panel:#fff;}}{styles}</style>
<div class="word-grid interactive" style="--grid-size:10;--word-grid-width:520px">{cells}</div>
"""

with sync_playwright() as playwright:
    for scale in (1, 1.25, 1.5, 2):
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(device_scale_factor=scale, viewport={"width": 850, "height": 760})
        page = context.new_page()
        page.set_content(html)
        grid = page.locator(".word-grid")
        for width in (468, 471, 517):
            grid.evaluate("(node, width) => node.style.width = `${width}px`", width)
            assert grid.evaluate("node => getComputedStyle(node).gap") == "0px"
            assert grid.locator('[data-x="0"][data-y="4"]').evaluate("node => getComputedStyle(node).borderLeftWidth") == "0px"
            assert grid.locator('[data-x="1"][data-y="4"]').evaluate("node => getComputedStyle(node).borderLeftWidth") == "1px"
            assert grid.locator('[data-x="4"][data-y="0"]').evaluate("node => getComputedStyle(node).borderTopWidth") == "0px"
            assert grid.locator('[data-x="4"][data-y="1"]').evaluate("node => getComputedStyle(node).borderTopWidth") == "1px"
            geometry = grid.evaluate("""node => {
              const cells = [...node.querySelectorAll('button')];
              return cells.map(cell => {
                const box = cell.getBoundingClientRect();
                return {x: Number(cell.dataset.x), y: Number(cell.dataset.y), left: box.left, top: box.top, right: box.right, bottom: box.bottom};
              });
            }""")
            for cell in geometry:
                if cell["x"]:
                    left = next(item for item in geometry if item["x"] == cell["x"] - 1 and item["y"] == cell["y"])
                    assert abs(cell["left"] - left["right"]) < 0.02
                if cell["y"]:
                    above = next(item for item in geometry if item["x"] == cell["x"] and item["y"] == cell["y"] - 1)
                    assert abs(cell["top"] - above["bottom"]) < 0.02
            grid.screenshot(path=str(output / f"grid-{width}-scale-{scale}.png"))
        context.close()
        browser.close()

print("Word Search grid uses continuous explicit separators at fractional widths and 1x-2x display scales.")
