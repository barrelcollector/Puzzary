"""Isolated web-first plugin management and supporting API checks."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from app.plugin_installer import test_github_access


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-plugin-management-qa"
shutil.rmtree(QA_ROOT, ignore_errors=True)
PLUGIN_DIR = QA_ROOT / "plugins"
PACKAGE = PLUGIN_DIR / "example_clock"
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
    "author": "Puzzary QA",
    "description": "Exercises managed plugin metadata and settings.",
    "license": "Apache-2.0",
    "min_core_version": "0.1.0",
    "capabilities": ["puzzles.generate", "puzzles.play"],
    "requires": [],
    "settings": [
        {"name": "endpoint", "label": "Service URL", "type": "text", "required": True, "default": "https://example.test"},
        {"name": "mode", "label": "Mode", "type": "select", "options": ["quiet", "loud"], "default": "quiet"},
        {"name": "api_key", "label": "API key", "type": "secret"},
        {"name": "alerts", "label": "Alerts", "type": "checkbox", "default": False},
    ],
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
ROLLBACK = PLUGIN_DIR / ".example_clock.rollback"
shutil.copytree(PACKAGE, ROLLBACK)
rollback_manifest = json.loads((ROLLBACK / "puzzary-plugin.json").read_text(encoding="utf-8"))
rollback_manifest["version"] = "0.9.0"
(ROLLBACK / "puzzary-plugin.json").write_text(json.dumps(rollback_manifest), encoding="utf-8")

UPLOAD_SOURCE = QA_ROOT / "upload-source"
UPLOAD_SOURCE.mkdir()
upload_manifest = {
    "schema": "puzzary.plugin.v1", "api_version": "1", "slug": "uploaded_timer",
    "name": "Uploaded Timer", "version": "1.0.0", "entrypoint": "plugin.py",
    "author": "Puzzary QA", "license": "Apache-2.0",
    "capabilities": ["puzzles.generate"], "requires": [],
}
(UPLOAD_SOURCE / "puzzary-plugin.json").write_text(json.dumps(upload_manifest), encoding="utf-8")
(UPLOAD_SOURCE / "plugin.py").write_text("# upload fixture\n", encoding="utf-8")
UPLOAD_ARCHIVE = QA_ROOT / "uploaded-timer.zip"
with zipfile.ZipFile(UPLOAD_ARCHIVE, "w", zipfile.ZIP_DEFLATED) as archive:
    for source in UPLOAD_SOURCE.iterdir():
        archive.write(source, source.name)
UPLOAD_DIGEST = hashlib.sha256(UPLOAD_ARCHIVE.read_bytes()).hexdigest()
missing_dependency_manifest = {**upload_manifest, "slug": "needs_missing", "name": "Needs Missing", "requires": ["missing_plugin"]}
(UPLOAD_SOURCE / "puzzary-plugin.json").write_text(json.dumps(missing_dependency_manifest), encoding="utf-8")
MISSING_DEPENDENCY_ARCHIVE = QA_ROOT / "needs-missing.zip"
with zipfile.ZipFile(MISSING_DEPENDENCY_ARCHIVE, "w", zipfile.ZIP_DEFLATED) as archive:
    for source in UPLOAD_SOURCE.iterdir():
        archive.write(source, source.name)
MISSING_DEPENDENCY_DIGEST = hashlib.sha256(MISSING_DEPENDENCY_ARCHIVE.read_bytes()).hexdigest()

environment = os.environ.copy()
environment.update({
    "DATABASE_URL": f"sqlite:///{(QA_ROOT / 'plugins.sqlite').as_posix()}",
    "SESSION_SECRET": "isolated-plugin-management-secret",
    "UPLOAD_DIR": str(QA_ROOT / "uploads"),
    "PUZZARY_PLUGIN_DIR": str(PLUGIN_DIR),
    "APP_BASE_URL": "http://127.0.0.1:8818",
})
BASE = "http://127.0.0.1:8818"
ADMIN_PASSWORD = "Plugin-management-admin-2026!"


class GitHubResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return json.dumps({
            "full_name": "barrelcollector/puzzary-official-plugins",
            "private": True,
        }).encode("utf-8")


with patch("app.plugin_installer.urllib.request.urlopen", return_value=GitHubResponse()) as github_request:
    checked = test_github_access("write-only-test-token")
    assert checked == {"repository": "barrelcollector/puzzary-official-plugins", "private": True}
    sent_request = github_request.call_args.args[0]
    assert sent_request.headers["Authorization"] == "Bearer write-only-test-token"

server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8818"],
    cwd=ROOT,
    env=environment,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        request = playwright.request.new_context()
        for _ in range(100):
            try:
                if request.get(BASE + "/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("Plugin management QA server did not start")

        delivery = (QA_ROOT / "bootstrap-admin-setup.txt").read_text(encoding="utf-8")
        setup_link = next(line.removeprefix("Setup link: ") for line in delivery.splitlines() if line.startswith("Setup link: "))
        setup_token = setup_link.split("#token=", 1)[1]
        setup = request.post(BASE + "/password/setup", form={
            "token": setup_token,
            "password": ADMIN_PASSWORD,
            "confirm_password": ADMIN_PASSWORD,
        }, max_redirects=0)
        assert setup.status == 303 and setup.headers["location"] == "/library", setup.text()
        assert not (QA_ROOT / "bootstrap-admin-setup.txt").exists()

        login = request.post(BASE + "/login", form={"username": "admin", "password": ADMIN_PASSWORD}, max_redirects=0)
        assert login.status == 303, login.text()

        page = request.get(BASE + "/admin/plugins")
        assert page.ok, page.text()
        html = page.text()
        for phrase in (
            "Puzzle Plugins", "Update All Modules", "Official Modules", "Update Module",
            "Add From GitHub", "Example Clock", "Additional Installed Modules",
            "GitHub packages loaded", "Bundled fallback", "Restore 0.9.0",
            "Public release catalog", "Trusted code only", "Upload Local Package",
            "Puzzary QA", "Apache-2.0", "puzzles.generate", "Configure",
        ):
            assert phrase in html, phrase
        assert html.count('action="/admin/plugins/install-official"') == 11
        assert len(re.findall(r'action="/admin/plugins/[^\"]+/status"', html)) == 11
        assert "Enabled Modules" not in html
        assert html.index("Additional Installed Modules") < html.index("Add From GitHub")
        assert html.index("Add From GitHub") < html.index("Private GitHub Access")
        assert 'action="/admin/plugins/install-upload"' in html
        assert 'action="/admin/plugins/example_clock/uninstall"' in html

        settings = request.get(BASE + "/admin/settings")
        assert settings.ok and 'href="/admin/plugins"' in settings.text()
        assert 'name="disabled_puzzle_types"' not in settings.text()

        catalog = request.get(BASE + "/api/plugin-packages/catalog")
        assert catalog.ok and len(catalog.json()["plugins"]) == 10
        status = request.get(BASE + "/api/plugin-packages")
        assert status.ok and status.json()["external_count"] == 1
        assert status.json()["custom_installed"][0]["slug"] == "example_clock"
        github_status = request.get(BASE + "/api/plugin-packages/github-access")
        assert github_status.ok and github_status.json() == {
            "configured": False,
            "source": "none",
            "stored": False,
            "environment_fallback": False,
        }
        saved_access = request.put(BASE + "/api/plugin-packages/github-access", data={
            "token": "write-only-test-token",
        })
        assert saved_access.ok and saved_access.json()["source"] == "saved"
        saved_page = request.get(BASE + "/admin/plugins").text()
        assert "Saved in Puzzary" in saved_page
        assert 'type="password"' in saved_page
        assert "write-only-test-token" not in saved_page
        with sqlite3.connect(QA_ROOT / "plugins.sqlite") as database:
            encrypted = database.execute(
                "SELECT value FROM app_settings WHERE key = 'plugin_github_token_encrypted'"
            ).fetchone()[0]
        assert encrypted and encrypted != "write-only-test-token"
        removed_access = request.delete(BASE + "/api/plugin-packages/github-access")
        assert removed_access.ok and removed_access.json()["configured"] is False
        missing_test = request.post(BASE + "/admin/plugins/github-access/test", max_redirects=0)
        assert missing_test.status == 303 and "error=" in missing_test.headers["location"]
        web_saved = request.post(BASE + "/admin/plugins/github-access", form={
            "github_token": "second-write-only-token",
        }, max_redirects=0)
        assert web_saved.status == 303 and "message=" in web_saved.headers["location"]
        assert "second-write-only-token" not in request.get(BASE + "/api/plugin-packages").text()
        web_removed = request.post(BASE + "/admin/plugins/github-access/remove", max_redirects=0)
        assert web_removed.status == 303

        bad_web_install = request.post(BASE + "/admin/plugins/install", form={
            "url": "https://example.invalid/plugin.zip",
            "sha256": "0" * 64,
        }, max_redirects=0)
        assert bad_web_install.status == 303 and "error=" in bad_web_install.headers["location"]
        unknown_official = request.post(
            BASE + "/admin/plugins/install-official",
            form={"slug": "not_a_plugin"},
            max_redirects=0,
        )
        assert unknown_official.status == 303 and "error=" in unknown_official.headers["location"]
        bad_api_install = request.post(BASE + "/api/plugin-packages/install", data={
            "url": "https://example.invalid/plugin.zip",
            "sha256": "0" * 64,
        })
        assert bad_api_install.status == 400

        settings_saved = request.put(BASE + "/api/plugin-packages/example_clock/settings", data={
            "values": {"endpoint": "https://service.test", "mode": "loud", "api_key": "super-secret", "alerts": True},
        })
        assert settings_saved.ok, settings_saved.text()
        assert settings_saved.json()["values"]["api_key"] == ""
        assert settings_saved.json()["secret_configured"]["api_key"] is True
        settings_read = request.get(BASE + "/api/plugin-packages/example_clock/settings")
        assert settings_read.ok and "super-secret" not in settings_read.text()
        with sqlite3.connect(QA_ROOT / "plugins.sqlite") as database:
            encrypted_settings = database.execute(
                "SELECT value FROM app_settings WHERE key = 'plugin_config:example_clock'"
            ).fetchone()[0]
        assert encrypted_settings and "super-secret" not in encrypted_settings
        web_settings = request.post(BASE + "/admin/plugins/example_clock/settings", form={
            "endpoint": "https://web.test", "mode": "quiet", "api_key": "", "alerts": "true",
        }, max_redirects=0)
        assert web_settings.status == 303 and "message=" in web_settings.headers["location"]

        rejected_upload = request.post(BASE + "/api/plugin-packages/install-upload", multipart={
            "package": {"name": "uploaded-timer.zip", "mimeType": "application/zip", "buffer": UPLOAD_ARCHIVE.read_bytes()},
            "sha256": UPLOAD_DIGEST,
            "trust_package": "false",
        })
        assert rejected_upload.status == 422
        wrong_digest = request.post(BASE + "/api/plugin-packages/install-upload", multipart={
            "package": {"name": "uploaded-timer.zip", "mimeType": "application/zip", "buffer": UPLOAD_ARCHIVE.read_bytes()},
            "sha256": "0" * 64,
            "trust_package": "true",
        })
        assert wrong_digest.status == 400 and "digest mismatch" in wrong_digest.text()
        missing_dependency = request.post(BASE + "/api/plugin-packages/install-upload", multipart={
            "package": {"name": "needs-missing.zip", "mimeType": "application/zip", "buffer": MISSING_DEPENDENCY_ARCHIVE.read_bytes()},
            "sha256": MISSING_DEPENDENCY_DIGEST,
            "trust_package": "true",
        })
        assert missing_dependency.status == 400 and "missing required plugin dependencies" in missing_dependency.text()
        uploaded = request.post(BASE + "/api/plugin-packages/install-upload", multipart={
            "package": {"name": "uploaded-timer.zip", "mimeType": "application/zip", "buffer": UPLOAD_ARCHIVE.read_bytes()},
            "sha256": UPLOAD_DIGEST,
            "trust_package": "true",
        })
        assert uploaded.ok and uploaded.json()["slug"] == "uploaded_timer", uploaded.text()
        uploaded_status = request.get(BASE + "/api/plugin-packages").json()
        timer = next(item for item in uploaded_status["custom_installed"] if item["slug"] == "uploaded_timer")
        assert timer["installed_sha256"] == UPLOAD_DIGEST
        assert timer["install_source"].startswith("local upload:")
        uninstalled = request.delete(BASE + "/api/plugin-packages/uploaded_timer")
        assert uninstalled.ok and uninstalled.json()["rollback_available"] is True
        assert uninstalled.json()["data_preserved"] is True
        restored_upload = request.post(BASE + "/api/plugin-packages/uploaded_timer/rollback")
        assert restored_upload.ok and restored_upload.json()["version"] == "1.0.0"

        disabled = request.post(BASE + "/admin/plugins/maze/status", form={"enabled": "false"}, max_redirects=0)
        assert disabled.status == 303
        disabled_page = request.get(BASE + "/admin/plugins").text()
        assert re.search(r'action="/admin/plugins/maze/status">\s*<input type="hidden" name="enabled" value="true">', disabled_page)
        enabled = request.patch(BASE + "/api/plugin-packages/maze", data={"enabled": True})
        assert enabled.ok and enabled.json() == {"slug": "maze", "enabled": True}
        web_rollback = request.post(BASE + "/admin/plugins/example_clock/rollback", max_redirects=0)
        assert web_rollback.status == 303
        rolled_back = request.get(BASE + "/api/plugin-packages").json()
        assert rolled_back["custom_installed"][0]["version"] == "0.9.0"
        assert rolled_back["restart_required"] is True
        api_rollback = request.post(BASE + "/api/plugin-packages/example_clock/rollback")
        assert api_rollback.ok and api_rollback.json()["version"] == "1.0.0"

        request.dispose()
        browser = playwright.chromium.launch()
        for width in (1280, 390):
            context = browser.new_context(viewport={"width": width, "height": 900})
            login = context.request.post(BASE + "/login", form={"username": "admin", "password": ADMIN_PASSWORD}, max_redirects=0)
            assert login.status == 303
            page = context.new_page()
            page.goto(BASE + "/admin/plugins")
            assert page.locator(".plugin-card").count() == 12
            assert page.locator('.plugin-card form[action$="/status"]').count() == 12
            assert page.get_by_role("button", name="Update Module").count() == 10
            assert page.locator('form[action="/admin/plugins/install"]').is_visible()
            assert page.locator('form[action="/admin/plugins/install-upload"]').is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            context.close()
        browser.close()
    print("Plugin web UI, trusted local upload, encrypted settings, metadata, recoverable uninstall, GitHub access, status controls, and supporting APIs passed.")
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    shutil.rmtree(QA_ROOT, ignore_errors=True)
