"""Isolated HTTP, TLS configuration, proxy, cookie, redirect, and URL checks for #63."""
from __future__ import annotations

import os
import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-transport-security-qa"
QA_DB = QA_ROOT / "transport.sqlite"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)

os.environ["DATABASE_URL"] = f"sqlite:///{QA_DB.as_posix()}"
os.environ["SESSION_SECRET"] = "isolated-transport-security-secret"
os.environ["UPLOAD_DIR"] = str(QA_ROOT / "uploads")
os.environ["APP_BASE_URL"] = "http://localhost:8809"
for name in (
    "PUZZARY_TLS_CERT_FILE", "PUZZARY_TLS_KEY_FILE", "PUZZARY_TLS_KEY_PASSWORD",
    "PUZZARY_TRUSTED_PROXIES", "PUZZARY_HTTPS_REDIRECT", "PUZZARY_SECURE_COOKIES",
    "PUZZARY_HOST", "PUZZARY_PORT",
):
    os.environ.pop(name, None)

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.main import hash_password, hash_secret
from app.models import Puzzle, User
from app.permissions import initialize_permissions
from app.server import main as server_main
from app.transport import TransportConfigurationError, load_transport_config
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def rejected(env: dict[str, str], text: str, *, validate_files: bool = False) -> None:
    try:
        load_transport_config(env, validate_files=validate_files)
    except TransportConfigurationError as error:
        assert text.lower() in str(error).lower(), str(error)
    else:
        raise AssertionError(f"Expected transport configuration failure containing {text!r}")


default = load_transport_config({"APP_BASE_URL": "http://localhost:8790"})
assert not default.native_tls and not default.secure_cookies and not default.https_redirect
assert not default.trusted_proxies and default.warnings
https = load_transport_config({"APP_BASE_URL": "https://puzzary.example.test"})
assert https.secure_cookies and not https.native_tls
proxy = load_transport_config({
    "APP_BASE_URL": "https://puzzary.example.test",
    "PUZZARY_TRUSTED_PROXIES": "127.0.0.1,172.20.0.0/16",
    "PUZZARY_HTTPS_REDIRECT": "true",
})
assert proxy.forwarded_allow_ips == "127.0.0.1,172.20.0.0/16" and proxy.https_redirect
assert load_transport_config({
    "APP_BASE_URL": "https://puzzary.example.test",
    "PUZZARY_TRUSTED_PROXIES": "*",
}).warnings

rejected({"APP_BASE_URL": "https://puzzary.example.test", "PUZZARY_TLS_CERT_FILE": "cert.pem"}, "configured together")
rejected({"APP_BASE_URL": "http://puzzary.example.test", "PUZZARY_TLS_CERT_FILE": "cert.pem", "PUZZARY_TLS_KEY_FILE": "key.pem"}, "https://")
rejected({"APP_BASE_URL": "https://puzzary.example.test", "PUZZARY_SECURE_COOKIES": "false"}, "cannot be disabled")
rejected({"APP_BASE_URL": "https://puzzary.example.test", "PUZZARY_HTTPS_REDIRECT": "true"}, "trusted_proxies")
rejected({"APP_BASE_URL": "https://puzzary.example.test/path"}, "absolute HTTP or HTTPS origin")
rejected({"APP_BASE_URL": "http://localhost:8790", "PUZZARY_TRUSTED_PROXIES": "not-an-address"}, "invalid IP")
rejected({"APP_BASE_URL": "http://localhost:8790", "PUZZARY_PORT": "70000"}, "1 to 65535")
rejected({
    "APP_BASE_URL": "https://puzzary.example.test",
    "PUZZARY_TLS_CERT_FILE": str(QA_ROOT / "missing-cert.pem"),
    "PUZZARY_TLS_KEY_FILE": str(QA_ROOT / "missing-key.pem"),
}, "readable file", validate_files=True)
bad_cert = QA_ROOT / "bad-cert.pem"
bad_key = QA_ROOT / "bad-key.pem"
bad_cert.write_text("not a certificate", encoding="utf-8")
bad_key.write_text("not a private key", encoding="utf-8")
rejected({
    "APP_BASE_URL": "https://puzzary.example.test",
    "PUZZARY_TLS_CERT_FILE": str(bad_cert),
    "PUZZARY_TLS_KEY_FILE": str(bad_key),
}, "could not load", validate_files=True)

with patch.dict(os.environ, {
    "APP_BASE_URL": "https://puzzary.example.test",
    "PUZZARY_TRUSTED_PROXIES": "127.0.0.1",
    "PUZZARY_HTTPS_REDIRECT": "true",
    "PUZZARY_PORT": "8810",
}, clear=True), patch("app.server.uvicorn.run") as run:
    server_main()
    options = run.call_args.kwargs
    assert options["proxy_headers"] is True
    assert options["forwarded_allow_ips"] == "127.0.0.1"
    assert options["ssl_certfile"] is None and options["port"] == 8810


async def forwarded_websocket_scheme(client_host: str) -> str:
    captured: dict[str, str] = {}

    async def target(scope, receive, send):
        captured["scheme"] = scope["scheme"]

    async def receive():
        return {"type": "websocket.disconnect"}

    async def send(message):
        return None

    middleware = ProxyHeadersMiddleware(target, trusted_hosts="127.0.0.1")
    await middleware({
        "type": "websocket", "scheme": "ws", "client": (client_host, 12345),
        "headers": [(b"x-forwarded-proto", b"https")],
    }, receive, send)
    return captured["scheme"]


assert asyncio.run(forwarded_websocket_scheme("127.0.0.1")) == "wss"
assert asyncio.run(forwarded_websocket_scheme("203.0.113.20")) == "ws"

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    admin = User(
        display_name="Transport Admin", is_admin=True, is_active=True,
        password_hash=hash_password("Transport-test-123"),
        api_token_hash=hash_secret("transport-api-token"), api_token_prefix="transport",
    )
    db.add(admin)
    db.commit()
    initialize_permissions(db)
    db.add(Puzzle(
        id="transport-puzzle", puzzle_type="unknown_transport", title="Transport fixture",
        theme="", difficulty="easy", prompt="", print_payload={}, play_payload={},
        solution_payload={}, puzzle_metadata={}, source="test", created_by_id=admin.id,
    ))
    db.commit()


def start_server(port: int, **overrides: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{QA_DB.as_posix()}",
        "SESSION_SECRET": "isolated-transport-security-secret",
        "UPLOAD_DIR": str(QA_ROOT / "uploads"),
        "PUZZARY_HOST": "127.0.0.1",
        "PUZZARY_PORT": str(port),
    })
    for name in (
        "PUZZARY_TLS_CERT_FILE", "PUZZARY_TLS_KEY_FILE", "PUZZARY_TLS_KEY_PASSWORD",
        "PUZZARY_TRUSTED_PROXIES", "PUZZARY_HTTPS_REDIRECT", "PUZZARY_SECURE_COOKIES",
    ):
        env.pop(name, None)
    env.update(overrides)
    return subprocess.Popen(
        [sys.executable, "-m", "app.server"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop_server(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def wait_ready(context, url: str, headers: dict[str, str] | None = None) -> None:
    for _ in range(100):
        try:
            response = context.get(url + "/health", headers=headers, max_redirects=0)
            if response.status in {200, 307}:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"Transport QA server did not start at {url}")


try:
    with sync_playwright() as playwright:
        request = playwright.request.new_context()

        http_server = start_server(8809, APP_BASE_URL="http://localhost:8809")
        try:
            http_base = "http://127.0.0.1:8809"
            wait_ready(request, http_base)
            assert request.get(http_base + "/health").status == 200
            login = request.post(http_base + "/login", form={
                "username": "Transport Admin", "password": "Transport-test-123",
            }, max_redirects=0)
            assert login.status == 303
            cookie = login.headers.get("set-cookie", "")
            assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
            assert "; secure" not in cookie.lower()
            settings = request.get(http_base + "/admin/settings")
            assert settings.ok
            assert "Public base URL uses HTTP" in settings.text()
            assert "Secure session cookies" in settings.text()
        finally:
            stop_server(http_server)

        proxy_server = start_server(
            8810,
            APP_BASE_URL="https://puzzary.example.test",
            PUZZARY_TRUSTED_PROXIES="127.0.0.1",
            PUZZARY_HTTPS_REDIRECT="true",
        )
        try:
            proxy_base = "http://127.0.0.1:8810"
            trusted_headers = {"X-Forwarded-Proto": "https", "Host": "puzzary.example.test"}
            wait_ready(request, proxy_base, trusted_headers)
            assert request.get(proxy_base + "/health", headers=trusted_headers).status == 200
            redirect = request.get(proxy_base + "/health", max_redirects=0)
            assert redirect.status == 307 and redirect.headers["location"].startswith("https://")
            login = request.post(proxy_base + "/login", headers=trusted_headers, form={
                "username": "Transport Admin", "password": "Transport-test-123",
            }, max_redirects=0)
            assert login.status == 303 and "; secure" in login.headers.get("set-cookie", "").lower()
            api = request.get(proxy_base + "/api/puzzles/transport-puzzle", headers={
                **trusted_headers, "Authorization": "Bearer transport-api-token",
            })
            assert api.ok and api.json()["url"].startswith("https://puzzary.example.test/")
            client_js = request.get(proxy_base + "/static/group-solve.js", headers=trusted_headers).text()
            assert "location.protocol === 'https:' ? 'wss:' : 'ws:'" in client_js
        finally:
            stop_server(proxy_server)

        untrusted_server = start_server(
            8811,
            APP_BASE_URL="https://puzzary.example.test",
            PUZZARY_TRUSTED_PROXIES="10.20.30.40",
            PUZZARY_HTTPS_REDIRECT="true",
        )
        try:
            untrusted_base = "http://127.0.0.1:8811"
            wait_ready(request, untrusted_base, {"X-Forwarded-Proto": "https"})
            forged = request.get(
                untrusted_base + "/health",
                headers={"X-Forwarded-Proto": "https"},
                max_redirects=0,
            )
            assert forged.status == 307
        finally:
            stop_server(untrusted_server)
        request.dispose()
    print("HTTP compatibility, validation, trusted proxy redirects, secure cookies, HTTPS URLs, and WSS selection passed.")
finally:
    shutil.rmtree(QA_ROOT, ignore_errors=True)
