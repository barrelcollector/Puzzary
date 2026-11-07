"""Render the same self-contained print document used by the web preview."""

from threading import BoundedSemaphore

from playwright.sync_api import sync_playwright


PRINT_RENDER_SLOTS = BoundedSemaphore(2)
PX_PER_MM = 96 / 25.4


def render_pdf(html: str, layout: dict) -> bytes:
    if not PRINT_RENDER_SLOTS.acquire(timeout=10):
        raise TimeoutError("Print renderer is busy")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": round(layout["page_width_mm"] * PX_PER_MM), "height": 900},
                )
                # All CSS and QR images are embedded; never fetch remote resources.
                page.route("**/*", lambda route: route.abort())
                page.emulate_media(media="print")
                page.set_content(html, wait_until="load", timeout=30000)
                page.wait_for_function(
                    "document.fonts.status === 'loaded' && window.issuePagesReady !== false",
                    timeout=10000,
                )
                height_mm = layout["page_height_mm"]
                if height_mm is None:
                    height_px = page.locator(".print-sheet").first.evaluate(
                        "el => Math.max(el.scrollHeight, el.getBoundingClientRect().height)"
                    )
                    height_mm = height_px / PX_PER_MM + 2 * layout["page_margin_mm"] + 1
                if height_mm > 5000:
                    raise ValueError("Print output exceeds the maximum roll length of 5 metres")
                # Use explicit dimensions for roll paper: CSS @page cannot express auto height.
                page.add_style_tag(content=(
                    "@page { size: "
                    f"{layout['page_width_mm']}mm {height_mm}mm !important; "
                    f"margin: {layout['page_margin_mm']}mm !important; }}"
                ))
                return page.pdf(print_background=True, prefer_css_page_size=True)
            finally:
                browser.close()
    finally:
        PRINT_RENDER_SLOTS.release()


def render_png(html: str, layout: dict) -> bytes:
    """Render the printable sheet as a PNG for image consumers."""
    if not PRINT_RENDER_SLOTS.acquire(timeout=10):
        raise TimeoutError("Print renderer is busy")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                width = round(layout["page_width_mm"] * PX_PER_MM)
                page = browser.new_page(viewport={"width": width, "height": 1200})
                # The print document is self-contained; image rendering must not fetch
                # arbitrary resources from the network.
                page.route("**/*", lambda route: route.abort())
                page.emulate_media(media="print")
                page.set_content(html, wait_until="load", timeout=30000)
                page.wait_for_function(
                    "document.fonts.status === 'loaded' && window.issuePagesReady !== false",
                    timeout=10000,
                )
                sheet = page.locator(".print-sheet").first
                if sheet.count() == 0:
                    raise ValueError("Printable sheet was not found")
                height_px = sheet.evaluate(
                    "el => Math.max(el.scrollHeight, el.getBoundingClientRect().height)"
                )
                if height_px / PX_PER_MM > 5000:
                    raise ValueError("Print output exceeds the maximum image length of 5 metres")
                return sheet.screenshot(type="png", animations="disabled")
            finally:
                browser.close()
    finally:
        PRINT_RENDER_SLOTS.release()
