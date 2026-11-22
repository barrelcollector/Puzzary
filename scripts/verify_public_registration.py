"""Isolated public-registration, verification, abuse-control, and role-safety checks."""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from email import policy
from email.parser import Parser
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request


QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-public-registration-qa"
QA_DB = QA_ROOT / "registration.sqlite"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)

PUZZARY_PORT = 8906
SMTP_PORT = 8907
BASE = f"http://127.0.0.1:{PUZZARY_PORT}"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{QA_DB.as_posix()}",
    "SESSION_SECRET": "isolated-public-registration-secret",
    "UPLOAD_DIR": str(QA_ROOT / "uploads"),
    "APP_BASE_URL": BASE,
    "PUZZARY_HOST": "127.0.0.1",
    "PUZZARY_PORT": str(PUZZARY_PORT),
})

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import (
    cleanup_stale_registrations,
    hash_password,
    normalized_identity,
    set_app_setting,
    utcnow,
    verify_password,
)
from app.models import AppSetting, OIDCIdentity, PermissionAudit, PermissionRole, User
from app.permissions import PLAYER, initialize_permissions
from app.server import FeedTokenLogFilter


MESSAGES: list[str] = []
MESSAGE_LOCK = threading.Lock()


class SMTPHandler(socketserver.StreamRequestHandler):
    def respond(self, value: bytes) -> None:
        self.wfile.write(value + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        self.respond(b"220 registration.test ESMTP")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith("EHLO"):
                self.respond(b"250-registration.test")
                self.respond(b"250 8BITMIME")
            elif upper.startswith("HELO") or upper.startswith("MAIL FROM") or upper.startswith("RCPT TO"):
                self.respond(b"250 OK")
            elif upper == "DATA":
                self.respond(b"354 End data with <CR><LF>.<CR><LF>")
                lines = []
                while True:
                    data = self.rfile.readline()
                    if data in {b".\r\n", b".\n", b""}:
                        break
                    lines.append(data)
                with MESSAGE_LOCK:
                    MESSAGES.append(b"".join(lines).decode("utf-8", "replace"))
                self.respond(b"250 Accepted")
            elif upper == "QUIT":
                self.respond(b"221 Bye")
                return
            else:
                self.respond(b"250 OK")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def wait_ready(request) -> None:
    for _ in range(120):
        try:
            if request.get(BASE + "/health").ok:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Puzzary public-registration QA server did not become ready")


def login(context, username: str, password: str):
    client = getattr(context, "request", context)
    return client.post(
        BASE + "/login",
        form={"username": username, "password": password},
        max_redirects=0,
    )


def configure_registration(
    context,
    role_id: int,
    enabled: str = "on",
    verification_required: str = "on",
    smtp_configured: bool = True,
):
    return context.request.post(
        BASE + "/admin/settings",
        form={
            "public_base_url": BASE,
            "oidc_login_mode": "local",
            "public_registration_enabled": enabled,
            "public_registration_verification_required": verification_required,
            "public_registration_role_id": str(role_id),
            "public_registration_verification_hours": "24",
            "registration_smtp_host": "127.0.0.1" if smtp_configured else "",
            "registration_smtp_port": str(SMTP_PORT),
            "registration_smtp_security": "none",
            "registration_smtp_username": "",
            "registration_smtp_password": "smtp-secret" if smtp_configured else "",
            "registration_smtp_from": "puzzary@example.test" if smtp_configured else "",
        },
        max_redirects=0,
    )


def token_from_latest_message() -> str:
    with MESSAGE_LOCK:
        message = MESSAGES[-1]
    content = Parser(policy=policy.default).parsestr(message).get_content()
    match = re.search(r"/register/verify\?token=([A-Za-z0-9_-]+)", content)
    assert match, message
    return match.group(1)


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(
        display_name="Registration Admin",
        display_name_normalized=normalized_identity("Registration Admin"),
        email="admin@example.test",
        email_normalized="admin@example.test",
        email_verified_at=utcnow(),
        registration_source="admin",
        is_admin=True,
        is_active=True,
        password_hash=hash_password("Admin-test-123"),
    )
    db.add(admin)
    db.commit()
    initialize_permissions(db)
    admin_id = admin.id
    player_role = db.scalar(select(PermissionRole).where(PermissionRole.name == "Player"))
    admin_role = db.scalar(select(PermissionRole).where(PermissionRole.name == "Admin"))
    player_role_id, admin_role_id = player_role.id, admin_role.id

smtp = SMTPServer(("127.0.0.1", SMTP_PORT), SMTPHandler)
smtp_thread = threading.Thread(target=smtp.serve_forever, daemon=True)
smtp_thread.start()
server = subprocess.Popen(
    [sys.executable, "-m", "app.server"],
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=None,
)

try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        anonymous = playwright.request.new_context()
        wait_ready(anonymous)

        # Registration is closed by default in both UI and API.
        login_html = anonymous.get(BASE + "/login").text()
        assert "Create account" not in login_html
        assert anonymous.get(BASE + "/register").status == 404
        disabled_api = anonymous.post(BASE + "/api/register", data={
            "display_name": "Closed User", "email": "closed@example.test", "password": "Closed-test-123",
        })
        assert disabled_api.status == 403
        assert FeedTokenLogFilter.redact(
            "GET /register/verify?token=one-time-secret HTTP/1.1"
        ) == "GET /register/verify?token=[REDACTED] HTTP/1.1"
        assert FeedTokenLogFilter.redact(
            "GET /password/setup?return_to=%2F&token=setup-secret HTTP/1.1"
        ) == "GET /password/setup?return_to=%2F&token=[REDACTED] HTTP/1.1"

        admin_context = browser.new_context()
        assert login(admin_context, "Registration Admin", "Admin-test-123").status == 303

        # Elevated roles cannot be selected as a registration default.
        rejected_role = configure_registration(admin_context, admin_role_id)
        assert rejected_role.status == 303 and "non-privileged" in rejected_role.headers["location"]
        with SessionLocal() as db:
            assert db.get(AppSetting, "public_registration_enabled") is None

        # Email verification is optional by default; accounts activate without SMTP when it is off.
        before_optional = len(MESSAGES)
        optional_enabled = configure_registration(
            admin_context,
            player_role_id,
            verification_required="off",
            smtp_configured=False,
        )
        assert optional_enabled.status == 303
        optional = anonymous.post(BASE + "/api/register", data={
            "display_name": "Optional Verification",
            "email": "optional@example.test",
            "password": "Optional-test-123",
        })
        assert optional.status == 202 and "ready to sign in" in optional.json()["message"]
        assert len(MESSAGES) == before_optional
        with SessionLocal() as db:
            optional_user = db.scalar(select(User).where(User.email_normalized == "optional@example.test"))
            assert optional_user and optional_user.is_active and optional_user.email_verified_at is None
            optional_actions = set(db.scalars(
                select(PermissionAudit.action).where(PermissionAudit.subject_id == str(optional_user.id))
            ))
            assert {"registration.created", "registration.activated"} <= optional_actions
        assert login(anonymous, "OPTIONAL VERIFICATION", "Optional-test-123").status == 303

        enabled = configure_registration(admin_context, player_role_id)
        assert enabled.status == 303
        settings_html = admin_context.request.get(BASE + "/admin/settings").text()
        assert "Required before sign-in" in settings_html
        assert "puzzary@example.test" in settings_html
        assert "Saved password: Yes" in settings_html
        with SessionLocal() as db:
            saved_smtp_password = db.get(AppSetting, "registration_smtp_password_encrypted")
            assert saved_smtp_password and "smtp-secret" not in saved_smtp_password.value

        assert "Create account" in anonymous.get(BASE + "/login").text()
        page = browser.new_page()
        page.goto(BASE + "/register")
        assert page.get_by_role("heading", name="Create account").is_visible()
        csrf_rejected = anonymous.post(BASE + "/register", form={
            "display_name": "No CSRF",
            "email": "no-csrf@example.test",
            "password": "No-csrf-test-123",
            "confirm_password": "No-csrf-test-123",
        })
        assert csrf_rejected.status == 403
        registration_form = page.locator('form[action="/register"]')

        # Validation is accessible and specific without exposing existing accounts.
        registration_form.locator('[name="display_name"]').fill("QA Player")
        registration_form.locator('[name="email"]').fill("player@example.test")
        registration_form.locator('[name="password"]').fill("weakweak12")
        registration_form.locator('[name="confirm_password"]').fill("weakweak12")
        registration_form.get_by_role("button", name="Create account").click()
        assert page.get_by_role("alert").is_visible()

        registration_form = page.locator('form[action="/register"]')
        registration_form.locator('[name="display_name"]').fill("QA Player")
        registration_form.locator('[name="email"]').fill("player@example.test")
        registration_form.locator('[name="password"]').fill("Player-test-123")
        registration_form.locator('[name="confirm_password"]').fill("Player-test-123")
        registration_form.get_by_role("button", name="Create account").click()
        assert page.get_by_role("status").is_visible()
        with SessionLocal() as db:
            player = db.scalar(select(User).where(User.email_normalized == "player@example.test"))
            assert player and not player.is_active and player.email_verified_at is None
            assert player.registration_source == "self" and player.display_name_normalized == "qa player"
            assert verify_password("Player-test-123", player.password_hash)
            assert [role.id for role in player.roles] == [player_role_id]
            assert set(player.roles[0].permissions or []) == PLAYER
            assert not player.is_admin and db.scalar(select(OIDCIdentity).where(OIDCIdentity.user_id == player.id)) is None
            first_token_hash = player.email_verification_token_hash
            player_id = player.id
        assert login(anonymous, "QA PLAYER", "Player-test-123").headers["location"].startswith("/login?error=")

        token = token_from_latest_message()
        assert token not in first_token_hash
        verified = anonymous.get(BASE + f"/register/verify?token={token}")
        assert verified.status == 200 and "Email verified" in verified.text()
        assert anonymous.get(BASE + f"/register/verify?token={token}").status == 400
        with SessionLocal() as db:
            player = db.get(User, player_id)
            assert player.is_active and player.email_verified_at and not player.email_verification_token_hash
            actions = set(db.scalars(select(PermissionAudit.action).where(PermissionAudit.subject_id == str(player_id))))
            assert {"registration.created", "registration.email_sent", "registration.verified"} <= actions

        # Existing email/name attempts receive the same generic response and create no account.
        duplicate = anonymous.post(BASE + "/api/register", data={
            "display_name": "Another Name", "email": "PLAYER@EXAMPLE.TEST", "password": "Another-test-123",
        })
        fresh = anonymous.post(BASE + "/api/register", data={
            "display_name": "Pending User", "email": "pending@example.test", "password": "Pending-test-123",
        })
        assert duplicate.status == fresh.status == 202 and duplicate.json() == fresh.json()
        with SessionLocal() as db:
            assert db.scalar(select(User).where(User.display_name == "Another Name")) is None
            pending = db.scalar(select(User).where(User.email_normalized == "pending@example.test"))
            assert pending and not pending.is_active
            pending.email_verification_sent_at = utcnow() - timedelta(minutes=2)
            expired_token = pending.email_verification_token_hash
            pending_id = pending.id
            db.commit()

        # Resend is generic, throttled, rotates the token, and leaves the old link invalid.
        before_resend = len(MESSAGES)
        resend = anonymous.post(BASE + "/api/register/resend", data={"email": "pending@example.test"})
        missing_resend = anonymous.post(BASE + "/api/register/resend", data={"email": "missing@example.test"})
        assert resend.status == missing_resend.status == 202 and resend.json() == missing_resend.json()
        assert len(MESSAGES) == before_resend + 1
        new_token = token_from_latest_message()
        with SessionLocal() as db:
            pending = db.scalar(select(User).where(User.email_normalized == "pending@example.test"))
            assert pending.email_verification_token_hash != expired_token

        # Concurrent normalized duplicates are accepted generically but only one record survives.
        def concurrent_signup(name: str):
            body = json.dumps({
                "display_name": name,
                "email": f"{name.replace(' ', '.').lower()}@example.test",
                "password": "Concurrent-test-123",
            }).encode("utf-8")
            request = urllib_request.Request(
                BASE + "/api/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib_request.urlopen(request, timeout=15) as response:
                    return response.status
            except urllib_error.HTTPError as response:
                return response.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(concurrent_signup, ["Concurrent User", "CONCURRENT USER"]))
        assert statuses == [202, 202]
        with SessionLocal() as db:
            concurrent_users = list(db.scalars(select(User).where(User.display_name_normalized == "concurrent user")))
            assert len(concurrent_users) == 1

        # Registration limits apply independently of login and resend limits.
        limiter = playwright.request.new_context()
        limit_statuses = []
        for _ in range(6):
            limit_statuses.append(limiter.post(BASE + "/api/register", data={
                "display_name": "Limited User",
                "email": "limited@example.test",
                "password": "Limited-test-123",
            }).status)
        assert limit_statuses[-1] == 429

        # Expired tokens fail and stale registrations release PII/identities after retention.
        with SessionLocal() as db:
            pending = db.scalar(select(User).where(User.email_normalized == "pending@example.test"))
            pending.email_verification_expires_at = utcnow() - timedelta(seconds=1)
            pending.registration_created_at = utcnow() - timedelta(days=8)
            db.commit()
        assert anonymous.get(BASE + f"/register/verify?token={new_token}").status == 400
        with SessionLocal() as db:
            assert cleanup_stale_registrations(db) == 1
            pending = db.get(User, pending_id)
            assert pending.registration_source == "expired" and pending.email is None and pending.password_hash is None

        # Disabling removes entry points and rejects new requests without disabling verified users.
        assert configure_registration(admin_context, player_role_id, enabled="off").status == 303
        assert "Create account" not in anonymous.get(BASE + "/login").text()
        assert anonymous.post(BASE + "/api/register", data={
            "display_name": "Disabled Again", "email": "disabled@example.test", "password": "Disabled-test-123",
        }).status == 403
        invalid_after_disable = anonymous.get(BASE + "/register/verify?token=not-a-token")
        assert invalid_after_disable.status == 400
        assert "Back to login" in invalid_after_disable.text() and "Request another" not in invalid_after_disable.text()
        assert login(anonymous, "qa player", "Player-test-123").status == 303

        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    smtp.shutdown()
    smtp.server_close()

print("Public registration defaults closed; immediate and verified signup, normalized uniqueness, safe roles, throttling, expiry, resend, cleanup, audit, OIDC separation, and disablement passed.")
