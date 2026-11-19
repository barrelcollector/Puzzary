"""Isolated OIDC discovery, PKCE, linking, provisioning, recovery, UI and API checks."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-oidc-qa"
QA_DB = QA_ROOT / "oidc.sqlite"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)

PUZZARY_PORT = 8895
IDP_PORT = 8894
PUZZARY_BASE = f"http://127.0.0.1:{PUZZARY_PORT}"
IDP_BASE = f"http://127.0.0.1:{IDP_PORT}"
SESSION_SECRET = "isolated-oidc-session-secret"
CLIENT_ID = "puzzary-qa-client"
CLIENT_SECRET = "puzzary-qa-secret"

os.environ["DATABASE_URL"] = f"sqlite:///{QA_DB.as_posix()}"
os.environ["SESSION_SECRET"] = SESSION_SECRET
os.environ["UPLOAD_DIR"] = str(QA_ROOT / "uploads")
os.environ["APP_BASE_URL"] = PUZZARY_BASE
os.environ["PUZZARY_HOST"] = "127.0.0.1"
os.environ["PUZZARY_PORT"] = str(PUZZARY_PORT)

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import hash_password, hash_secret
from app.models import OIDCIdentity, OIDCProvider, User
from app.permissions import initialize_permissions


PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
PUBLIC_JWK.update({"kid": "qa-key", "use": "sig", "alg": "RS256"})
ISSUED_CODES: dict[str, dict] = {}
TOKENS: dict[str, dict] = {}
IDP_USER = {
    "sub": "qa-admin-subject",
    "name": "QA Admin Identity",
    "preferred_username": "qa-admin",
    "email": "admin@example.test",
    "email_verified": True,
}


class FakeOIDCHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def json_response(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/.well-known/openid-configuration":
            assert self.headers.get("User-Agent") == "Puzzary/0.1 OIDC client"
            return self.json_response({
                "issuer": IDP_BASE,
                "authorization_endpoint": f"{IDP_BASE}/authorize",
                "token_endpoint": f"{IDP_BASE}/token",
                "userinfo_endpoint": f"{IDP_BASE}/userinfo",
                "jwks_uri": f"{IDP_BASE}/jwks",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "profile", "email"],
            })
        if parsed.path == "/jwks":
            assert self.headers.get("User-Agent") == "Puzzary/0.1 OIDC client"
            return self.json_response({"keys": [PUBLIC_JWK]})
        if parsed.path == "/authorize":
            query = parse_qs(parsed.query)
            required = ["client_id", "redirect_uri", "state", "nonce", "code_challenge"]
            if any(not query.get(name) for name in required) or query["client_id"][0] != CLIENT_ID:
                return self.json_response({"error": "invalid_request"}, 400)
            assert query.get("code_challenge_method") == ["S256"]
            code = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
            ISSUED_CODES[code] = {
                "redirect_uri": query["redirect_uri"][0],
                "nonce": query["nonce"][0],
                "challenge": query["code_challenge"][0],
                "user": dict(IDP_USER),
            }
            location = f"{query['redirect_uri'][0]}?{urlencode({'code': code, 'state': query['state'][0]})}"
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        if parsed.path == "/userinfo":
            assert self.headers.get("User-Agent") == "Puzzary/0.1 OIDC client"
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            claims = TOKENS.get(token)
            if claims is None:
                return self.json_response({"error": "invalid_token"}, 401)
            return self.json_response(claims)
        return self.json_response({"error": "not_found"}, 404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/token":
            return self.json_response({"error": "not_found"}, 404)
        assert self.headers.get("User-Agent") == "Puzzary/0.1 OIDC client"
        auth = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if auth != expected:
            return self.json_response({"error": "invalid_client"}, 401)
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        code = form.get("code", [""])[0]
        issued = ISSUED_CODES.pop(code, None)
        verifier = form.get("code_verifier", [""])[0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if not issued or challenge != issued["challenge"] or form.get("redirect_uri", [""])[0] != issued["redirect_uri"]:
            return self.json_response({"error": "invalid_grant"}, 400)
        now = int(time.time())
        claims = {
            **issued["user"],
            "iss": IDP_BASE,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": issued["nonce"],
        }
        id_token = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "qa-key"})
        access_token = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
        TOKENS[access_token] = dict(issued["user"])
        return self.json_response({
            "token_type": "Bearer",
            "access_token": access_token,
            "id_token": id_token,
            "expires_in": 300,
        })


def wait_ready(request) -> None:
    for _ in range(120):
        try:
            if request.get(PUZZARY_BASE + "/health").ok:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Puzzary OIDC QA server did not become ready")


def login(context, username: str, password: str):
    return context.request.post(
        PUZZARY_BASE + "/login",
        form={"username": username, "password": password},
        max_redirects=0,
    )


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(
        display_name="OIDC Admin",
        email="admin@example.test",
        is_admin=True,
        is_active=True,
        password_hash=hash_password("Admin-test-123"),
        api_token_hash=hash_secret("oidc-admin-api-token"),
        api_token_prefix="oidc-admin",
    )
    player = User(
        display_name="OIDC Player",
        email="player@example.test",
        is_active=True,
        password_hash=hash_password("Player-test-123"),
    )
    db.add_all([admin, player])
    db.commit()
    initialize_permissions(db)
    admin_id, player_id = admin.id, player.id

idp = ThreadingHTTPServer(("127.0.0.1", IDP_PORT), FakeOIDCHandler)
idp_thread = threading.Thread(target=idp.serve_forever, daemon=True)
idp_thread.start()
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

        default_login = anonymous.get(PUZZARY_BASE + "/login").text()
        assert "Puzzary accounts only (default)" not in default_login
        assert 'data-local-login' in default_login and "Sign in with" not in default_login

        admin_context = browser.new_context()
        assert login(admin_context, "OIDC Admin", "Admin-test-123").status == 303
        created = admin_context.request.post(
            PUZZARY_BASE + "/admin/settings/oidc",
            form={
                "display_name": "QA Identity",
                "issuer_url": IDP_BASE,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scopes": "openid profile email",
                "provisioning_mode": "link_only",
            },
            max_redirects=0,
        )
        assert created.status == 303
        with SessionLocal() as db:
            provider = db.scalar(select(OIDCProvider))
            assert provider and CLIENT_SECRET not in provider.client_secret_encrypted
            provider_id = provider.id
            assert not provider.enabled and provider.last_tested_at is None

        test = admin_context.request.post(
            f"{PUZZARY_BASE}/admin/settings/oidc/{provider_id}/test"
        )
        assert test.ok and "Single sign-on test passed" in test.text(), (test.status, test.text()[:500])
        with SessionLocal() as db:
            provider = db.get(OIDCProvider, provider_id)
            assert provider.last_test_status == "ok" and provider.last_tested_at

        enabled = admin_context.request.post(
            f"{PUZZARY_BASE}/admin/settings/oidc/{provider_id}",
            form={
                "display_name": "QA Identity",
                "issuer_url": IDP_BASE,
                "client_id": CLIENT_ID,
                "client_secret": "",
                "scopes": "openid profile email",
                "provisioning_mode": "link_only",
                "enabled": "on",
            },
            max_redirects=0,
        )
        assert enabled.status == 303
        mode = admin_context.request.post(
            PUZZARY_BASE + "/admin/settings",
            form={"public_base_url": PUZZARY_BASE, "oidc_login_mode": "mixed"},
            max_redirects=0,
        )
        assert mode.status == 303

        mixed_login = anonymous.get(PUZZARY_BASE + "/login").text()
        assert "Sign in with QA Identity" in mixed_login and 'data-local-login' in mixed_login

        player_context = browser.new_context()
        assert login(player_context, "OIDC Player", "Player-test-123").status == 303
        IDP_USER.update({
            "sub": "qa-player-subject",
            "name": "OIDC Player",
            "preferred_username": "oidc-player",
            "email": "player@example.test",
            "email_verified": True,
        })
        linked = player_context.request.post(
            f"{PUZZARY_BASE}/account/oidc/{provider_id}/link"
        )
        assert linked.ok and "Single sign-on identity linked" in linked.text()
        with SessionLocal() as db:
            identity = db.scalar(select(OIDCIdentity).where(OIDCIdentity.user_id == player_id))
            assert identity and identity.subject == "qa-player-subject"
            assert identity.claims.get("email_verified") is True

        sso_context = browser.new_context()
        signed_in = sso_context.request.get(
            f"{PUZZARY_BASE}/auth/oidc/{provider_id}/start"
        )
        assert signed_in.ok and 'data-signed-in="true"' in signed_in.text(), (
            signed_in.status,
            signed_in.url,
            signed_in.text()[:500],
        )

        with SessionLocal() as db:
            before_users = len(list(db.scalars(select(User))))
        IDP_USER.update({
            "sub": "same-email-different-subject",
            "name": "Not Automatically Merged",
            "preferred_username": "not-merged",
            "email": "admin@example.test",
            "email_verified": True,
        })
        unlinked_context = browser.new_context()
        unlinked = unlinked_context.request.get(
            f"{PUZZARY_BASE}/auth/oidc/{provider_id}/start"
        )
        assert unlinked.ok and "not linked to a Puzzary account" in unlinked.text()
        with SessionLocal() as db:
            assert len(list(db.scalars(select(User)))) == before_users

        api_headers = {"Authorization": "Bearer oidc-admin-api-token"}
        listing = anonymous.get(
            PUZZARY_BASE + "/api/admin/oidc/providers", headers=api_headers
        )
        assert listing.ok, (listing.status, listing.text()[:500])
        listing_text = listing.text()
        listing_payload = listing.json()
        assert CLIENT_SECRET not in listing_text
        assert "client_secret" not in listing_payload["providers"][0]
        assert listing_payload["providers"][0]["has_client_secret"] is True
        assert listing_payload["local_accounts_default"] is True

        changed = anonymous.patch(
            f"{PUZZARY_BASE}/api/admin/oidc/providers/{provider_id}",
            headers=api_headers,
            data={"provisioning_mode": "jit"},
        )
        assert changed.ok and changed.json()["provisioning_mode"] == "jit"

        IDP_USER.update({
            "sub": "jit-player-subject",
            "name": "JIT Player",
            "preferred_username": "jit-player",
            "email": "jit@example.test",
            "email_verified": True,
        })
        jit_context = browser.new_context()
        jit_login = jit_context.request.get(
            f"{PUZZARY_BASE}/auth/oidc/{provider_id}/start"
        )
        assert jit_login.ok and 'data-signed-in="true"' in jit_login.text()
        with SessionLocal() as db:
            jit_user = db.scalar(select(User).where(User.display_name == "JIT Player"))
            assert jit_user and not jit_user.is_admin and not jit_user.password_hash
            assert [role.name for role in jit_user.roles] == ["Player"]

        oidc_only = anonymous.put(
            PUZZARY_BASE + "/api/admin/oidc/settings",
            headers=api_headers,
            data={"login_mode": "oidc"},
        )
        assert oidc_only.ok
        hidden_local = browser.new_context().request.get(PUZZARY_BASE + "/login").text()
        assert "Administrator recovery login" in hidden_local and 'data-local-login' not in hidden_local
        recovery = browser.new_context().request.get(PUZZARY_BASE + "/login?local=1").text()
        assert 'data-local-login' in recovery
        denied_player = browser.new_context()
        response = login(denied_player, "OIDC Player", "Player-test-123")
        assert response.status == 303 and "reserved" in response.headers["location"]
        recovery_admin = browser.new_context()
        assert login(recovery_admin, "OIDC Admin", "Admin-test-123").status == 303

        page = admin_context.new_page()
        page.set_viewport_size({"width": 390, "height": 900})
        assert page.goto(PUZZARY_BASE + "/admin/settings").ok
        assert page.locator("text=OpenID Connect Providers").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.close()

        disabled = anonymous.patch(
            f"{PUZZARY_BASE}/api/admin/oidc/providers/{provider_id}",
            headers=api_headers,
            data={"enabled": False},
        )
        assert disabled.ok and disabled.json()["enabled"] is False
        settings = anonymous.get(
            PUZZARY_BASE + "/api/admin/oidc/providers", headers=api_headers
        ).json()
        assert settings["login_mode"] == "local", settings
        restored_local = browser.new_context().request.get(PUZZARY_BASE + "/login").text()
        assert 'data-local-login' in restored_local and "Sign in with" not in restored_local

        browser.close()
        anonymous.dispose()

    print(
        "OIDC default-local mode, encrypted secrets, discovery, PKCE S256, signed ID tokens, "
        "explicit linking, no email auto-merge, player-only JIT, redacted API, admin recovery, "
        "safe local fallback, and responsive settings UI passed."
    )
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    idp.shutdown()
    idp.server_close()
    shutil.rmtree(QA_ROOT, ignore_errors=True)
