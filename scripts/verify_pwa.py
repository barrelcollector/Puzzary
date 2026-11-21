"""Verify PWA installability, safe caching, offline fallback, and update controls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from qa_runtime import qa_directory


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = qa_directory("pwa")
DATABASE = QA_ROOT / "pwa.sqlite"
DATABASE.unlink(missing_ok=True)
BASE = "http://127.0.0.1:8820"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{DATABASE.as_posix()}",
    "SESSION_SECRET": "isolated-pwa-test-secret",
    "UPLOAD_DIR": str(QA_ROOT / "uploads"),
    "APP_BASE_URL": BASE,
    "PUZZARY_HOST": "127.0.0.1",
    "PUZZARY_PORT": "8820",
})

from app.schema_migrations import upgrade_database


upgrade_database()
manifest = json.loads((ROOT / "app" / "static" / "site.webmanifest").read_text(encoding="utf-8"))
assert manifest["display"] == "standalone"
assert manifest["start_url"].startswith("/library") and manifest["scope"] == "/"
assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])

worker_source = (ROOT / "app" / "static" / "notification-service-worker.js").read_text(encoding="utf-8")
for marker in (
    'addEventListener("install"', 'addEventListener("activate"', 'addEventListener("fetch"',
    'addEventListener("push"', 'addEventListener("notificationclick"', "SKIP_WAITING",
    "CLEAR_USER_DATA", "API responses and authenticated HTML are deliberately always network-only",
):
    assert marker in worker_source, marker
assert "caches.match(request)" not in worker_source

server = subprocess.Popen(
    [sys.executable, "-m", "app.server"],
    cwd=ROOT,
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="allow", viewport={"width": 390, "height": 844})
        page = context.new_page()
        for _ in range(120):
            try:
                if context.request.get(BASE + "/health").ok:
                    break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("PWA QA server did not start")

        page.goto(BASE + "/login")
        page.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.ready")
        page.reload()
        page.wait_for_function("Boolean(navigator.serviceWorker.controller)")
        assert page.locator('link[rel="manifest"]').get_attribute("href") == "/static/site.webmanifest?v=2"
        assert page.locator('[data-pwa-install]').count() == 1
        assert page.locator('meta[name="theme-color"]').count() == 2
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        served_worker = context.request.get(BASE + "/service-worker.js")
        assert served_worker.ok and "__PUZZARY_VERSION__" not in served_worker.text()
        assert served_worker.headers["service-worker-allowed"] == "/"
        assert context.request.get(BASE + "/static/site.webmanifest?v=2").json()["display"] == "standalone"

        page.evaluate("fetch('/api/notifications', {credentials:'same-origin'}).catch(() => null)")
        cached_urls = page.evaluate("""async () => {
          const result = [];
          for (const name of await caches.keys()) {
            const cache = await caches.open(name);
            for (const request of await cache.keys()) result.push(request.url);
          }
          return result;
        }""")
        assert cached_urls and all("/static/" in url or "/plugins/" in url for url in cached_urls)
        assert not any("/api/" in url or "/login" in url or "/puzzles/" in url for url in cached_urls)

        context.set_offline(True)
        page.wait_for_function("!navigator.onLine")
        page.wait_for_selector("[data-pwa-connectivity]:visible")
        response = page.goto(BASE + "/library", wait_until="domcontentloaded")
        assert response and response.ok
        assert page.locator("h1").inner_text() == "Puzzary is offline"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        context.set_offline(False)

        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()

print("PWA manifest, secure registration, shell-only caching, offline fallback, connectivity UI, updates, themes, and push sharing passed.")
