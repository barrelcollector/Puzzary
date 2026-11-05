"""Permission-aware in-app and Web Push notifications for puzzle publication."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import uuid
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import SessionLocal, get_db
from app.feeds import normalized_filters, visible_filter_options
from app.models import (
    AppSetting,
    PushDelivery,
    PushSubscription,
    Puzzle,
    PuzzleNotification,
    PuzzleNotificationPreference,
    PuzzlePublicationEvent,
    User,
)
from app.permissions import can_discover, puzzle_is_published


router = APIRouter()
logger = logging.getLogger("puzzary.notifications")
VAPID_SETTING = "web_push_vapid_private_key"
VAPID_SUBJECT = os.getenv("PUZZARY_VAPID_SUBJECT", "mailto:notifications@puzzary.local").strip()
PUSH_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,512}$")


def positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


PUSH_LOOP_SECONDS = max(2, positive_env("PUZZARY_PUSH_LOOP_SECONDS", 10))


def core():
    from app import main
    return main


def utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ensure_vapid_keys(db: Session) -> str:
    """Return the persisted VAPID private PEM, generating it once if needed."""
    configured = os.getenv("PUZZARY_VAPID_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    setting = db.get(AppSetting, VAPID_SETTING)
    private_pem = configured or (setting.value.strip() if setting is not None else "")
    if not private_pem:
        vapid = Vapid02()
        vapid.generate_keys()
        private_pem = vapid.private_pem().decode("ascii").strip()
        db.add(AppSetting(key=VAPID_SETTING, value=private_pem))
        db.commit()
    try:
        Vapid02.from_pem(private_pem.encode("ascii"))
    except Exception as error:
        raise RuntimeError("The configured Web Push VAPID private key is invalid") from error
    return private_pem


def vapid_instance(db: Session) -> Vapid02:
    return Vapid02.from_pem(ensure_vapid_keys(db).encode("ascii"))


def vapid_public_key(db: Session) -> str:
    public_bytes = vapid_instance(db).public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")


def clean_filters(filters: dict | None) -> dict:
    filters = filters or {}
    return {
        "puzzle_types": [str(value) for value in filters.get("puzzle_types", [])],
        "themes": [str(value) for value in filters.get("themes", [])],
        "tag_ids": [int(value) for value in filters.get("tag_ids", [])],
        "creator_ids": [int(value) for value in filters.get("creator_ids", [])],
    }


def filter_summary(filters: dict, db: Session) -> str:
    from app.feeds import feed_filter_summary
    summary = feed_filter_summary({**clean_filters(filters), "limit": 25}, db)
    return summary.removesuffix(" · latest 25")


def matches_filters(puzzle: Puzzle, filters: dict) -> bool:
    values = clean_filters(filters)
    if values["puzzle_types"] and puzzle.puzzle_type not in values["puzzle_types"]:
        return False
    if values["themes"] and puzzle.theme not in values["themes"]:
        return False
    if values["tag_ids"] and not ({tag.id for tag in puzzle.tags} & set(values["tag_ids"])):
        return False
    if values["creator_ids"] and puzzle.created_by_id not in values["creator_ids"]:
        return False
    return True


def user_can_receive(db: Session, user: User, puzzle: Puzzle, filters: dict) -> bool:
    if not user.is_active or not user.can("library.view"):
        return False
    if puzzle.archived_at is not None or puzzle.access_password_hash or not can_discover(user, puzzle):
        return False
    if not matches_filters(puzzle, filters):
        return False
    try:
        core().require_series_access(db, puzzle, user)
    except HTTPException as error:
        if error.status_code == 423:
            return False
        raise
    return True


def notification_body(puzzle: Puzzle) -> str:
    plugin = core().get_puzzle_type(puzzle.puzzle_type)
    label = plugin.label if plugin is not None else puzzle.puzzle_type.replace("_", " ").title()
    return f"{label} · {puzzle.theme}"[:300] if puzzle.theme else f"New {label} puzzle"[:300]


def queue_puzzle_publication(db: Session, puzzle: Puzzle, published_at: datetime | None = None) -> int:
    """Create exactly one publication event and eligible per-user notifications."""
    if db.get(PuzzlePublicationEvent, puzzle.id) is not None:
        return 0
    if not puzzle_is_published(puzzle) or puzzle.archived_at is not None:
        return 0
    event_time = utc(published_at or puzzle.publish_at)
    db.add(PuzzlePublicationEvent(puzzle_id=puzzle.id, published_at=event_time))
    preferences = db.scalars(
        select(PuzzleNotificationPreference)
        .where(PuzzleNotificationPreference.enabled.is_(True))
        .options(selectinload(PuzzleNotificationPreference.user))
    ).all()
    created = 0
    for preference in preferences:
        user = preference.user
        if user is None or not user_can_receive(db, user, puzzle, preference.filters or {}):
            continue
        notification = PuzzleNotification(
            id=uuid.uuid4().hex[:26],
            user_id=user.id,
            puzzle_id=puzzle.id,
            event_type="published",
            title=puzzle.title[:160],
            body=notification_body(puzzle),
        )
        db.add(notification)
        db.flush()
        subscriptions = db.scalars(select(PushSubscription).where(
            PushSubscription.user_id == user.id,
            PushSubscription.revoked_at.is_(None),
        )).all()
        for subscription in subscriptions:
            db.add(PushDelivery(notification_id=notification.id, subscription_id=subscription.id))
        created += 1
    return created


def apply_due_publication_notifications(db: Session) -> int:
    now = datetime.now(timezone.utc)
    due = db.scalars(
        select(Puzzle)
        .where(
            Puzzle.publish_at.is_not(None),
            Puzzle.publish_at <= now,
            Puzzle.archived_at.is_(None),
            ~Puzzle.id.in_(select(PuzzlePublicationEvent.puzzle_id)),
        )
        .options(selectinload(Puzzle.tags), selectinload(Puzzle.creator))
    ).all()
    total = sum(queue_puzzle_publication(db, puzzle, puzzle.publish_at) for puzzle in due)
    if due:
        db.commit()
    return total


def validate_subscription_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if len(endpoint) > 4000:
        raise HTTPException(422, "Push subscription endpoint is too long")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, "Push subscription endpoint must be a public HTTPS URL")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise HTTPException(422, "Push subscription endpoint must use a public host")
    if parsed.hostname.lower() == "localhost":
        raise HTTPException(422, "Push subscription endpoint must use a public host")
    return endpoint


def endpoint_resolves_public(endpoint: str, resolver=socket.getaddrinfo) -> bool:
    parsed = urlparse(endpoint)
    try:
        addresses = resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        resolved = {ipaddress.ip_address(item[4][0]) for item in addresses}
    except (OSError, ValueError, TypeError):
        return False
    return bool(resolved) and all(address.is_global for address in resolved)


def safe_error(error: Exception) -> str:
    status = getattr(error, "status_code", None)
    return f"{type(error).__name__}{f' ({status})' if status else ''}"[:300]


def delivery_payload(notification: PuzzleNotification, db: Session) -> str:
    return json.dumps({
        "title": notification.title,
        "body": notification.body,
        "url": core().puzzle_url(notification.puzzle_id, db),
        "tag": f"puzzary-puzzle-{notification.puzzle_id}",
        "notification_id": notification.id,
    }, separators=(",", ":"))


def deliver_pending_pushes(
    db: Session,
    *,
    sender=webpush,
    resolver=socket.getaddrinfo,
    limit: int = 25,
) -> dict:
    now = datetime.now(timezone.utc)
    deliveries = db.scalars(
        select(PushDelivery)
        .where(
            PushDelivery.status.in_(["pending", "retry"]),
            PushDelivery.attempts < 3,
            or_(PushDelivery.next_attempt_at.is_(None), PushDelivery.next_attempt_at <= now),
        )
        .order_by(PushDelivery.id)
        .limit(limit)
    ).all()
    result = {"delivered": 0, "retried": 0, "revoked": 0, "suppressed": 0}
    vapid = vapid_instance(db) if deliveries else None
    for delivery in deliveries:
        notification = delivery.notification
        subscription = delivery.subscription
        preference = db.get(PuzzleNotificationPreference, notification.user_id)
        puzzle = notification.puzzle
        user = notification.user
        if (
            subscription is None
            or subscription.revoked_at is not None
            or preference is None
            or not preference.enabled
            or puzzle is None
            or user is None
            or not user_can_receive(db, user, puzzle, preference.filters or {})
        ):
            delivery.status = "suppressed"
            result["suppressed"] += 1
            continue
        if not endpoint_resolves_public(subscription.endpoint, resolver):
            delivery.status = "failed"
            delivery.last_error = "Push endpoint did not resolve to a public address"
            subscription.last_error = delivery.last_error
            continue
        delivery.attempts += 1
        try:
            sender(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=delivery_payload(notification, db).encode("utf-8"),
                vapid_private_key=vapid,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=3600,
                timeout=10,
            )
        except WebPushException as error:
            status_code = error.status_code
            message = safe_error(error)
            delivery.last_error = message
            subscription.last_error = message
            if status_code in {404, 410}:
                subscription.revoked_at = now
                delivery.status = "expired"
                result["revoked"] += 1
            elif status_code in {429, 500, 502, 503, 504} and delivery.attempts < 3:
                delivery.status = "retry"
                delivery.next_attempt_at = now + timedelta(minutes=5 * delivery.attempts)
                result["retried"] += 1
            else:
                delivery.status = "failed"
        except Exception as error:
            message = safe_error(error)
            delivery.last_error = message
            subscription.last_error = message
            if delivery.attempts < 3:
                delivery.status = "retry"
                delivery.next_attempt_at = now + timedelta(minutes=5 * delivery.attempts)
                result["retried"] += 1
            else:
                delivery.status = "failed"
        else:
            delivery.status = "delivered"
            delivery.delivered_at = now
            delivery.last_error = ""
            subscription.last_success_at = now
            subscription.last_error = ""
            result["delivered"] += 1
    if deliveries:
        db.commit()
    return result


def process_push_work() -> None:
    with SessionLocal() as db:
        apply_due_publication_notifications(db)
        deliver_pending_pushes(db)


async def notification_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(process_push_work)
        except Exception:
            logger.exception("Browser-notification background processing failed")
        await asyncio.sleep(PUSH_LOOP_SECONDS)


def preference_for(db: Session, user: User) -> PuzzleNotificationPreference:
    preference = db.get(PuzzleNotificationPreference, user.id)
    if preference is None:
        preference = PuzzleNotificationPreference(user_id=user.id, enabled=False, filters={})
    return preference


def account_context(request: Request, db: Session, user: User) -> dict:
    preference = preference_for(db, user)
    puzzle_types, themes, tags, creators = visible_filter_options(db, user)
    subscriptions = db.scalars(
        select(PushSubscription)
        .where(PushSubscription.user_id == user.id)
        .order_by(PushSubscription.created_at.desc())
    ).all()
    notifications = db.scalars(
        select(PuzzleNotification)
        .where(PuzzleNotification.user_id == user.id)
        .order_by(PuzzleNotification.created_at.desc())
        .limit(50)
    ).all()
    return {
        "request": request,
        "current_user": user,
        "preference": preference,
        "filters": clean_filters(preference.filters),
        "filter_summary": filter_summary(preference.filters or {}, db),
        "puzzle_types": puzzle_types,
        "themes": themes,
        "tags": tags,
        "creators": creators,
        "subscriptions": subscriptions,
        "notifications": notifications,
        "vapid_public_key": vapid_public_key(db),
        "https_warning": not core().app_base_url(db).lower().startswith("https://"),
        "message": request.query_params.get("message", ""),
    }


@router.get("/account/notifications", response_class=HTMLResponse)
def account_notifications(request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return core().templates.TemplateResponse("account_notifications.html", account_context(request, db, user))


@router.post("/account/notifications/preferences")
def save_notification_preferences(
    request: Request,
    enabled: bool = Form(False),
    puzzle_types: list[str] = Form(default=[]),
    themes: list[str] = Form(default=[]),
    tag_ids: list[int] = Form(default=[]),
    creator_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    preference = preference_for(db, user)
    preference.enabled = enabled
    filters = normalized_filters(db, user, puzzle_types, themes, tag_ids, creator_ids, 25)
    filters.pop("limit", None)
    preference.filters = filters
    db.add(preference)
    db.commit()
    return RedirectResponse("/account/notifications?message=Notification%20preferences%20saved.", status_code=303)


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionWrite(BaseModel):
    endpoint: str
    keys: PushKeys
    device_name: str = "Browser"


@router.post("/api/notifications/subscriptions")
def create_push_subscription(payload: PushSubscriptionWrite, request: Request, db: Session = Depends(get_db)):
    user = core().current_user(request, db)
    if user is None:
        raise HTTPException(401, "Sign in to enable notifications")
    endpoint = validate_subscription_endpoint(payload.endpoint)
    p256dh = payload.keys.p256dh.strip()
    auth = payload.keys.auth.strip()
    if not PUSH_KEY_PATTERN.fullmatch(p256dh) or not PUSH_KEY_PATTERN.fullmatch(auth):
        raise HTTPException(422, "Push subscription keys are invalid")
    endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    subscription = db.scalar(select(PushSubscription).where(PushSubscription.endpoint_hash == endpoint_hash))
    if subscription is not None and subscription.user_id != user.id:
        raise HTTPException(409, "This browser subscription belongs to another account")
    if subscription is None:
        subscription = PushSubscription(id=uuid.uuid4().hex[:26], user_id=user.id, endpoint_hash=endpoint_hash, endpoint=endpoint)
    subscription.p256dh = p256dh
    subscription.auth = auth
    subscription.device_name = (payload.device_name.strip() or "Browser")[:120]
    subscription.user_agent = request.headers.get("user-agent", "")[:300]
    subscription.revoked_at = None
    subscription.last_error = ""
    preference = preference_for(db, user)
    preference.enabled = True
    db.add_all([subscription, preference])
    db.commit()
    return JSONResponse({"id": subscription.id, "enabled": True}, status_code=201)


@router.post("/account/notifications/subscriptions/{subscription_id}/revoke")
def revoke_push_subscription(subscription_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    subscription = db.get(PushSubscription, subscription_id)
    if subscription is None or subscription.user_id != user.id:
        raise HTTPException(404, "Browser subscription not found")
    if subscription.revoked_at is None:
        subscription.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/account/notifications?message=Browser%20notifications%20disabled.", status_code=303)


@router.post("/account/notifications/read")
def mark_notifications_read(request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    now = datetime.now(timezone.utc)
    unread = db.scalars(select(PuzzleNotification).where(
        PuzzleNotification.user_id == user.id,
        PuzzleNotification.read_at.is_(None),
    )).all()
    for notification in unread:
        notification.read_at = now
    if unread:
        db.commit()
    return RedirectResponse("/account/notifications", status_code=303)


@router.get("/api/notifications")
def notification_indicator(request: Request, db: Session = Depends(get_db)):
    user = core().current_user(request, db)
    if user is None:
        raise HTTPException(401, "Sign in required")
    unread_count = db.scalar(select(func.count()).select_from(PuzzleNotification).where(
        PuzzleNotification.user_id == user.id,
        PuzzleNotification.read_at.is_(None),
    )) or 0
    unread = db.scalars(select(PuzzleNotification).where(
        PuzzleNotification.user_id == user.id,
        PuzzleNotification.read_at.is_(None),
    ).order_by(PuzzleNotification.created_at.desc()).limit(5)).all()
    return {
        "unread": unread_count,
        "items": [
            {"id": item.id, "title": item.title, "body": item.body, "url": f"/puzzles/{item.puzzle_id}"}
            for item in unread[:5]
        ],
    }


@router.get("/service-worker.js")
def notification_service_worker():
    path = core().BASE_DIR / "static" / "notification-service-worker.js"
    return Response(
        path.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )
