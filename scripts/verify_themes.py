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
from app.models import AppearanceTheme, User


Base.metadata.create_all(engine)
with SessionLocal() as db:
    users = [
        User(display_name="Theme Admin", is_admin=True, is_active=True, session_version=1, password_hash=hash_password("Theme-test-123")),
        User(display_name="Theme Player", is_admin=False, is_active=True, session_version=1, password_hash=hash_password("Theme-test-123")),
    ]
    db.add_all(users)
    db.commit()
    ids = [user.id for user in users]


def cookie(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id":user_id, "session_version":1}).encode())
    return TimestampSigner(SESSION_SECRET).sign(payload).decode()


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
