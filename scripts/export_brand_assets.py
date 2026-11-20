"""Export Puzzary browser and application icons from the SVG master."""
from __future__ import annotations

import base64
import struct
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "app" / "static" / "brand"
MASTER = BRAND_DIR / "puzzary-mark.svg"
BRAND_ASSET_VERSION = 1
SIZES = {
    "puzzary-16.png": 16,
    "puzzary-32.png": 32,
    "puzzary-48.png": 48,
    "apple-touch-icon.png": 180,
    "puzzary-192.png": 192,
    "puzzary-512.png": 512,
}


def render_png(page, svg: str, size: int, destination: Path) -> None:
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(
        '<style>html,body{margin:0;width:100%;height:100%;overflow:hidden}</style>'
        f'<img alt="" src="data:image/svg+xml;base64,{encoded}" '
        'style="display:block;width:100%;height:100%">'
    )
    page.locator("img").screenshot(path=str(destination), omit_background=True)


def write_ico(destination: Path, sources: list[Path]) -> None:
    images = [source.read_bytes() for source in sources]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + (16 * len(images))
    entries = []
    for source, data in zip(sources, images, strict=True):
        size = int(source.stem.rsplit("-", 1)[-1])
        entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    destination.write_bytes(header + b"".join(entries) + b"".join(images))


def main() -> None:
    svg = MASTER.read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        for filename, size in SIZES.items():
            render_png(page, svg, size, BRAND_DIR / filename)
        maskable = svg.replace('rx="112"', 'rx="0"', 1)
        render_png(page, maskable, 512, BRAND_DIR / "puzzary-maskable-512.png")
        browser.close()
    write_ico(
        BRAND_DIR / "favicon.ico",
        [BRAND_DIR / f"puzzary-{size}.png" for size in (16, 32, 48)],
    )
    print(f"Exported Puzzary brand assets (version {BRAND_ASSET_VERSION}).")


if __name__ == "__main__":
    main()
