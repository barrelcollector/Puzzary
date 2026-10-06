"""Read-only integration checks against an existing puzzle database."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import select
from starlette.requests import Request

from app.database import SessionLocal
from app.main import render_puzzle_print_page
from app.models import Puzzle
from app.print_pdf import render_pdf


def main():
    output = Path("/tmp/puzzary-pdf-qa")
    output.mkdir(exist_ok=True)
    results = []
    with SessionLocal() as db, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for kind in ("word_search", "crossword", "sudoku", "word_pyramid"):
                puzzle = db.scalar(select(Puzzle).where(Puzzle.puzzle_type == kind).order_by(Puzzle.created_at.desc()))
                assert puzzle is not None, f"No {kind} available for verification"
                for size in ("a4", "letter", "zebra", "4x6"):
                    request = Request({"type": "http", "method": "GET", "path": "/",
                                       "query_string": f"size={size}".encode(), "headers": []})
                    response = render_puzzle_print_page(puzzle.id, request, db, None, False, pdf_mode=True)
                    html = response.body.decode()
                    layout = response.context["print_layout"]
                    # Inspect layout before generating, including successful embedded image loads.
                    page = browser.new_page(viewport={"width": round(layout["page_width_mm"] * 96 / 25.4), "height": 900})
                    page.emulate_media(media="print")
                    page.set_content(html, wait_until="load")
                    checks = page.evaluate("""() => {
                        const sheet = document.querySelector('.print-sheet');
                        const box = sheet.getBoundingClientRect();
                        return {
                            overflow: sheet.scrollWidth > box.width + 2,
                            images: [...sheet.querySelectorAll('img')].every(i => i.complete && i.naturalWidth > 0),
                            width: box.width,
                            height: box.height
                        };
                    }""")
                    page.close()
                    assert not checks["overflow"], (kind, size, checks)
                    assert checks["images"], (kind, size, "QR image failed")
                    pdf = render_pdf(html, layout)
                    assert pdf.startswith(b"%PDF-"), (kind, size)
                    filename = f"{kind}-{size}.pdf"
                    (output / filename).write_bytes(pdf)
                    results.append({"file": filename, "puzzle": puzzle.id, "bytes": len(pdf), **checks})
        finally:
            browser.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
