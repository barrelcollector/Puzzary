"""Clean-install, one-time setup, expiry, and upgrade checks for issue #92."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit


QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-first-run-admin-qa"
QA_ROOT.mkdir(parents=True, exist_ok=True)
QA_DB = QA_ROOT / "first-run.sqlite"
QA_DATA = QA_ROOT / "data"
QA_UPLOADS = QA_DATA / "uploads"
DELIVERY_FILE = QA_DATA / "bootstrap-admin-setup.txt"
SERVER_LOG = QA_ROOT / "server.log"
BASE = "http://127.0.0.1:8820"

os.environ["DATABASE_URL"] = f"sqlite:///{QA_DB.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-first-run-admin-secret"
os.environ["UPLOAD_DIR"] = str(QA_UPLOADS)
os.environ["APP_BASE_URL"] = BASE
os.environ["PUZZARY_BOOTSTRAP_ADMIN_NAME"] = "admin"

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import as_utc, ensure_admin_user, hash_password, hash_secret, utcnow, verify_password
from app.models import User
from app.permissions import initialize_permissions


def reset_database() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    DELIVERY_FILE.unlink(missing_ok=True)


def delivery_token() -> str:
    content = DELIVERY_FILE.read_text(encoding="utf-8")
    setup_line = next(line for line in content.splitlines() if line.startswith("Setup link: "))
    link = setup_line.removeprefix("Setup link: ")
    parsed = urlsplit(link)
    assert not parsed.query and parsed.fragment.startswith("token=")
    return parsed.fragment.removeprefix("token=")


reset_database()
with SessionLocal() as db:
    ensure_admin_user(db)
    initialize_permissions(db)
    admin = db.scalar(select(User).where(User.display_name == "admin"))
    assert admin is not None and admin.is_admin and admin.is_active
    assert admin.password_hash is None
    assert not verify_password("admin", admin.password_hash)
    assert admin.reset_token_hash and as_utc(admin.reset_token_expires_at) > utcnow()
    token = delivery_token()
    assert admin.reset_token_hash == hash_secret(token)
    assert token not in admin.reset_token_hash
    first_hash = admin.reset_token_hash
first_delivery = DELIVERY_FILE.read_text(encoding="utf-8")
first_mtime = DELIVERY_FILE.stat().st_mtime_ns
with SessionLocal() as db:
    ensure_admin_user(db)
    admin = db.scalar(select(User).where(User.display_name == "admin"))
    assert admin.reset_token_hash == first_hash
assert DELIVERY_FILE.read_text(encoding="utf-8") == first_delivery
assert DELIVERY_FILE.stat().st_mtime_ns == first_mtime

with SERVER_LOG.open("w", encoding="utf-8") as output:
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8820"],
        stdout=output,
        stderr=subprocess.STDOUT,
    )
try:
    with sync_playwright() as playwright:
        client = playwright.request.new_context(base_url=BASE)
        for _ in range(120):
            try:
                if client.get("/health").ok:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("First-run QA server did not start")

        failed_login = client.post(
            "/login", form={"username": "admin", "password": "admin"}, max_redirects=0
        )
        assert failed_login.status == 303
        assert "Invalid%20username" in failed_login.headers["location"]
        assert "First-time administrator setup is waiting" in client.get("/login").text()
        browser = playwright.chromium.launch(headless=True)
        setup_page = browser.new_page()
        setup_page.goto(f"{BASE}/password/setup#token={token}")
        assert setup_page.locator("#password-setup-form").is_visible()
        assert setup_page.locator("#password-setup-token").input_value() == token
        assert setup_page.url == f"{BASE}/password/setup"
        browser.close()

        password = "First-run-admin-2026!"
        completed = client.post(
            "/password/setup",
            form={"token": token, "password": password, "confirm_password": password},
        )
        assert completed.ok and completed.url.endswith("/library")
        assert not DELIVERY_FILE.exists()
        assert "invalid or expired" in client.get(f"/password/setup?token={token}").text()
        replay = client.post(
            "/password/setup",
            form={"token": token, "password": password, "confirm_password": password},
            max_redirects=0,
        )
        assert replay.status == 303 and "invalid%20or%20expired" in replay.headers["location"]
        fresh_client = playwright.request.new_context(base_url=BASE)
        login = fresh_client.post(
            "/login", form={"username": "admin", "password": password}, max_redirects=0
        )
        assert login.status == 303 and login.headers["location"] == "/library"
        fresh_client.dispose()
        client.dispose()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()

with SessionLocal() as db:
    admin = db.scalar(select(User).where(User.display_name == "admin"))
    assert admin.password_hash and verify_password("First-run-admin-2026!", admin.password_hash)
    assert admin.reset_token_hash is None and admin.reset_token_expires_at is None
server_output = SERVER_LOG.read_text(encoding="utf-8")
assert token not in server_output and "Setup link:" not in server_output

# An expired unused credential is replaced once on the next startup.
reset_database()
with SessionLocal() as db:
    ensure_admin_user(db)
    old_token = delivery_token()
    admin = db.scalar(select(User).where(User.display_name == "admin"))
    old_hash = admin.reset_token_hash
    admin.reset_token_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
with SessionLocal() as db:
    ensure_admin_user(db)
    admin = db.scalar(select(User).where(User.display_name == "admin"))
    replacement = delivery_token()
    assert replacement != old_token and admin.reset_token_hash != old_hash

# Existing installations retain accounts, password hashes, and administrator state.
reset_database()
existing_hash = hash_password("Existing-admin-password-2026!")
player_hash = hash_password("Existing-player-password-2026!")
with SessionLocal() as db:
    db.add_all([
        User(display_name="Existing Admin", is_admin=True, is_active=True, password_hash=existing_hash),
        User(display_name="Existing Player", is_admin=False, is_active=True, password_hash=player_hash),
    ])
    db.commit()
    ensure_admin_user(db)
    users = list(db.scalars(select(User).order_by(User.id)))
    assert len(users) == 2
    assert users[0].display_name == "Existing Admin" and users[0].password_hash == existing_hash
    assert users[1].display_name == "Existing Player" and users[1].password_hash == player_hash
assert not DELIVERY_FILE.exists()

# A legacy database with users but no administrator promotes the oldest account
# without changing its established password.
reset_database()
legacy_hash = hash_password("Legacy-user-password-2026!")
with SessionLocal() as db:
    db.add(User(display_name="Legacy User", is_admin=False, is_active=True, password_hash=legacy_hash))
    db.commit()
    ensure_admin_user(db)
    legacy = db.scalar(select(User).where(User.display_name == "Legacy User"))
    assert legacy.is_admin and legacy.is_active and legacy.password_hash == legacy_hash
    assert legacy.reset_token_hash is None
assert not DELIVERY_FILE.exists()

print(
    "First-run admin has no default password; setup delivery, expiry, one-time use, "
    "login, upgrade preservation, and legacy promotion passed."
)
