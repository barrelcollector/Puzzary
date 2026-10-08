"""Isolated appearance-theme package, permission, fallback, and browser checks."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

database = Path(tempfile.gettempdir()) / "puzzary-theme-qa.sqlite"
artifacts = Path(os.environ.get("QA_ARTIFACT_DIR", Path(tempfile.gettempdir()) / "puzzary-qa-artifacts"))
artifacts.mkdir(parents=True, exist_ok=True)
database.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-theme-test-secret"
os.environ["UPLOAD_DIR"] = str(Path(tempfile.gettempdir()) / "puzzary-theme-uploads")

from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright

from app.appearance import DARK_TOKENS, SCHEMA, decode_package
from app.database import Base, SessionLocal, engine
from app.main import SESSION_SECRET, hash_password
from app.models import AppearanceTheme, Puzzle, User
from app.puzzles import list_puzzle_types


Base.metadata.create_all(engine)
generated = {}
for plugin in list_puzzle_types():
    options = {"grid_size":4} if plugin.slug in ("sudoku", "takuzu") else {}
    if plugin.slug == "cross_equations":
        options = {"grid_size":3, "operators":["add"]}
    if plugin.slug == "maze":
        options = {"maze_size":"custom", "custom_width":5, "custom_height":5, "allow_loops":"no"}
    if plugin.slug == "word_search":
        item = plugin.manual_builder({"grid":"CATXXXXX\nDOGXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX\nXXXXXXXX", "words":"CAT,DOG", "auto_confirm":"no"})
    elif plugin.slug == "word_pyramid":
        item = plugin.manual_builder({"rows":"TIP|Advice|Useful suggestion\nPINT|A measure|Liquid measure\nPOINT|Sharp end|A location", "gimme_count":0})
    elif plugin.slug == "spell_it":
        item = plugin.manual_builder({"entries":[{"answer":"CAT", "definition":"A small feline"}], "mask_percent":100, "preserve_edges":"no", "letter_bank":"off", "hint_limit":2})
    else:
        item = plugin.generator("", "easy", None, "theme-qa", options)
    generated[plugin.slug] = item

with SessionLocal() as db:
    users = [
        User(display_name="Theme Admin", is_admin=True, is_active=True, session_version=1, password_hash=hash_password("Theme-test-123")),
        User(display_name="Theme Player", is_admin=False, is_active=True, session_version=1, password_hash=hash_password("Theme-test-123")),
    ]
    db.add_all(users)
    db.flush()
    ids = [user.id for user in users]
    for kind, item in generated.items():
        db.add(Puzzle(
            id=f"theme-{kind}", puzzle_type=kind, title=f"Theme {kind.replace('_', ' ').title()}",
            theme="", difficulty="easy", prompt=item.prompt, play_payload=item.play_payload,
            print_payload=item.print_payload, solution_payload=item.solution_payload,
            puzzle_metadata={}, source="test", created_by_id=ids[0],
        ))
    db.commit()


def cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id":user_id, "session_version":1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


def contrast(page, foreground_selector: str, background_selector: str | None = None, foreground_property: str = "color", background_property: str = "backgroundColor") -> float:
    """Return the WCAG contrast ratio for two rendered component colors."""
    return page.evaluate(
        r"""([foregroundSelector, backgroundSelector, foregroundProperty, backgroundProperty]) => {
          const parse = value => {
            const match = value.match(/[\d.]+/g);
            if (!match || match.length < 3) throw new Error(`Unsupported color: ${value}`);
            return match.slice(0, 3).map(Number);
          };
          const luminance = value => {
            const channels = parse(value).map(channel => {
              channel /= 255;
              return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
            });
            return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
          };
          const foreground = getComputedStyle(document.querySelector(foregroundSelector))[foregroundProperty];
          const background = getComputedStyle(document.querySelector(backgroundSelector || foregroundSelector))[backgroundProperty];
          const [lighter, darker] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
          return (lighter + .05) / (darker + .05);
        }""",
        [foreground_selector, background_selector, foreground_property, background_property],
    )


manifest = {
    "schema":SCHEMA, "slug":"qa-midnight", "name":"QA Midnight", "version":"1.2.3",
    "author":"QA", "description":"Portable test theme", "tokens":{**DARK_TOKENS, "radius":10},
}
base = "http://127.0.0.1:8793"
server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8793"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin = browser.new_context(viewport={"width":1280, "height":900})
        player = browser.new_context(viewport={"width":390, "height":844})
        admin.add_cookies([{"name":"session", "value":cookie(ids[0]), "url":base}])
        player.add_cookies([{"name":"session", "value":cookie(ids[1]), "url":base}])
        for _ in range(100):
            try:
                if admin.request.get(base + "/health").ok:
                    break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("Theme QA server failed to start")

        assert player.request.get(base + "/admin/themes").status == 403
        response = admin.request.post(base + "/admin/themes/import", multipart={
            "package":{"name":"qa-midnight.puzzary-theme.json", "mimeType":"application/json", "buffer":json.dumps(manifest).encode()}
        })
        assert response.ok, response.text()
        with SessionLocal() as db:
            installed = db.get(AppearanceTheme, "qa-midnight")
            assert installed and installed.manifest["tokens"]["radius"] == 10

        exported = admin.request.get(base + "/admin/themes/qa-midnight/export")
        assert exported.ok and exported.json() == manifest
        assert "qa-midnight.puzzary-theme.json" in exported.headers["content-disposition"]

        unsafe = {**manifest, "slug":"unsafe-theme", "tokens":{**manifest["tokens"], "bg":"url(https://example.com/x)"}}
        response = admin.request.post(base + "/admin/themes/create", form={"manifest_json":json.dumps(unsafe)})
        assert "six-digit hex color" in response.text()
        low_contrast = {**manifest, "slug":"low-contrast", "tokens":{**manifest["tokens"], "ink":manifest["tokens"]["bg"]}}
        response = admin.request.post(base + "/admin/themes/create", form={"manifest_json":json.dumps(low_contrast)})
        assert "4.5:1 contrast" in response.text()
        try:
            decode_package(b"{" + b"x" * 65536)
            raise AssertionError("Oversize package accepted")
        except ValueError as error:
            assert "64 KB" in str(error)

        assert admin.request.post(base + "/admin/themes/qa-midnight/default").ok
        guest = browser.new_context(color_scheme="dark")
        guest_page = guest.new_page(); guest_page.goto(base + "/login")
        assert guest_page.locator("html").get_attribute("data-theme") == "qa-midnight"

        with SessionLocal() as db:
            db.add(AppearanceTheme(slug="broken-theme", name="Broken", manifest={"schema":"future-version"}))
            db.get(User, ids[1]).appearance_theme = "broken-theme"
            db.commit()
        page = player.new_page(); page.goto(base + "/account")
        assert page.locator("html").get_attribute("data-theme") == "qa-midnight", "Broken preferences must fall back to the server default"
        assert admin.request.get(base + "/admin/themes").ok, "A broken installed record must not break theme administration"
        assert admin.request.post(base + "/admin/themes/broken-theme/delete").ok
        assert player.request.post(base + "/account/appearance", form={"appearance_theme":"server"}).ok
        page.reload()
        assert page.locator("html").get_attribute("data-theme") == "qa-midnight", "Users without a preference use the server default"
        assert player.request.post(base + "/account/appearance", form={"appearance_theme":"light"}).ok
        page.reload()
        assert page.locator("html").get_attribute("data-theme") == "light"
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(245, 245, 242)"
        assert player.request.post(base + "/account/appearance", form={"appearance_theme":"system"}).ok
        system_dark = browser.new_context(viewport={"width":390, "height":844}, color_scheme="dark")
        system_dark.add_cookies([{"name":"session", "value":cookie(ids[1]), "url":base}])
        system_page = system_dark.new_page(); system_page.goto(base + "/account")
        assert system_page.locator("html").get_attribute("data-theme") == "system"
        assert system_page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(16, 21, 18)"
        system_dark.close()
        assert player.request.post(base + "/account/appearance", form={"appearance_theme":"dark"}).ok
        page.reload()
        assert page.locator("html").get_attribute("data-theme") == "dark"
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(16, 21, 18)"

        audit = player.new_page()
        audit_errors = []
        audit.on("pageerror", lambda error: audit_errors.append(str(error)))
        puzzle_checks = {
            "cross_equations":[(".cross-equations-cell.number-cell.given", None, "color", "backgroundColor", 4.5), (".cross-equations-cell.symbol-cell", None, "color", "backgroundColor", 4.5)],
            "crossword":[(".crossword-cell input:disabled", None, "color", "backgroundColor", 4.5)],
            "maze":[(".maze-board line", ".maze-paper", "stroke", "fill", 4.5)],
            "nonogram":[(".nonogram-cell", None, "color", "backgroundColor", 4.5)],
            "spell_it":[(".spell-slot", None, "color", "backgroundColor", 4.5)],
            "sudoku":[(".sudoku-cell input:disabled", None, "color", "backgroundColor", 4.5)],
            "takuzu":[(".takuzu-cell.given", None, "color", "backgroundColor", 4.5)],
            "word_pyramid":[(".word-pyramid-clue", None, "color", "backgroundColor", 4.5), (".word-pyramid-cells input", None, "color", "backgroundColor", 4.5)],
            "word_search":[(".word-grid button", None, "color", "backgroundColor", 4.5)],
        }
        for kind, checks in puzzle_checks.items():
            response = audit.goto(base + f"/puzzles/theme-{kind}", wait_until="networkidle")
            assert response.ok, (kind, response.status)
            assert audit.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{kind} overflows mobile viewport"
            for foreground_selector, background_selector, foreground_property, background_property, minimum in checks:
                ratio = contrast(audit, foreground_selector, background_selector, foreground_property, background_property)
                assert ratio >= minimum, (kind, foreground_selector, ratio)
            if kind == "word_search":
                audit.locator(".word-grid button").first.evaluate("node => node.classList.add('candidate-cell')")
                assert contrast(audit, ".word-grid button.candidate-cell") >= 4.5
            if kind == "spell_it":
                audit.locator(".spell-slot").first.evaluate("node => node.readOnly = true")
                assert contrast(audit, ".spell-slot[readonly]") >= 4.5
            audit.screenshot(path=str(artifacts / f"theme-{kind}-mobile.png"), full_page=True)
        assert not audit_errors, audit_errors

        audit.goto(base + "/library", wait_until="networkidle")
        assert contrast(audit, ".preview-word-grid") >= 4.5
        assert contrast(audit, ".preview-fallback strong", ".puzzle-preview-fallback") >= 4.5
        audit.screenshot(path=str(artifacts / "theme-library-mobile.png"), full_page=True)

        dashboard = admin.new_page(); dashboard.goto(base + "/admin", wait_until="networkidle")
        assert contrast(dashboard, ".review-count-grid a") >= 4.5
        assert contrast(dashboard, ".dashboard-actions a") >= 4.5
        dashboard.screenshot(path=str(artifacts / "theme-dashboard-desktop.png"), full_page=True)

        admin_mobile = browser.new_context(viewport={"width":390, "height":844}, is_mobile=True, has_touch=True)
        admin_mobile.add_cookies([{"name":"session", "value":cookie(ids[0]), "url":base}])
        admin_page = admin_mobile.new_page()
        admin_errors = []
        admin_page.on("pageerror", lambda error: admin_errors.append(str(error)))
        admin_routes = [
            "/generate", "/account", "/admin", "/admin/puzzles", "/admin/puzzles/manual",
            "/admin/generate", "/admin/word-banks", "/admin/word-banks/import", "/admin/dictionary",
            "/admin/tags", "/admin/activity", "/admin/publishing", "/admin/users", "/admin/roles",
            "/admin/audit", "/admin/settings", "/admin/themes",
        ] + [f"/admin/puzzles/theme-{kind}/edit" for kind in puzzle_checks]
        for route in admin_routes:
            response = admin_page.goto(base + route, wait_until="networkidle")
            assert response.ok, (route, response.status)
            assert admin_page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{route} overflows mobile viewport"
        assert not admin_errors, admin_errors
        admin_mobile.close()

        audit.emulate_media(media="print")
        for kind in puzzle_checks:
            response = audit.goto(base + f"/puzzles/theme-{kind}/print", wait_until="networkidle")
            assert response.ok, (kind, response.status)
            assert contrast(audit, ".print-sheet") >= 4.5, f"{kind} print sheet lost paper contrast"
            audit.screenshot(path=str(artifacts / f"theme-{kind}-print.png"), full_page=True)
        audit.emulate_media(media="screen")
        dashboard.close(); audit.close()

        assert player.request.post(base + "/account/appearance", form={"appearance_theme":"qa-midnight"}).ok
        page.reload()
        assert page.locator("html").get_attribute("data-theme") == "qa-midnight"
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--radius').trim()") == "10px"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(artifacts / "theme-account-mobile.png"), full_page=True)

        editor = admin.new_page(); editor.goto(base + "/admin/themes?edit=qa-midnight#theme-editor")
        assert editor.locator("html").get_attribute("data-theme") == "qa-midnight"
        assert editor.locator('[data-preview-theme="light"] p').evaluate("node => getComputedStyle(node).color") == "rgb(21, 21, 21)"
        assert editor.locator('[data-theme-field="name"]').input_value() == "QA Midnight"
        editor.locator('[data-theme-token="accent"]').fill("#70ddc4")
        assert editor.locator("[data-theme-preview]").evaluate("node => getComputedStyle(node).getPropertyValue('--accent').trim()") == "#70ddc4"
        assert editor.evaluate("document.documentElement.scrollWidth <= innerWidth")
        editor.screenshot(path=str(artifacts / "theme-admin-desktop.png"), full_page=True)

        assert admin.request.post(base + "/admin/themes/qa-midnight/toggle").ok
        page.reload()
        assert page.locator("html").get_attribute("data-theme") == "system"
        with SessionLocal() as db:
            assert db.get(User, ids[1]).appearance_theme == "qa-midnight", "Preference should survive temporary disable"
        assert admin.request.post(base + "/admin/themes/qa-midnight/toggle").ok
        page.reload(); assert page.locator("html").get_attribute("data-theme") == "qa-midnight"
        assert admin.request.post(base + "/admin/themes/qa-midnight/delete").ok
        page.reload(); assert page.locator("html").get_attribute("data-theme") == "system"
        guest.close(); browser.close()
    print("Theme modes, persistence, defaults, safe import/export, permissions, preview, accessibility validation and fallback passed.")
finally:
    server.terminate()
    server.wait(timeout=10)
    engine.dispose()
    for _ in range(20):
        try:
            database.unlink(missing_ok=True)
            break
        except PermissionError:
            time.sleep(.1)
