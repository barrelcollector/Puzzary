"""Isolated publication, Web Push, privacy, lifecycle, and UI checks for #64."""
from __future__ import annotations

import json
import os
import base64
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


QA_ROOT = Path(tempfile.gettempdir()) / "puzzary-notifications-qa"
if QA_ROOT.exists():
    shutil.rmtree(QA_ROOT)
QA_ROOT.mkdir(parents=True)
QA_DB = QA_ROOT / "notifications.sqlite"
BASE = "http://127.0.0.1:8814"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{QA_DB.as_posix()}",
    "SESSION_SECRET": "isolated-notification-secret",
    "UPLOAD_DIR": str(QA_ROOT / "uploads"),
    "APP_BASE_URL": BASE,
    "PUZZARY_PUSH_LOOP_SECONDS": "3600",
})

from playwright.sync_api import sync_playwright
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPusher, WebPushException
from sqlalchemy import func, inspect, select

from app.database import Base, SessionLocal, engine
from app.main import hash_password
from app.models import (
    AppSetting,
    PushDelivery,
    PushSubscription,
    Puzzle,
    PuzzleNotification,
    PuzzleNotificationPreference,
    PuzzlePublicationEvent,
    PuzzleTag,
    User,
)
from app.notifications import (
    VAPID_SETTING,
    apply_due_publication_notifications,
    deliver_pending_pushes,
    ensure_vapid_keys,
    queue_puzzle_publication,
    validate_subscription_endpoint,
    vapid_public_key,
    vapid_instance,
)
from app.permissions import initialize_permissions
from app.puzzles import get_puzzle_type


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
tables = set(inspect(engine).get_table_names())
assert {
    "puzzle_notification_preferences", "push_subscriptions", "puzzle_publication_events",
    "puzzle_notifications", "push_deliveries",
} <= tables

with SessionLocal() as db:
    admin = User(
        display_name="Notify Admin", is_admin=True, is_active=True, session_version=1,
        password_hash=hash_password("Notify-test-123"),
    )
    player = User(
        display_name="Notify Player", is_active=True, session_version=1,
        password_hash=hash_password("Notify-test-123"),
    )
    filtered = User(
        display_name="Filtered Player", is_active=True, session_version=1,
        password_hash=hash_password("Notify-test-123"),
    )
    db.add_all([admin, player, filtered])
    db.commit()
    initialize_permissions(db)
    db.add(PuzzleNotificationPreference(user_id=player.id, enabled=True, filters={}))
    db.add(PuzzleNotificationPreference(user_id=filtered.id, enabled=True, filters={"themes": ["Never Match"]}))
    db.add(PuzzleTag(name="Notify tag", tag_type="General", slug="notify-tag"))
    db.commit()
    admin_id, player_id, filtered_id = admin.id, player.id, filtered.id
    first_private_key = ensure_vapid_keys(db)
    first_public_key = vapid_public_key(db)
    assert len(first_public_key) == 87 and first_private_key.startswith("-----BEGIN PRIVATE KEY-----")
    assert ensure_vapid_keys(db) == first_private_key and vapid_public_key(db) == first_public_key
    assert db.get(AppSetting, VAPID_SETTING).value == first_private_key
    signed = vapid_instance(db).sign({
        "sub": "mailto:notifications@puzzary.local",
        "aud": "https://push.example.com",
        "exp": int(time.time()) + 300,
    })
    assert signed["Authorization"].startswith("vapid t=") and ",k=" in signed["Authorization"]

client_public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
)
b64url = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
encrypted = WebPusher({
    "endpoint": "https://push.example.com/encryption-check",
    "keys": {"p256dh": b64url(client_public), "auth": b64url(os.urandom(16))},
}).encode(b'{"title":"Encryption check"}', content_encoding="aes128gcm")
assert isinstance(encrypted["body"], bytes) and len(encrypted["body"]) > 30

try:
    validate_subscription_endpoint("http://push.example.com/subscription")
    raise AssertionError("HTTP push endpoint was accepted")
except Exception as error:
    assert getattr(error, "status_code", None) == 422
try:
    validate_subscription_endpoint("https://127.0.0.1/private")
    raise AssertionError("Loopback push endpoint was accepted")
except Exception as error:
    assert getattr(error, "status_code", None) == 422


def wait_ready(request) -> None:
    for _ in range(120):
        try:
            if request.get(BASE + "/health").ok:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Notification QA server did not start")


server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8814"],
    env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        admin_context = browser.new_context()
        player_context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        wait_ready(admin_context.request)
        for context, name in ((admin_context, "Notify Admin"), (player_context, "Notify Player")):
            response = context.request.post(BASE + "/login", form={"username": name, "password": "Notify-test-123"}, max_redirects=0)
            assert response.status == 303

        page = player_context.new_page()
        page.add_init_script("""
          window.__permissionRequests = 0;
          Object.defineProperty(window, 'Notification', {configurable: true, value: {
            permission: 'default', requestPermission: async () => { window.__permissionRequests += 1; return 'denied'; }
          }});
          Object.defineProperty(window, 'PushManager', {configurable: true, value: function PushManager(){}});
        """)
        page.goto(BASE + "/account/notifications")
        assert page.locator("h1").inner_text() == "Notifications"
        assert page.evaluate("window.__permissionRequests") == 0
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("[data-enable-notifications]").click()
        page.wait_for_function("window.__permissionRequests === 1")
        assert "denied" in page.locator("[data-notification-status]").inner_text().lower()
        worker = player_context.request.get(BASE + "/service-worker.js")
        assert worker.ok and "application/javascript" in worker.headers["content-type"]
        assert worker.headers["service-worker-allowed"] == "/"
        assert 'addEventListener("push"' in worker.text() and 'addEventListener("notificationclick"' in worker.text()

        for index in (1, 2):
            subscribed = player_context.request.post(BASE + "/api/notifications/subscriptions", data={
                "endpoint": f"https://push.example.com/device-{index}",
                "keys": {"p256dh": "A" * 65, "auth": "B" * 22},
                "device_name": f"Browser {index}",
            })
            assert subscribed.status == 201, subscribed.text()
        devices_page = player_context.request.get(BASE + "/account/notifications").text()
        assert "Browser 1" in devices_page and "Browser 2" in devices_page
        assert "push.example.com" not in devices_page and ("A" * 30) not in devices_page

        generated = admin_context.request.post(BASE + "/api/puzzles/generate", data={
            "puzzle_type": "takuzu", "theme": "Generated Theme", "difficulty": "easy", "seed": "notify-generated",
        })
        assert generated.ok, generated.text()
        generated_id = generated.json()["id"]
        exported = admin_context.request.get(BASE + f"/api/puzzles/{generated_id}/export")
        assert exported.ok
        imported = admin_context.request.post(BASE + "/api/puzzles/import", data={
            "source_text": json.dumps(exported.json()), "dry_run": False, "duplicate_policy": "copy",
        })
        assert imported.ok and imported.json()["imported"] == 1, imported.text()

        plugin = get_puzzle_type("takuzu")
        manual_data = plugin.manual_builder({
            "solution_grid": [[0, 0, 1, 1], [0, 1, 0, 1], [1, 0, 1, 0], [1, 1, 0, 0]],
            "given_count": 0, "seed": "notify-manual",
        })
        manual = admin_context.request.post(BASE + "/api/puzzles/manual", data={
            "puzzle_type": "takuzu", "title": "Manual & Safe", "theme": "Manual Theme", "difficulty": "easy",
            "prompt": "MANUAL SPOILER PROMPT", "print_payload": manual_data.print_payload,
            "play_payload": manual_data.play_payload, "solution_payload": {"kind": "takuzu", **manual_data.solution_payload},
            "metadata": {}, "hints": ["SECRET MANUAL HINT"], "draft": False,
        })
        assert manual.ok, manual.text()

        release = datetime.now(timezone.utc) + timedelta(days=1)
        scheduled = admin_context.request.post(BASE + "/api/puzzles/generate", data={
            "puzzle_type": "takuzu", "theme": "Scheduled Theme", "difficulty": "easy",
            "seed": "notify-scheduled", "publish_at": release.isoformat(),
        })
        assert scheduled.ok and scheduled.json()["publication_state"] == "scheduled"
        scheduled_id = scheduled.json()["id"]
        with SessionLocal() as db:
            assert db.get(PuzzlePublicationEvent, scheduled_id) is None
            db.get(Puzzle, scheduled_id).publish_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            assert apply_due_publication_notifications(db) == 1
            assert apply_due_publication_notifications(db) == 0

        for title, extra in (
            ("UNLISTED PRIVATE", {"visibility": "unlisted"}),
            ("PASSWORD PRIVATE", {"access_password": "Private-test-123"}),
        ):
            private = admin_context.request.post(BASE + "/api/puzzles/generate", data={
                "puzzle_type": "takuzu", "theme": title, "difficulty": "easy", "seed": title, **extra,
            })
            assert private.ok

        with SessionLocal() as db:
            notifications = db.scalars(select(PuzzleNotification).where(PuzzleNotification.user_id == player_id)).all()
            titles = {item.title for item in notifications}
            assert generated.json()["title"] in titles
            assert "Manual & Safe" in titles
            assert any(item.puzzle_id == scheduled_id for item in notifications)
            assert len(notifications) == 4
            assert db.scalar(select(func.count()).select_from(PuzzleNotification).where(PuzzleNotification.user_id == filtered_id)) == 0
            assert not any("PRIVATE" in item.title for item in notifications)
            assert queue_puzzle_publication(db, db.get(Puzzle, generated_id)) == 0
            db.commit()
            expected_notifications = len(notifications)
            assert db.scalar(select(func.count()).select_from(PushDelivery)) == expected_notifications * 2

            sent: list[dict] = []
            def fake_sender(**kwargs):
                sent.append(kwargs)
                return SimpleNamespace(status_code=201)
            public_resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))]
            result = deliver_pending_pushes(db, sender=fake_sender, resolver=public_resolver, limit=100)
            assert result["delivered"] == expected_notifications * 2
            assert len(sent) == expected_notifications * 2
            payloads = [json.loads(call["data"]) for call in sent]
            assert all(item["url"].startswith(BASE + "/puzzles/") for item in payloads)
            serialized = json.dumps(payloads)
            assert "MANUAL SPOILER" not in serialized and "SECRET MANUAL HINT" not in serialized
            assert all(call["ttl"] == 3600 and call["timeout"] == 10 for call in sent)

            suppressed_puzzle = Puzzle(
                id="notify-suppressed", puzzle_type="takuzu", title="Suppressed after queue", theme="Access",
                difficulty="easy", prompt="", print_payload=manual_data.print_payload, play_payload=manual_data.play_payload,
                solution_payload=manual_data.solution_payload, puzzle_metadata={}, source="test", created_by_id=admin_id,
            )
            db.add(suppressed_puzzle); db.flush()
            assert queue_puzzle_publication(db, suppressed_puzzle) == 1
            db.get(User, player_id).is_active = False
            db.commit()
            suppressed = deliver_pending_pushes(db, sender=fake_sender, resolver=public_resolver, limit=100)
            assert suppressed["suppressed"] == 2
            db.get(User, player_id).is_active = True
            db.commit()

            expired_subscription = PushSubscription(
                id="expired-push-subscription", user_id=player_id,
                endpoint_hash="f" * 64, endpoint="https://push.example.com/expired",
                p256dh="C" * 65, auth="D" * 22, device_name="Expired browser",
            )
            expired_puzzle = Puzzle(
                id="notify-expired", puzzle_type="takuzu", title="Expired endpoint check", theme="Delivery",
                difficulty="easy", prompt="", print_payload=manual_data.print_payload, play_payload=manual_data.play_payload,
                solution_payload=manual_data.solution_payload, puzzle_metadata={}, source="test", created_by_id=admin_id,
            )
            db.add_all([expired_subscription, expired_puzzle]); db.flush()
            assert queue_puzzle_publication(db, expired_puzzle) == 1
            db.commit()
            def gone_sender(**kwargs):
                if kwargs["subscription_info"]["endpoint"].endswith("/expired"):
                    raise WebPushException("gone", response=SimpleNamespace(status_code=410, headers={}, text="gone"))
                return SimpleNamespace(status_code=201)
            expired = deliver_pending_pushes(db, sender=gone_sender, resolver=public_resolver, limit=100)
            assert expired["revoked"] == 1 and expired["delivered"] == 2
            assert db.get(PushSubscription, expired_subscription.id).revoked_at is not None

        badge_page = player_context.new_page()
        badge_page.goto(BASE + "/library")
        badge_page.locator(".notification-badge").wait_for()
        assert int(badge_page.locator(".notification-badge").inner_text()) >= 1
        assert player_context.request.post(BASE + "/account/notifications/read", max_redirects=0).status == 303
        assert player_context.request.get(BASE + "/api/notifications").json()["unread"] == 0

        with SessionLocal() as db:
            active_subscription = db.scalar(select(PushSubscription).where(
                PushSubscription.user_id == player_id, PushSubscription.revoked_at.is_(None)))
            subscription_id = active_subscription.id
        assert player_context.request.post(
            BASE + f"/account/notifications/subscriptions/{subscription_id}/revoke", max_redirects=0,
        ).status == 303
        with SessionLocal() as db:
            assert db.get(PushSubscription, subscription_id).revoked_at is not None

        browser.close()
finally:
    server.terminate()
    server.wait(timeout=15)

print("Notification opt-in UI, explicit permission timing, VAPID persistence, device management, generated/manual/imported/scheduled publication events, filters, privacy, deduplication, delivery, access re-checks, fallback badge, and revocation passed.")
