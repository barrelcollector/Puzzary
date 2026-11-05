"""Revocable, permission-aware RSS and Atom puzzle feeds."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import os
import secrets
import threading
import time
import uuid
from urllib.parse import quote
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Puzzle, PuzzleFeedToken, PuzzleTag, User
from app.permissions import can_discover


router = APIRouter()
ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/elements/1.1/"
ET.register_namespace("", ATOM_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("dc", DC_NS)


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


FEED_RATE_LIMIT_MAX = _positive_env("PUZZARY_FEED_RATE_LIMIT", 120)
FEED_RATE_LIMIT_WINDOW = _positive_env("PUZZARY_FEED_RATE_WINDOW_SECONDS", 300)
FEED_RATE_BUCKETS: dict[str, deque[float]] = {}
FEED_RATE_LOCK = threading.Lock()


def core():
    from app import main
    return main


def utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def feed_url(secret: str, feed_format: str, db: Session) -> str:
    return f"{core().app_base_url(db)}/feeds/{quote(secret, safe='')}.{feed_format}"


def create_secret() -> str:
    return secrets.token_urlsafe(32)


def puzzle_is_feed_available(db: Session, user: User, puzzle: Puzzle) -> bool:
    if puzzle.archived_at is not None or puzzle.access_password_hash or not can_discover(user, puzzle):
        return False
    try:
        core().require_series_access(db, puzzle, user)
    except HTTPException as error:
        if error.status_code == 423:
            return False
        raise
    return True


def visible_filter_options(db: Session, user: User) -> tuple[list, list[str], list[PuzzleTag], list[User]]:
    puzzles = db.scalars(
        select(Puzzle).options(selectinload(Puzzle.tags), selectinload(Puzzle.creator))
    ).all()
    puzzles = [puzzle for puzzle in puzzles if puzzle_is_feed_available(db, user, puzzle)]
    puzzle_types = {puzzle.puzzle_type for puzzle in puzzles}
    plugins = [plugin for plugin in core().list_puzzle_types() if plugin.slug in puzzle_types]
    themes = sorted({puzzle.theme for puzzle in puzzles if puzzle.theme}, key=str.casefold)
    tags_by_id = {
        tag.id: tag for puzzle in puzzles for tag in puzzle.tags if tag.tag_type != "Clue"
    }
    creators_by_id = {
        puzzle.creator.id: puzzle.creator for puzzle in puzzles if puzzle.creator is not None
    }
    return (
        sorted(plugins, key=lambda plugin: plugin.label.casefold()),
        themes,
        sorted(tags_by_id.values(), key=lambda tag: tag.label.casefold()),
        sorted(creators_by_id.values(), key=lambda creator: creator.display_name.casefold()),
    )


def normalized_filters(
    db: Session,
    user: User,
    puzzle_types: list[str],
    themes: list[str],
    tag_ids: list[int],
    creator_ids: list[int],
    entry_limit: int,
) -> dict:
    available_types, available_themes, available_tags, available_creators = visible_filter_options(db, user)
    type_values = {plugin.slug for plugin in available_types}
    theme_values = set(available_themes)
    tag_values = {tag.id for tag in available_tags}
    creator_values = {creator.id for creator in available_creators}
    return {
        "puzzle_types": sorted({value for value in puzzle_types if value in type_values}),
        "themes": sorted({value for value in themes if value in theme_values}, key=str.casefold),
        "tag_ids": sorted({value for value in tag_ids if value in tag_values}),
        "creator_ids": sorted({value for value in creator_ids if value in creator_values}),
        "limit": entry_limit if entry_limit in {10, 25, 50} else 25,
    }


def feed_filter_summary(filters: dict, db: Session) -> str:
    parts: list[str] = []
    if filters.get("puzzle_types"):
        parts.append("types: " + ", ".join(filters["puzzle_types"]))
    if filters.get("themes"):
        parts.append("themes: " + ", ".join(filters["themes"]))
    if filters.get("tag_ids"):
        tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(filters["tag_ids"]))).all()
        parts.append("tags: " + ", ".join(sorted(tag.label for tag in tags)))
    if filters.get("creator_ids"):
        creators = db.scalars(select(User).where(User.id.in_(filters["creator_ids"]))).all()
        parts.append("creators: " + ", ".join(sorted(user.display_name for user in creators)))
    parts.append(f"latest {int(filters.get('limit', 25))}")
    return " · ".join(parts) if parts else "All available puzzles"


def account_feed_context(
    request: Request,
    db: Session,
    user: User,
    *,
    created_secret: str = "",
    created_name: str = "",
) -> dict:
    puzzle_types, themes, tags, creators = visible_filter_options(db, user)
    tokens = db.scalars(
        select(PuzzleFeedToken)
        .where(PuzzleFeedToken.user_id == user.id)
        .order_by(desc(PuzzleFeedToken.created_at))
    ).all()
    return {
        "request": request,
        "current_user": user,
        "tokens": tokens,
        "token_summaries": {token.id: feed_filter_summary(token.filters or {}, db) for token in tokens},
        "puzzle_types": puzzle_types,
        "themes": themes,
        "tags": tags,
        "creators": creators,
        "created_name": created_name,
        "created_atom_url": feed_url(created_secret, "atom", db) if created_secret else "",
        "created_rss_url": feed_url(created_secret, "rss", db) if created_secret else "",
        "http_warning": not core().app_base_url(db).lower().startswith("https://"),
        "error": request.query_params.get("error", ""),
        "message": request.query_params.get("message", ""),
    }


@router.get("/account/feeds", response_class=HTMLResponse)
def account_feeds(request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return core().templates.TemplateResponse("account_feeds.html", account_feed_context(request, db, user))


@router.post("/account/feeds", response_class=HTMLResponse, status_code=201)
def account_feed_create(
    request: Request,
    name: str = Form("Puzzle feed"),
    puzzle_types: list[str] = Form(default=[]),
    themes: list[str] = Form(default=[]),
    tag_ids: list[int] = Form(default=[]),
    creator_ids: list[int] = Form(default=[]),
    entry_limit: int = Form(25),
    db: Session = Depends(get_db),
):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    clean_name = name.strip()[:120]
    if not clean_name:
        raise HTTPException(status_code=422, detail="Feed name is required")
    secret = create_secret()
    token = PuzzleFeedToken(
        id=uuid.uuid4().hex[:26],
        user_id=user.id,
        token_hash=core().hash_secret(secret),
        token_prefix=secret[:8],
        name=clean_name,
        filters=normalized_filters(db, user, puzzle_types, themes, tag_ids, creator_ids, entry_limit),
    )
    db.add(token)
    db.commit()
    return core().templates.TemplateResponse(
        "account_feeds.html",
        account_feed_context(request, db, user, created_secret=secret, created_name=clean_name),
        status_code=201,
    )


def owned_token(db: Session, user: User, token_id: str) -> PuzzleFeedToken:
    token = db.get(PuzzleFeedToken, token_id)
    if token is None or token.user_id != user.id:
        raise HTTPException(status_code=404, detail="Feed token not found")
    return token


@router.post("/account/feeds/{token_id}/rotate", response_class=HTMLResponse)
def account_feed_rotate(token_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    token = owned_token(db, user, token_id)
    if token.revoked_at is not None:
        raise HTTPException(status_code=409, detail="Revoked feed tokens cannot be rotated")
    secret = create_secret()
    token.token_hash = core().hash_secret(secret)
    token.token_prefix = secret[:8]
    token.rotated_at = core().utcnow()
    token.last_used_at = None
    db.commit()
    with FEED_RATE_LOCK:
        FEED_RATE_BUCKETS.pop(token.id, None)
    return core().templates.TemplateResponse(
        "account_feeds.html",
        account_feed_context(request, db, user, created_secret=secret, created_name=token.name),
    )


@router.post("/account/feeds/{token_id}/revoke")
def account_feed_revoke(token_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    token = owned_token(db, user, token_id)
    if token.revoked_at is None:
        token.revoked_at = core().utcnow()
        db.commit()
    return RedirectResponse("/account/feeds?message=Feed%20access%20revoked.", status_code=303)


def resolve_feed_token(secret: str, db: Session) -> PuzzleFeedToken:
    if len(secret) < 32 or len(secret) > 128:
        raise HTTPException(status_code=404, detail="Feed not found")
    token = db.scalar(select(PuzzleFeedToken).where(
        PuzzleFeedToken.token_hash == core().hash_secret(secret),
        PuzzleFeedToken.revoked_at.is_(None),
    ))
    if token is None or token.user is None or not token.user.is_active or not token.user.can("library.view"):
        raise HTTPException(status_code=404, detail="Feed not found")
    now = time.monotonic()
    with FEED_RATE_LOCK:
        bucket = FEED_RATE_BUCKETS.setdefault(token.id, deque())
        while bucket and now - bucket[0] >= FEED_RATE_LIMIT_WINDOW:
            bucket.popleft()
        if len(bucket) >= FEED_RATE_LIMIT_MAX:
            raise HTTPException(
                status_code=429,
                detail="Feed request limit reached; try again shortly",
                headers={"Retry-After": str(min(FEED_RATE_LIMIT_WINDOW, 60))},
            )
        bucket.append(now)
    return token


def puzzle_feed_items(token: PuzzleFeedToken, db: Session) -> list[Puzzle]:
    filters = token.filters or {}
    puzzles = db.scalars(
        select(Puzzle)
        .options(selectinload(Puzzle.tags), selectinload(Puzzle.creator))
    ).all()
    puzzles.sort(key=lambda puzzle: utc(puzzle.publish_at or puzzle.created_at), reverse=True)
    selected: list[Puzzle] = []
    selected_types = set(filters.get("puzzle_types") or [])
    selected_themes = set(filters.get("themes") or [])
    selected_tags = {int(value) for value in (filters.get("tag_ids") or [])}
    selected_creators = {int(value) for value in (filters.get("creator_ids") or [])}
    for puzzle in puzzles:
        if not puzzle_is_feed_available(db, token.user, puzzle):
            continue
        if selected_types and puzzle.puzzle_type not in selected_types:
            continue
        if selected_themes and puzzle.theme not in selected_themes:
            continue
        if selected_tags and not ({tag.id for tag in puzzle.tags} & selected_tags):
            continue
        if selected_creators and puzzle.created_by_id not in selected_creators:
            continue
        selected.append(puzzle)
        if len(selected) >= int(filters.get("limit", 25)):
            break
    return selected


def safe_puzzle_metadata(puzzle: Puzzle, db: Session) -> dict:
    plugin = core().get_puzzle_type(puzzle.puzzle_type)
    type_label = plugin.label if plugin is not None else puzzle.puzzle_type.replace("_", " ").title()
    tags = sorted(tag.label for tag in puzzle.tags if tag.tag_type != "Clue")
    details = [f"{type_label} puzzle", f"Difficulty: {puzzle.difficulty.title()}"]
    if puzzle.theme:
        details.append(f"Theme: {puzzle.theme}")
    if tags:
        details.append("Tags: " + ", ".join(tags))
    return {
        "id": f"urn:puzzary:puzzle:{puzzle.id}",
        "title": puzzle.title,
        "url": core().puzzle_url(puzzle.id, db),
        "summary": " · ".join(details),
        "published": utc(puzzle.publish_at or puzzle.created_at),
        "puzzle_type": puzzle.puzzle_type,
        "type_label": type_label,
        "theme": puzzle.theme,
        "difficulty": puzzle.difficulty,
        "tags": tags,
        "creator": puzzle.creator.display_name if puzzle.creator else "",
    }


def atom_document(feed_id: str, name: str, url: str, updated_at: datetime, items: list[dict]) -> bytes:
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    ET.SubElement(feed, f"{{{ATOM_NS}}}id").text = f"urn:puzzary:feed:{feed_id}"
    ET.SubElement(feed, f"{{{ATOM_NS}}}title").text = name
    author = ET.SubElement(feed, f"{{{ATOM_NS}}}author")
    ET.SubElement(author, f"{{{ATOM_NS}}}name").text = "Puzzary"
    ET.SubElement(feed, f"{{{ATOM_NS}}}updated").text = updated_at.isoformat().replace("+00:00", "Z")
    ET.SubElement(feed, f"{{{ATOM_NS}}}link", {"rel": "self", "href": url, "type": "application/atom+xml"})
    for item in items:
        entry = ET.SubElement(feed, f"{{{ATOM_NS}}}entry")
        ET.SubElement(entry, f"{{{ATOM_NS}}}id").text = item["id"]
        ET.SubElement(entry, f"{{{ATOM_NS}}}title").text = item["title"]
        ET.SubElement(entry, f"{{{ATOM_NS}}}link", {"href": item["url"]})
        published = item["published"].isoformat().replace("+00:00", "Z")
        ET.SubElement(entry, f"{{{ATOM_NS}}}published").text = published
        ET.SubElement(entry, f"{{{ATOM_NS}}}updated").text = published
        ET.SubElement(entry, f"{{{ATOM_NS}}}summary", {"type": "text"}).text = item["summary"]
        if item["creator"]:
            author = ET.SubElement(entry, f"{{{ATOM_NS}}}author")
            ET.SubElement(author, f"{{{ATOM_NS}}}name").text = item["creator"]
        for category in [item["type_label"], item["theme"], *item["tags"]]:
            if category:
                ET.SubElement(entry, f"{{{ATOM_NS}}}category", {"term": category})
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def rss_document(name: str, url: str, home_url: str, updated_at: datetime, items: list[dict]) -> bytes:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = name
    ET.SubElement(channel, "link").text = home_url
    ET.SubElement(channel, "description").text = "Newly available Puzzary puzzles"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(updated_at, usegmt=True)
    ET.SubElement(channel, f"{{{ATOM_NS}}}link", {"href": url, "rel": "self", "type": "application/rss+xml"})
    for item in items:
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = item["title"]
        ET.SubElement(entry, "link").text = item["url"]
        ET.SubElement(entry, "guid", {"isPermaLink": "false"}).text = item["id"]
        ET.SubElement(entry, "pubDate").text = format_datetime(item["published"], usegmt=True)
        ET.SubElement(entry, "description").text = item["summary"]
        if item["creator"]:
            ET.SubElement(entry, f"{{{DC_NS}}}creator").text = item["creator"]
        for category in [item["type_label"], item["theme"], *item["tags"]]:
            if category:
                ET.SubElement(entry, "category").text = category
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


@router.get("/feeds/{secret}.{feed_format}")
def puzzle_feed(secret: str, feed_format: str, db: Session = Depends(get_db)):
    if feed_format not in {"atom", "rss"}:
        raise HTTPException(status_code=404, detail="Feed not found")
    token = resolve_feed_token(secret, db)
    puzzles = puzzle_feed_items(token, db)
    items = [safe_puzzle_metadata(puzzle, db) for puzzle in puzzles]
    url = feed_url(secret, feed_format, db)
    title = f"Puzzary · {token.name}"
    updated_at = items[0]["published"] if items else utc(token.rotated_at or token.created_at)
    if feed_format == "atom":
        body = atom_document(token.id, title, url, updated_at, items)
        media_type = "application/atom+xml"
    else:
        body = rss_document(title, url, core().app_base_url(db) + "/library", updated_at, items)
        media_type = "application/rss+xml"
    now = core().utcnow()
    if token.last_used_at is None or now - utc(token.last_used_at) > timedelta(minutes=5):
        token.last_used_at = now
        db.commit()
    return Response(body, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    })
