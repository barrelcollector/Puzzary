from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import uuid
import hashlib
import hmac
import math
import secrets
import time
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import qrcode
from qrcode.image.pure import PyPNGImage
from qrcode.image.svg import SvgPathImage
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from playwright.sync_api import Error as PlaywrightError
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.content import THEMES
from app.database import SessionLocal, ensure_schema, get_db, init_db
from app.dictionary_imports import ImportedWord, fetch_dictionary_source, parse_imported_words
from app.print_pdf import render_pdf
from app.models import (
    AppSetting,
    DictionaryDefinition,
    DictionaryImportSource,
    DictionaryWord,
    Puzzle,
    PuzzleAttempt,
    PuzzleCategory,
    PuzzleDictionary,
    PuzzleHint,
    PuzzlePhoto,
    PuzzleProgress,
    PublishedIssue,
    PuzzleTag,
    User,
    WordBank,
    WordBankWord,
)
from app.puzzles import fallback_preview, get_puzzle_type, list_puzzle_types
from app.schemas import (
    DictionaryDefinitionPatch,
    DictionaryDefinitionWrite,
    DictionaryImportRequest,
    DictionaryImportSourceWrite,
    DictionaryWordPatch,
    DictionaryWordWrite,
    PuzzleTagPatch,
    PuzzleTagWrite,
    PuzzleAttemptCreate,
    PuzzleAttemptResponse,
    PuzzleGenerateRequest,
    PuzzleProgressResponse,
    PuzzleProgressUpdate,
    PuzzleImportRequest,
    PuzzleManualWrite,
    PuzzlePrintPreview,
    PuzzleSummary,
    UserCreate,
    WordBankEntryWrite,
    WordBankPatch,
    WordBankWrite,
)


DEFAULT_APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8790").rstrip("/")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-local-session-secret")
PUZZARY_TIMEZONE = os.getenv("PUZZARY_TIMEZONE", "America/New_York")
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Puzzary", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", max_age=60 * 60 * 24 * 7)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
from app.publishing import router as publishing_router
app.include_router(publishing_router)

DICTIONARY_SYNC_CHECK_SECONDS = 15 * 60

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 390000
RESET_TOKEN_TTL_HOURS = 24
LOGIN_RATE_LIMIT_WINDOW = 300
LOGIN_RATE_LIMIT_MAX = 8
LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def tag_value(tags: list[PuzzleTag], tag_type: str) -> str:
    for tag in tags:
        if tag.tag_type == tag_type:
            return tag.name
    return ""


def tag_values(tags: list[PuzzleTag], tag_type: str) -> list[str]:
    return [tag.name for tag in tags if tag.tag_type == tag_type]


def tag_lines(tags: list[PuzzleTag], tag_type: str) -> str:
    return "\n".join(tag_values(tags, tag_type))


templates.env.globals["tag_value"] = tag_value
templates.env.globals["tag_values"] = tag_values
templates.env.globals["tag_lines"] = tag_lines


def app_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(PUZZARY_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def local_datetime_value(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(app_timezone()).strftime("%Y-%m-%dT%H:%M")


def local_datetime_label(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(app_timezone()).strftime("%Y-%m-%d %I:%M %p %Z")


def default_auto_archive_at() -> datetime:
    return utcnow() + timedelta(hours=24)


def default_auto_archive_value() -> str:
    return local_datetime_value(default_auto_archive_at())


templates.env.globals["local_datetime_value"] = local_datetime_value
templates.env.globals["local_datetime_label"] = local_datetime_label
templates.env.globals["default_auto_archive_value"] = default_auto_archive_value
templates.env.globals["puzzary_timezone"] = PUZZARY_TIMEZONE


def template_exists(template_name: str) -> bool:
    try:
        templates.env.get_template(template_name)
    except TemplateNotFound:
        return False
    return True


@app.on_event("startup")
async def startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ensure_schema()
    init_db()
    with SessionLocal() as db:
        ensure_admin_user(db)
        seed_word_banks(db)
        seed_word_definitions(db)
        apply_due_auto_archives(db)
        run_due_dictionary_import_sources(db)
    app.state.auto_archive_task = asyncio.create_task(auto_archive_loop())
    app.state.dictionary_sync_task = asyncio.create_task(dictionary_import_source_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    task = getattr(app.state, "auto_archive_task", None)
    if task is not None:
        task.cancel()
    sync_task = getattr(app.state, "dictionary_sync_task", None)
    if sync_task is not None:
        sync_task.cancel()


def normalize_base_url(value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL must start with http:// or https:// and include a host.")
    return text


def app_setting(db: Session, key: str, default: str = "") -> str:
    setting = db.get(AppSetting, key)
    if setting is None or not setting.value:
        return default
    return setting.value


def set_app_setting(db: Session, key: str, value: str) -> None:
    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=value)
        db.add(setting)
    else:
        setting.value = value


def disabled_puzzle_type_slugs(db: Session) -> set[str]:
    raw_value = app_setting(db, "disabled_puzzle_types", "[]")
    try:
        values = json.loads(raw_value)
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        return set()
    registered = {puzzle_type.slug for puzzle_type in list_puzzle_types()}
    return {str(value) for value in values if str(value) in registered}


def set_disabled_puzzle_type_slugs(db: Session, slugs: list[str]) -> None:
    registered = {puzzle_type.slug for puzzle_type in list_puzzle_types()}
    disabled = sorted({str(slug) for slug in slugs if str(slug) in registered})
    set_app_setting(db, "disabled_puzzle_types", json.dumps(disabled))


def is_puzzle_type_enabled(db: Session, slug: str) -> bool:
    return slug not in disabled_puzzle_type_slugs(db)


def list_enabled_puzzle_types(db: Session) -> list:
    disabled = disabled_puzzle_type_slugs(db)
    return [puzzle_type for puzzle_type in list_puzzle_types() if puzzle_type.slug not in disabled]


def app_base_url(db: Session | None = None) -> str:
    if db is None:
        return DEFAULT_APP_BASE_URL
    return app_setting(db, "public_base_url", DEFAULT_APP_BASE_URL).rstrip("/")


def puzzle_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}"


def puzzle_print_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}/print"


def puzzle_print_render_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}/print/render"


def puzzle_qr_short_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/q/{puzzle_id}"


def puzzle_qr_svg_url(puzzle_id: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/puzzles/{puzzle_id}/qr.svg"


PRINT_PAGE_PRESETS = {
    "receipt": {"label": "Receipt roll", "width_mm": 80.0, "height_mm": None, "margin_mm": 4.0},
    "zebra": {"label": "Zebra roll", "width_mm": 80.0, "height_mm": None, "margin_mm": 4.0},
    "4x6": {"label": "4 x 6 in", "width_mm": 101.6, "height_mm": 152.4, "margin_mm": 5.0},
    "letter": {"label": "US Letter", "width_mm": 215.9, "height_mm": 279.4, "margin_mm": 8.0},
    "a4": {"label": "A4", "width_mm": 210.0, "height_mm": 297.0, "margin_mm": 8.0},
}


def positive_float(value: str | None, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return number


def apply_print_request_layout(layout: dict, request: Request | None = None) -> dict:
    params = request.query_params if request is not None else {}
    requested_size = str(
        params.get("page_size") or params.get("size") or layout.get("page_size") or "receipt"
    ).strip().lower()
    if requested_size not in PRINT_PAGE_PRESETS and requested_size != "custom":
        requested_size = "receipt"

    preset = PRINT_PAGE_PRESETS.get(requested_size, PRINT_PAGE_PRESETS["receipt"])
    width_mm = positive_float(params.get("width_mm"), preset["width_mm"])
    height_mm = positive_float(params.get("height_mm"), preset["height_mm"])
    margin_mm = positive_float(params.get("margin_mm"), preset["margin_mm"]) or 0
    if width_mm is None:
        width_mm = PRINT_PAGE_PRESETS["receipt"]["width_mm"]

    if not 25 <= width_mm <= 1000 or (height_mm is not None and not 25 <= height_mm <= 5000):
        raise HTTPException(status_code=422, detail="Width must be 25-1000mm and height 25-5000mm")
    if params.get("margin_mm") == "0":
        margin_mm = 0

    shortest_side = min(width_mm, height_mm) if height_mm is not None else width_mm
    margin_mm = min(margin_mm, max((shortest_side - 10) / 2, 0))
    content_width_mm = max(width_mm - (margin_mm * 2), 10)

    layout["page_size"] = requested_size
    layout["page_size_label"] = "Custom" if requested_size == "custom" else preset["label"]
    layout["page_width_mm"] = round(width_mm, 2)
    layout["page_height_mm"] = round(height_mm, 2) if height_mm else None
    layout["page_margin_mm"] = round(margin_mm, 2)
    layout["content_width_mm"] = round(content_width_mm, 2)
    layout["page_size_presets"] = [
        {"value": key, "label": value["label"]} for key, value in PRINT_PAGE_PRESETS.items()
    ] + [{"value": "custom", "label": "Custom"}]
    return layout


def qr_png_data_uri(value: str) -> str:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=8)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=PyPNGImage)
    buffer = BytesIO()
    image.save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def print_layout_context(puzzle: Puzzle, puzzle_type, db: Session | None = None) -> dict:
    layout = puzzle_type.print_layout.as_dict()
    if puzzle_type.print_layout_builder is not None:
        try:
            override = puzzle_type.print_layout_builder(
                puzzle.print_payload or {},
                puzzle.play_payload or {},
                puzzle.puzzle_metadata or {},
            )
        except Exception:
            override = {}
        if isinstance(override, dict):
            layout.update(override)
    layout.setdefault("slug", puzzle.puzzle_type)
    layout.setdefault("sheet_class", "print-layout-default")
    layout.setdefault("page_size", "receipt")
    layout.setdefault("fit_to_width", True)
    layout.setdefault("include_open_link", True)
    layout.setdefault("include_solution", False)
    layout.setdefault("show_prompt", True)
    layout.setdefault("instructions", "")
    layout.setdefault("notes", [])
    layout.setdefault("max_width_px", 760)
    layout["url"] = puzzle_url(puzzle.id, db)
    layout["print_url"] = puzzle_print_url(puzzle.id, db)
    layout["render_url"] = puzzle_print_render_url(puzzle.id, db)
    layout["short_url"] = puzzle_qr_short_url(puzzle.id, db)
    layout["qr_url"] = puzzle_qr_svg_url(puzzle.id, db)
    layout["qr_target_url"] = puzzle_qr_short_url(puzzle.id, db)
    layout["qr_data_uri"] = qr_png_data_uri(layout["qr_target_url"])
    return layout


def serialize_puzzle(puzzle: Puzzle, db: Session | None = None) -> PuzzleSummary:
    return PuzzleSummary(
        id=puzzle.id,
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        theme=puzzle.theme,
        difficulty=puzzle.difficulty,
        url=puzzle_url(puzzle.id, db),
        print_payload=puzzle.print_payload,
        play_payload=puzzle.play_payload,
        metadata=puzzle.puzzle_metadata or {},
        hints=[hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
    )


def portable_puzzle_item(puzzle: Puzzle, db: Session | None = None) -> dict:
    return {
        "schema": "puzzary.puzzle.item.v1",
        "origin": {
            "id": puzzle.id,
            "url": puzzle_url(puzzle.id, db),
            "exported_from": app_base_url(db),
        },
        "puzzle_type": puzzle.puzzle_type,
        "title": puzzle.title,
        "theme": puzzle.theme,
        "difficulty": puzzle.difficulty,
        "prompt": puzzle.prompt,
        "print_payload": puzzle.print_payload or {},
        "play_payload": puzzle.play_payload or {},
        "solution_payload": puzzle.solution_payload or {},
        "metadata": puzzle.puzzle_metadata or {},
        "hints": [hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
        "category": puzzle.category.name if puzzle.category else "",
        "tags": [tag.label for tag in puzzle.tags],
        "source": puzzle.source,
        "created_at": puzzle.created_at.isoformat() if puzzle.created_at else None,
        "attribution": {
            "creator": creator_label(puzzle),
            "license": (puzzle.puzzle_metadata or {}).get("license", ""),
        },
    }


def portable_puzzle_bundle(puzzles: list[Puzzle], db: Session | None = None) -> dict:
    return {
        "schema": "puzzary.puzzle.bundle.v1",
        "exported_at": utcnow().isoformat(),
        "exported_from": app_base_url(db),
        "count": len(puzzles),
        "items": [portable_puzzle_item(puzzle, db) for puzzle in puzzles],
    }


def puzzle_import_sources_from_github(url: str, api_token: str = "") -> list[dict[str, str]]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "gist.github.com":
        gist_id = parsed.path.strip("/").split("/")[-1]
        if not gist_id:
            raise ValueError("GitHub Gist URL is missing a gist id")
        gist_api_url = f"https://api.github.com/gists/{gist_id}"
        gist_payload = json.loads(fetch_dictionary_source(gist_api_url, "generic", api_token)[0]["text"])
        sources = []
        for file_data in gist_payload.get("files", {}).values():
            raw_url = file_data.get("raw_url")
            filename = str(file_data.get("filename", "")).lower()
            if raw_url and filename.endswith((".json", ".puzzary")):
                sources.extend(fetch_dictionary_source(raw_url, "generic", api_token))
        if not sources:
            raise ValueError("No JSON puzzle files were found in that Gist")
        return sources
    sources = fetch_dictionary_source(url, "github", api_token)
    if len(sources) > 1:
        sources = [
            source for source in sources
            if str(source.get("url", "")).lower().endswith((".json", ".puzzary"))
        ]
    if not sources:
        raise ValueError("No JSON puzzle files were found in that GitHub source")
    return sources


def fetch_puzzle_import_sources(source_url: str, api_token: str = "") -> list[dict[str, str]]:
    url = source_url.strip()
    if not url:
        return []
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Puzzle import source must be an http or https URL")
    if parsed.netloc.lower() == "pastebin.com" or parsed.netloc.lower().endswith("github.com"):
        if "github.com" in parsed.netloc.lower():
            return puzzle_import_sources_from_github(url, api_token)
        return fetch_dictionary_source(url, "generic", api_token)
    if parsed.path.rstrip("/").endswith("/api/puzzles/export"):
        return fetch_dictionary_source(url, "generic", api_token)
    if re.search(r"/api/puzzles/[^/]+/export$", parsed.path.rstrip("/")):
        return fetch_dictionary_source(url, "generic", api_token)
    if parsed.path.rstrip("/") and parsed.path.rstrip("/") != "/":
        return fetch_dictionary_source(url, "generic", api_token)
    export_url = f"{parsed.scheme}://{parsed.netloc}/api/puzzles/export"
    return fetch_dictionary_source(export_url, "generic", api_token)


def puzzle_items_from_manifest(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("Puzzle import must be a JSON object")
    schema = str(payload.get("schema", ""))
    if schema == "puzzary.puzzle.bundle.v1":
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise ValueError("Puzzle bundle items must be a list")
        return [item for item in items if isinstance(item, dict)]
    if schema == "puzzary.puzzle.item.v1" or {"puzzle_type", "print_payload", "play_payload", "solution_payload"} <= set(payload.keys()):
        return [payload]
    if "items" in payload and isinstance(payload["items"], list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    raise ValueError("Unsupported puzzle import schema")


def parse_puzzle_import_items(source_text: str = "", source_url: str = "", api_token: str = "") -> list[dict]:
    sources: list[dict[str, str]] = []
    if source_text.strip():
        sources.append({"url": "pasted-content", "content_type": "application/json", "text": source_text})
    sources.extend(fetch_puzzle_import_sources(source_url, api_token))
    if not sources:
        raise ValueError("Provide pasted JSON, an uploaded file, or a source URL")
    items: list[dict] = []
    for source in sources:
        try:
            payload = json.loads(source["text"])
        except json.JSONDecodeError as error:
            raise ValueError(f"Puzzle source is not valid JSON: {error}") from error
        source_items = puzzle_items_from_manifest(payload)
        for item in source_items:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            item["metadata"] = {**metadata, "import_source_url": source.get("url", source_url)}
            items.append(item)
    return items


def normalize_import_id(item: dict) -> str:
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    return str(origin.get("id") or item.get("id") or "").strip()


def existing_imported_puzzle(db: Session, item: dict) -> Puzzle | None:
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    origin_id = normalize_import_id(item)
    origin_url = str(origin.get("url") or "").strip()
    imported = db.scalars(select(Puzzle).where(Puzzle.source == "imported")).all()
    for puzzle in imported:
        metadata = puzzle.puzzle_metadata if isinstance(puzzle.puzzle_metadata, dict) else {}
        imported_origin = metadata.get("import_origin") if isinstance(metadata.get("import_origin"), dict) else {}
        if origin_id and str(imported_origin.get("id") or "") == origin_id:
            return puzzle
        if origin_url and str(imported_origin.get("url") or "") == origin_url:
            return puzzle
    return None


def validate_portable_puzzle_item(db: Session, item: dict) -> tuple[bool, str]:
    puzzle_type_slug = str(item.get("puzzle_type") or "").strip()
    if not puzzle_type_slug:
        return False, "Missing puzzle_type"
    if get_puzzle_type(puzzle_type_slug) is None:
        return False, f"Puzzle type '{puzzle_type_slug}' is not installed"
    if not is_puzzle_type_enabled(db, puzzle_type_slug):
        return False, f"Puzzle type '{puzzle_type_slug}' is disabled"
    for key in ("print_payload", "play_payload", "solution_payload"):
        if not isinstance(item.get(key), dict):
            return False, f"{key} must be an object"
    return True, ""


def validate_manual_puzzle_payload(db: Session, payload: PuzzleManualWrite, allow_draft: bool = True) -> tuple[bool, str]:
    item = {
        "puzzle_type": payload.puzzle_type,
        "print_payload": payload.print_payload,
        "play_payload": payload.play_payload,
        "solution_payload": payload.solution_payload,
    }
    valid, error = validate_portable_puzzle_item(db, item)
    if not valid:
        return False, error
    if payload.draft and allow_draft:
        return True, ""
    for key in ("print_payload", "play_payload", "solution_payload"):
        value = getattr(payload, key)
        if not value:
            return False, f"{key} is required before publishing"
        kind = str(value.get("kind") or "").strip()
        if not kind:
            return False, f"{key}.kind is required before publishing"
        if kind != payload.puzzle_type:
            return False, f"{key}.kind must match puzzle_type"
    return True, ""


def parse_json_object_field(value: str, field_name: str) -> dict:
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field_name} must be valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def manual_payload_from_form(
    puzzle_type: str,
    title: str,
    theme: str,
    difficulty: str,
    prompt: str,
    print_payload: str,
    play_payload: str,
    solution_payload: str,
    metadata: str,
    hints: str,
    draft: str,
) -> PuzzleManualWrite:
    hint_lines = [line.strip() for line in hints.splitlines() if line.strip()]
    if not title.strip():
        raise ValueError("Title is required for raw payload mode")
    return PuzzleManualWrite(
        puzzle_type=puzzle_type.strip(),
        title=title.strip(),
        theme=(theme.strip() or "manual")[:80],
        difficulty=(difficulty.strip() or "manual")[:40],
        prompt=prompt.strip(),
        print_payload=parse_json_object_field(print_payload, "Print payload"),
        play_payload=parse_json_object_field(play_payload, "Play payload"),
        solution_payload=parse_json_object_field(solution_payload, "Solution payload"),
        metadata=parse_json_object_field(metadata, "Metadata"),
        hints=hint_lines,
        draft=draft == "true",
    )


def write_manual_puzzle(
    db: Session,
    payload: PuzzleManualWrite,
    creator: User | None,
    puzzle: Puzzle | None = None,
) -> Puzzle:
    valid, error = validate_manual_puzzle_payload(db, payload)
    if not valid:
        raise ValueError(error)
    metadata = dict(payload.metadata or {})
    metadata["manual"] = True
    metadata["draft"] = bool(payload.draft)
    metadata["manual_updated_at"] = utcnow().isoformat()
    now = datetime.now(timezone.utc)
    if puzzle is None:
        puzzle = Puzzle(
            id=uuid.uuid4().hex[:12],
            puzzle_type=payload.puzzle_type,
            title=payload.title[:160],
            theme=payload.theme[:80],
            difficulty=payload.difficulty[:40],
            prompt=payload.prompt,
            print_payload=payload.print_payload,
            play_payload=payload.play_payload,
            solution_payload=payload.solution_payload,
            puzzle_metadata=metadata,
            source="manual",
            auto_archive_at=None if payload.draft else default_auto_archive_at(),
            archived_at=now if payload.draft else None,
            created_by_id=creator.id if creator is not None else None,
        )
        db.add(puzzle)
        db.flush()
    else:
        puzzle.puzzle_type = payload.puzzle_type
        puzzle.title = payload.title[:160]
        puzzle.theme = payload.theme[:80]
        puzzle.difficulty = payload.difficulty[:40]
        puzzle.prompt = payload.prompt
        puzzle.print_payload = payload.print_payload
        puzzle.play_payload = payload.play_payload
        puzzle.solution_payload = payload.solution_payload
        puzzle.puzzle_metadata = metadata
        puzzle.source = "manual"
        puzzle.archived_at = now if payload.draft else None
        if not payload.draft and puzzle.auto_archive_at is None:
            puzzle.auto_archive_at = default_auto_archive_at()
    puzzle.hints.clear()
    db.flush()
    for index, hint_text in enumerate(payload.hints, start=1):
        db.add(PuzzleHint(puzzle_id=puzzle.id, order=index, text=hint_text))
    db.commit()
    db.refresh(puzzle)
    return puzzle


def puzzle_is_draft(puzzle: Puzzle) -> bool:
    metadata = puzzle.puzzle_metadata if isinstance(puzzle.puzzle_metadata, dict) else {}
    return bool(metadata.get("draft"))


def manual_puzzle_publish_error(db: Session, puzzle: Puzzle) -> str:
    metadata = puzzle.puzzle_metadata if isinstance(puzzle.puzzle_metadata, dict) else {}
    if not (metadata.get("manual") or puzzle.source == "manual"):
        return ""
    payload = PuzzleManualWrite(
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        theme=puzzle.theme,
        difficulty=puzzle.difficulty,
        prompt=puzzle.prompt,
        print_payload=puzzle.print_payload or {},
        play_payload=puzzle.play_payload or {},
        solution_payload=puzzle.solution_payload or {},
        metadata=metadata,
        hints=[hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
        draft=False,
    )
    valid, error = validate_manual_puzzle_payload(db, payload, allow_draft=False)
    return "" if valid else error


def active_word_definitions(word: DictionaryWord) -> list[DictionaryDefinition]:
    return [definition for definition in word.definitions if definition.archived_at is None]


templates.env.globals["active_word_definitions"] = active_word_definitions


def dashboard_review_context(
    db: Session,
    puzzles: list[Puzzle],
    dictionary_words: list[DictionaryWord],
    all_word_banks: list[WordBank],
    tags: list[PuzzleTag],
) -> dict:
    now = utcnow()
    soon_cutoff = now + timedelta(hours=24)
    draft_puzzles = [puzzle for puzzle in puzzles if puzzle_is_draft(puzzle)]
    incomplete_puzzles = []
    for puzzle in puzzles:
        error = manual_puzzle_publish_error(db, puzzle)
        if error:
            incomplete_puzzles.append({"puzzle": puzzle, "error": error})
    banned_words = [word for word in dictionary_words if word.banned_at is not None]
    archived_words = [word for word in dictionary_words if word.archived_at is not None]
    words_without_definitions = [
        word for word in dictionary_words
        if word.archived_at is None and word.banned_at is None and not active_word_definitions(word)
    ]
    definitions_without_clues = [
        definition
        for word in dictionary_words
        if word.archived_at is None and word.banned_at is None
        for definition in active_word_definitions(word)
        if not tag_value(definition.tags, "Clue").strip()
    ]
    archived_banks = [bank for bank in all_word_banks if bank.archived_at is not None]
    empty_banks = [
        bank for bank in all_word_banks
        if bank.archived_at is None and len(word_bank_entries(db, bank)) == 0
    ]
    unused_tags = [
        tag for tag in tags
        if not tag.puzzles and not tag.word_banks and not tag.dictionary_words and not tag.dictionary_definitions
    ]
    auto_archive_soon = [
        puzzle for puzzle in puzzles
        if puzzle.archived_at is None and puzzle.auto_archive_at is not None and now <= puzzle.auto_archive_at <= soon_cutoff
    ]
    return {
        "counts": {
            "total_puzzles": len(puzzles),
            "draft_puzzles": len(draft_puzzles),
            "incomplete_puzzles": len(incomplete_puzzles),
            "total_words": len(dictionary_words),
            "banned_words": len(banned_words),
            "archived_words": len(archived_words),
            "words_without_definitions": len(words_without_definitions),
            "definitions_without_clues": len(definitions_without_clues),
            "archived_banks": len(archived_banks),
            "empty_banks": len(empty_banks),
            "unused_tags": len(unused_tags),
            "auto_archive_soon": len(auto_archive_soon),
        },
        "draft_puzzles": draft_puzzles[:8],
        "incomplete_puzzles": incomplete_puzzles[:8],
        "banned_words": banned_words[:8],
        "archived_words": archived_words[:8],
        "words_without_definitions": words_without_definitions[:8],
        "definitions_without_clues": definitions_without_clues[:8],
        "archived_banks": archived_banks[:8],
        "empty_banks": empty_banks[:8],
        "unused_tags": unused_tags[:8],
        "auto_archive_soon": auto_archive_soon[:8],
    }


def import_portable_puzzles(
    db: Session,
    items: list[dict],
    creator: User | None = None,
    dry_run: bool = True,
    duplicate_policy: str = "skip",
    import_ids: list[str] | None = None,
) -> dict:
    selected_ids = {str(item_id) for item_id in (import_ids or []) if str(item_id).strip()}
    result = {
        "dry_run": dry_run,
        "total": len(items),
        "processed": 0,
        "imported": 0,
        "skipped": 0,
        "duplicates": 0,
        "unsupported": 0,
        "items": [],
        "errors": [],
    }
    for index, item in enumerate(items, start=1):
        origin_id = normalize_import_id(item) or f"item-{index}"
        if selected_ids and origin_id not in selected_ids:
            continue
        result["processed"] += 1
        valid, error = validate_portable_puzzle_item(db, item)
        row = {
            "origin_id": origin_id,
            "title": str(item.get("title") or "Imported Puzzle"),
            "puzzle_type": str(item.get("puzzle_type") or ""),
            "action": "preview",
            "error": error,
        }
        if not valid:
            result["unsupported"] += 1
            result["skipped"] += 1
            row["action"] = "unsupported"
            result["items"].append(row)
            continue
        duplicate = existing_imported_puzzle(db, item)
        if duplicate is not None and duplicate_policy != "copy":
            result["duplicates"] += 1
            result["skipped"] += 1
            row["action"] = "duplicate"
            row["existing_id"] = duplicate.id
            result["items"].append(row)
            continue
        row["action"] = "import" if not dry_run else "ready"
        result["items"].append(row)
        if dry_run:
            continue
        origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        metadata = {
            **metadata,
            "imported_at": utcnow().isoformat(),
            "import_origin": origin,
            "import_schema": str(item.get("schema") or "puzzary.puzzle.item.v1"),
        }
        puzzle = Puzzle(
            id=uuid.uuid4().hex[:12],
            puzzle_type=str(item.get("puzzle_type")),
            title=str(item.get("title") or "Imported Puzzle")[:160],
            theme=str(item.get("theme") or "imported")[:80],
            difficulty=str(item.get("difficulty") or "imported")[:40],
            prompt=str(item.get("prompt") or ""),
            print_payload=item.get("print_payload") or {},
            play_payload=item.get("play_payload") or {},
            solution_payload=item.get("solution_payload") or {},
            puzzle_metadata=metadata,
            source="imported",
            auto_archive_at=default_auto_archive_at(),
            created_by_id=creator.id if creator is not None else None,
        )
        db.add(puzzle)
        db.flush()
        for hint_index, hint_text in enumerate(item.get("hints") if isinstance(item.get("hints"), list) else [], start=1):
            if str(hint_text).strip():
                db.add(PuzzleHint(puzzle_id=puzzle.id, order=hint_index, text=str(hint_text).strip()))
        result["imported"] += 1
        row["local_id"] = puzzle.id
    if not dry_run:
        db.commit()
    return result


def puzzle_preview(puzzle: Puzzle) -> dict:
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    label = puzzle_type.label if puzzle_type is not None else puzzle.puzzle_type.replace("_", " ").title()
    if puzzle_type is None or puzzle_type.preview_builder is None:
        return fallback_preview(puzzle.puzzle_type, label)
    try:
        preview = puzzle_type.preview_builder(puzzle.print_payload or {}, puzzle.play_payload or {}, puzzle.puzzle_metadata or {})
    except Exception:
        return fallback_preview(puzzle.puzzle_type, label)
    if not isinstance(preview, dict) or not preview.get("kind"):
        return fallback_preview(puzzle.puzzle_type, label)
    preview.setdefault("label", label)
    preview.setdefault("meta", puzzle.puzzle_type.replace("_", " "))
    return preview


def parse_date_boundary(value: str, end_of_day: bool = False) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.fromisoformat(f"{text}T{'23:59:59' if end_of_day else '00:00:00'}")
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Date filter is not valid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=app_timezone())
    return parsed.astimezone(timezone.utc)


def progress_map_for_user(db: Session, user: User, puzzles: list[Puzzle]) -> dict[str, dict[str, int | str]]:
    progress_by_puzzle = {
        puzzle.id: {
            "found": 0,
            "total": progress_response(puzzle, user, None).expected_count,
            "status": "not_started",
        }
        for puzzle in puzzles
        if get_puzzle_type(puzzle.puzzle_type) is not None
    }
    if not progress_by_puzzle:
        return progress_by_puzzle
    progress_rows = db.scalars(
        select(PuzzleProgress).where(PuzzleProgress.user_id == user.id, PuzzleProgress.puzzle_id.in_(list(progress_by_puzzle.keys())))
    ).all()
    puzzles_by_id = {puzzle.id: puzzle for puzzle in puzzles}
    for progress in progress_rows:
        puzzle = puzzles_by_id.get(progress.puzzle_id)
        if puzzle is None:
            continue
        response = progress_response(puzzle, user, progress)
        status = "completed" if response.expected_count and response.found_count >= response.expected_count else "in_progress"
        progress_by_puzzle[progress.puzzle_id] = {
            "found": response.found_count,
            "total": response.expected_count,
            "status": status,
        }
    return progress_by_puzzle


def library_filter_state(request: Request, current_user: User) -> dict:
    params = request.query_params
    status = params.get("status", "any")
    if status not in {"any", "not_started", "in_progress", "completed"}:
        status = "any"
    completed = params.get("completed", "any")
    if completed not in {"any", "completed", "not_completed"}:
        completed = "any"
    started = params.get("started", "any")
    if started not in {"any", "started", "not_started"}:
        started = "any"
    archive_status = params.get("archive_status", "any")
    if archive_status not in {"any", "soon", "scheduled", "none"}:
        archive_status = "any"
    availability = params.get("availability", "active")
    if availability not in {"active", "archived", "scheduled", "any"}:
        availability = "active"
    sort = params.get("sort", "created_desc")
    valid_sorts = {
        "created_desc",
        "created_asc",
        "archive_asc",
        "archive_desc",
        "title_asc",
        "title_desc",
        "type_asc",
        "difficulty_asc",
        "creator_asc",
        "progress_desc",
        "progress_asc",
    }
    if sort not in valid_sorts:
        sort = "created_desc"
    return {
        "q": params.get("q", "").strip(),
        "types": params.getlist("type"),
        "tags": params.getlist("tag"),
        "themes": params.getlist("theme"),
        "difficulty": params.getlist("difficulty"),
        "status": status,
        "completed": completed,
        "started": started,
        "created_from": params.get("created_from", ""),
        "created_to": params.get("created_to", ""),
        "archive_from": params.get("archive_from", ""),
        "archive_to": params.get("archive_to", ""),
        "archive_status": archive_status,
        "created_by": params.get("created_by", "any"),
        "availability": availability,
        "sort": sort,
        "limit": max(12, min(200, int(params.get("limit", "60") or 60))) if str(params.get("limit", "60")).isdigit() else 60,
        "current_user_id": current_user.id,
    }


def library_theme_label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def library_theme_value(value: str) -> str:
    return slugify(library_theme_label(value))


def puzzle_theme_keys(puzzle: Puzzle) -> set[str]:
    keys = {puzzle.theme, slugify(puzzle.theme) if puzzle.theme else "", library_theme_value(puzzle.theme) if puzzle.theme else ""}
    if puzzle.category is not None:
        keys |= {str(puzzle.category.id), puzzle.category.slug, puzzle.category.name, library_theme_value(puzzle.category.name)}
    if puzzle.word_bank is not None:
        keys |= {puzzle.word_bank.slug, puzzle.word_bank.name, library_theme_value(puzzle.word_bank.name)}
    return {key for key in keys if key}


def creator_label(puzzle: Puzzle) -> str:
    return puzzle.creator.display_name if puzzle.creator is not None else "Unknown"


def puzzle_search_text(puzzle: Puzzle) -> str:
    parts = [
        puzzle.title,
        puzzle.prompt,
        puzzle.puzzle_type,
        puzzle.theme,
        puzzle.difficulty,
        puzzle.category.name if puzzle.category else "",
        puzzle.word_bank.name if puzzle.word_bank else "",
        creator_label(puzzle),
    ]
    parts.extend(tag.label for tag in puzzle.tags)
    return " ".join(part for part in parts if part).lower()


def active_filter_chips(filters: dict, options: dict) -> list[dict[str, str]]:
    chips: list[dict[str, str]] = []
    param_map = {
        "types": ("type", "Type", {item["value"]: item["label"] for item in options["types"]}),
        "tags": ("tag", "Tag", {item["value"]: item["label"] for item in options["tags"]}),
        "themes": ("theme", "Theme", {item["value"]: item["label"] for item in options["themes"]}),
        "difficulty": ("difficulty", "Difficulty", {item["value"]: item["label"] for item in options["difficulty"]}),
    }
    for key, (param, label, labels) in param_map.items():
        for value in filters[key]:
            chips.append({"param": param, "value": value, "label": f"{label}: {labels.get(value, value)}"})
    single_filters = [
        ("q", "Search", ""),
        ("status", "Status", "any"),
        ("completed", "Completed", "any"),
        ("started", "Started", "any"),
        ("created_from", "Created after", ""),
        ("created_to", "Created before", ""),
        ("archive_from", "Archives after", ""),
        ("archive_to", "Archives before", ""),
        ("archive_status", "Archive", "any"),
        ("created_by", "Creator", "any"),
        ("availability", "Availability", "active"),
        ("sort", "Sort", "created_desc"),
    ]
    creator_labels = {item["value"]: item["label"] for item in options["creators"]}
    sort_labels = {item["value"]: item["label"] for item in options["sorts"]}
    for key, label, default in single_filters:
        value = filters[key]
        if value == default or value == "":
            continue
        label_value = creator_labels.get(value, value) if key == "created_by" else sort_labels.get(value, value) if key == "sort" else value.replace("_", " ").title()
        chips.append({"param": key, "value": value, "label": f"{label}: {label_value}"})
    return chips


def query_without(request: Request, param: str, value: str | None = None) -> str:
    pairs = []
    for key, item in request.query_params.multi_items():
        if key == param and (value is None or item == value):
            continue
        pairs.append((key, item))
    query = urlencode(pairs, doseq=True)
    return f"/library?{query}" if query else "/library"


def safe_library_return_to(value: str) -> str:
    if not value.startswith("/library"):
        return "/library"
    if "://" in value or value.startswith("//"):
        return "/library"
    return value


def safe_return_to(value: str, default: str = "/library") -> str:
    if not value or "://" in value or value.startswith("//"):
        return default
    allowed_prefixes = ("/library", "/puzzles/", "/generate", "/admin", "/account")
    if not value.startswith(allowed_prefixes):
        return default
    return value


def library_options(db: Session, puzzles: list[Puzzle], current_user: User) -> dict:
    tags = db.scalars(select(PuzzleTag).order_by(PuzzleTag.tag_type, PuzzleTag.name)).all()
    users = db.scalars(select(User).order_by(User.display_name)).all()
    theme_values: dict[str, str] = {}
    for puzzle in puzzles:
        if puzzle.theme:
            theme_values.setdefault(library_theme_value(puzzle.theme), library_theme_label(puzzle.theme))
        if puzzle.category is not None:
            theme_values.setdefault(library_theme_value(puzzle.category.name), puzzle.category.name)
        if puzzle.word_bank is not None:
            theme_values.setdefault(library_theme_value(puzzle.word_bank.name), puzzle.word_bank.name)
    return {
        "types": [{"value": puzzle_type.slug, "label": puzzle_type.label} for puzzle_type in list_puzzle_types()],
        "tags": [{"value": str(tag.id), "label": tag.label} for tag in tags],
        "themes": [{"value": value, "label": label} for value, label in sorted(theme_values.items(), key=lambda item: item[1].lower())],
        "difficulty": [{"value": value, "label": value.title()} for value in ("easy", "medium", "hard")],
        "creators": [
            {"value": "me", "label": "Created by me"},
            {"value": "unknown", "label": "Unknown creator"},
        ] + [{"value": str(user.id), "label": user.display_name} for user in users],
        "sorts": [
            {"value": "created_desc", "label": "Newest first"},
            {"value": "created_asc", "label": "Oldest first"},
            {"value": "archive_asc", "label": "Archive soonest"},
            {"value": "archive_desc", "label": "Archive latest"},
            {"value": "title_asc", "label": "Title A-Z"},
            {"value": "title_desc", "label": "Title Z-A"},
            {"value": "type_asc", "label": "Puzzle type"},
            {"value": "difficulty_asc", "label": "Difficulty"},
            {"value": "creator_asc", "label": "Creator"},
            {"value": "progress_desc", "label": "Most progress"},
            {"value": "progress_asc", "label": "Least progress"},
        ],
    }


def filtered_library(
    db: Session,
    request: Request,
    current_user: User,
    include_archived_for_admin: bool = True,
) -> dict:
    all_puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at))).all()
    filters = library_filter_state(request, current_user)
    options = library_options(db, all_puzzles, current_user)
    progress_by_puzzle = progress_map_for_user(db, current_user, all_puzzles)
    now = utcnow()
    soon_cutoff = now + timedelta(hours=48)
    created_from = parse_date_boundary(filters["created_from"]) if filters["created_from"] else None
    created_to = parse_date_boundary(filters["created_to"], end_of_day=True) if filters["created_to"] else None
    archive_from = parse_date_boundary(filters["archive_from"]) if filters["archive_from"] else None
    archive_to = parse_date_boundary(filters["archive_to"], end_of_day=True) if filters["archive_to"] else None
    selected_tags = {int(tag_id) for tag_id in filters["tags"] if str(tag_id).isdigit()}

    def matches(puzzle: Puzzle) -> bool:
        if filters["availability"] == "active" and puzzle.archived_at is not None:
            return False
        if filters["availability"] == "archived" and puzzle.archived_at is None:
            return False
        if filters["availability"] == "scheduled" and puzzle.auto_archive_at is None:
            return False
        if not include_archived_for_admin and puzzle.archived_at is not None:
            return False
        if filters["types"] and puzzle.puzzle_type not in filters["types"]:
            return False
        if filters["difficulty"] and puzzle.difficulty not in filters["difficulty"]:
            return False
        if filters["themes"] and not (puzzle_theme_keys(puzzle) & set(filters["themes"])):
            return False
        if selected_tags and not ({tag.id for tag in puzzle.tags} & selected_tags):
            return False
        progress = progress_by_puzzle.get(puzzle.id, {"found": 0, "total": 0, "status": "not_started"})
        status = str(progress.get("status", "not_started"))
        if filters["status"] != "any" and status != filters["status"]:
            return False
        if filters["completed"] == "completed" and status != "completed":
            return False
        if filters["completed"] == "not_completed" and status == "completed":
            return False
        if filters["started"] == "started" and status == "not_started":
            return False
        if filters["started"] == "not_started" and status != "not_started":
            return False
        if created_from and puzzle.created_at < created_from:
            return False
        if created_to and puzzle.created_at > created_to:
            return False
        if archive_from and (puzzle.auto_archive_at is None or puzzle.auto_archive_at < archive_from):
            return False
        if archive_to and (puzzle.auto_archive_at is None or puzzle.auto_archive_at > archive_to):
            return False
        if filters["archive_status"] == "scheduled" and puzzle.auto_archive_at is None:
            return False
        if filters["archive_status"] == "none" and puzzle.auto_archive_at is not None:
            return False
        if filters["archive_status"] == "soon" and not (puzzle.archived_at is None and puzzle.auto_archive_at is not None and now <= puzzle.auto_archive_at <= soon_cutoff):
            return False
        if filters["created_by"] == "me" and puzzle.created_by_id != current_user.id:
            return False
        if filters["created_by"] == "unknown" and puzzle.created_by_id is not None:
            return False
        if filters["created_by"].isdigit() and puzzle.created_by_id != int(filters["created_by"]):
            return False
        if filters["q"] and filters["q"].lower() not in puzzle_search_text(puzzle):
            return False
        return True

    filtered = [puzzle for puzzle in all_puzzles if matches(puzzle)]
    sort = filters["sort"]
    difficulty_rank = {"easy": 1, "medium": 2, "hard": 3}

    def archive_key(puzzle: Puzzle) -> datetime:
        return puzzle.auto_archive_at or datetime.max.replace(tzinfo=timezone.utc)

    sorters = {
        "created_asc": lambda puzzle: (puzzle.created_at, puzzle.id),
        "archive_asc": lambda puzzle: (archive_key(puzzle), puzzle.title.lower()),
        "archive_desc": lambda puzzle: (archive_key(puzzle), puzzle.title.lower()),
        "title_asc": lambda puzzle: (puzzle.title.lower(), puzzle.id),
        "title_desc": lambda puzzle: (puzzle.title.lower(), puzzle.id),
        "type_asc": lambda puzzle: (puzzle.puzzle_type, puzzle.title.lower()),
        "difficulty_asc": lambda puzzle: (difficulty_rank.get(puzzle.difficulty, 99), puzzle.title.lower()),
        "creator_asc": lambda puzzle: (creator_label(puzzle).lower(), puzzle.title.lower()),
        "progress_desc": lambda puzzle: (int(progress_by_puzzle.get(puzzle.id, {}).get("found", 0)), puzzle.created_at),
        "progress_asc": lambda puzzle: (int(progress_by_puzzle.get(puzzle.id, {}).get("found", 0)), puzzle.created_at),
        "created_desc": lambda puzzle: (puzzle.created_at, puzzle.id),
    }
    reverse = sort in {"created_desc", "archive_desc", "title_desc", "progress_desc"}
    filtered.sort(key=sorters.get(sort, sorters["created_desc"]), reverse=reverse)
    limit = filters["limit"]
    visible = filtered[:limit]
    return {
        "puzzles": visible,
        "all_count": len(all_puzzles),
        "result_count": len(filtered),
        "has_more": len(filtered) > limit,
        "next_limit": min(200, limit + 24),
        "filters": filters,
        "options": options,
        "progress_by_puzzle": {puzzle.id: progress_by_puzzle[puzzle.id] for puzzle in visible if puzzle.id in progress_by_puzzle},
        "previews_by_puzzle": {puzzle.id: puzzle_preview(puzzle) for puzzle in visible},
        "labels_by_type": {puzzle_type.slug: puzzle_type.label for puzzle_type in list_puzzle_types()},
    }


def serialize_library_item(puzzle: Puzzle, current_user: User, preview: dict, progress: dict | None, db: Session | None = None) -> dict:
    progress = progress or {"found": 0, "total": progress_response(puzzle, current_user, None).expected_count, "status": "not_started"}
    return {
        "id": puzzle.id,
        "title": puzzle.title,
        "puzzle_type": puzzle.puzzle_type,
        "puzzle_type_label": get_puzzle_type(puzzle.puzzle_type).label if get_puzzle_type(puzzle.puzzle_type) else puzzle.puzzle_type,
        "theme": puzzle.theme,
        "difficulty": puzzle.difficulty,
        "category": puzzle.category.name if puzzle.category else None,
        "tags": [serialize_tag(tag) for tag in puzzle.tags],
        "created_by": {
            "id": puzzle.creator.id if puzzle.creator else None,
            "display_name": creator_label(puzzle),
        },
        "created_at": puzzle.created_at.isoformat() if puzzle.created_at else None,
        "auto_archive_at": puzzle.auto_archive_at.isoformat() if puzzle.auto_archive_at else None,
        "archived_at": puzzle.archived_at.isoformat() if puzzle.archived_at else None,
        "source": puzzle.source,
        "progress": progress,
        "preview": preview,
        "url": puzzle_url(puzzle.id, db),
    }


def serialize_generation_field(field) -> dict:
    validation = {}
    if field.required:
        validation["required"] = True
    if field.minimum is not None:
        validation["minimum"] = field.minimum
    if field.maximum is not None:
        validation["maximum"] = field.maximum
    if field.options:
        validation["allowed_values"] = [option["value"] for option in field.options]
    return {
        "name": field.name,
        "label": field.label,
        "field_type": field.field_type,
        "default": field.default,
        "required": field.required,
        "help_text": field.help_text,
        "options": list(field.options),
        "minimum": field.minimum,
        "maximum": field.maximum,
        "validation": validation,
    }


def validate_generation_options(puzzle_type, options: dict, fields=None) -> dict:
    validated = dict(options or {})
    for field in (fields if fields is not None else puzzle_type.generation_fields):
        raw_value = validated.get(field.name)
        raw_values = raw_value if isinstance(raw_value, list) else ([raw_value] if raw_value not in (None, "") else [])
        values = [str(value).strip() for value in raw_values if str(value).strip()]
        if field.required and not values:
            raise HTTPException(status_code=400, detail=f"{field.label} is required")

        allowed_values = {str(option["value"]) for option in field.options}
        if allowed_values and values:
            invalid_values = [value for value in values if value not in allowed_values]
            if invalid_values:
                raise HTTPException(status_code=400, detail=f"{field.label} has an unsupported value")

        if field.field_type == "number" and values:
            try:
                numeric_value = int(values[0])
            except ValueError as error:
                raise HTTPException(status_code=400, detail=f"{field.label} must be a number") from error
            if field.minimum is not None and numeric_value < field.minimum:
                raise HTTPException(status_code=400, detail=f"{field.label} must be at least {field.minimum}")
            if field.maximum is not None and numeric_value > field.maximum:
                raise HTTPException(status_code=400, detail=f"{field.label} must be no more than {field.maximum}")
    return validated


def serialize_puzzle_type_contract(puzzle_type, enabled: bool = True) -> dict:
    needs_word_bank = "word_bank" in puzzle_type.source_requirements
    return {
        "slug": puzzle_type.slug,
        "label": puzzle_type.label,
        "description": puzzle_type.description,
        "enabled": enabled,
        "supports_print": puzzle_type.supports_print,
        "supports_web_play": puzzle_type.supports_web_play,
        "digital_only": puzzle_type.digital_only,
        "supports_gimmes": puzzle_type.supports_gimmes,
        "gimme_modes": list(puzzle_type.gimme_modes),
        "needs_word_bank": needs_word_bank,
        "theme_mode": puzzle_type.theme_mode,
        "source_requirements": list(puzzle_type.source_requirements),
        "generation_fields": [serialize_generation_field(field) for field in puzzle_type.generation_fields],
        "manual_editor_fields": [serialize_generation_field(field) for field in puzzle_type.manual_editor_fields],
        "manual_editor_script": puzzle_type.manual_editor_script,
        "supports_manual_editor": puzzle_type.manual_builder is not None,
        "print_layout": puzzle_type.print_layout.as_dict(),
        "templates": {
            "play": f"puzzle_types/{puzzle_type.slug}.html",
            "solution": f"puzzle_solutions/{puzzle_type.slug}.html",
            "print": f"puzzle_print/{puzzle_type.slug}.html",
        },
    }


def puzzle_type_generation_schemas(db: Session | None = None, include_disabled: bool = False) -> dict[str, dict]:
    schemas = {}
    disabled = disabled_puzzle_type_slugs(db) if db is not None else set()
    for puzzle_type in list_puzzle_types():
        enabled = puzzle_type.slug not in disabled
        if not enabled and not include_disabled:
            continue
        schemas[puzzle_type.slug] = serialize_puzzle_type_contract(puzzle_type, enabled)
    return schemas


def generation_options_from_form(form) -> dict:
    options = {}
    for key, value in form.multi_items():
        if not key.startswith("gen_"):
            continue
        option_key = key.removeprefix("gen_")
        value = str(value).strip()
        if value:
            if option_key in options:
                if not isinstance(options[option_key], list):
                    options[option_key] = [options[option_key]]
                options[option_key].append(value)
            else:
                options[option_key] = value
    return options


def prefixed_options_from_form(form, prefix: str) -> dict:
    options = {}
    for key, value in form.multi_items():
        if not key.startswith(prefix):
            continue
        option_key = key.removeprefix(prefix)
        value = str(value).strip()
        if value:
            if option_key in options:
                if not isinstance(options[option_key], list):
                    options[option_key] = [options[option_key]]
                options[option_key].append(value)
            else:
                options[option_key] = value
    return options


def build_manual_payload_from_helper(
    db: Session,
    puzzle_type_slug: str,
    title: str,
    theme: str,
    difficulty: str,
    draft: str,
    form,
) -> PuzzleManualWrite:
    puzzle_type = get_puzzle_type(puzzle_type_slug)
    if puzzle_type is None or not is_puzzle_type_enabled(db, puzzle_type_slug):
        raise ValueError("Unsupported puzzle type")
    if puzzle_type.manual_builder is None:
        raise ValueError("This puzzle type does not have a GUI editor helper yet")
    try:
        manual_options = validate_generation_options(
            puzzle_type,
            prefixed_options_from_form(form, "manual_"),
            fields=puzzle_type.manual_editor_fields,
        )
    except HTTPException as error:
        raise ValueError(str(error.detail)) from error
    manual_options["_theme"] = theme.strip() or "manual"
    manual_options["_difficulty"] = difficulty.strip() or "manual"
    generated = puzzle_type.manual_builder(manual_options)
    metadata = dict(generated.metadata or {})
    metadata["manual_editor"] = True
    metadata["manual_editor_options"] = manual_options
    return PuzzleManualWrite(
        puzzle_type=puzzle_type_slug,
        title=(title.strip() or generated.title)[:160],
        theme=(theme.strip() or "manual")[:80],
        difficulty=(difficulty.strip() or "manual")[:40],
        prompt=generated.prompt,
        print_payload=generated.print_payload,
        play_payload=generated.play_payload,
        solution_payload=generated.solution_payload,
        metadata=metadata,
        hints=generated.hints,
        draft=draft == "true",
    )


def printable_gimme_cells(puzzle: Puzzle) -> dict[str, str]:
    print_payload = puzzle.print_payload if isinstance(puzzle.print_payload, dict) else {}
    solution_payload = puzzle.solution_payload if isinstance(puzzle.solution_payload, dict) else {}
    gimmes = print_payload.get("gimmes") if isinstance(print_payload.get("gimmes"), dict) else solution_payload.get("gimmes", {})
    if not isinstance(gimmes, dict):
        return {}

    if print_payload.get("kind") == "crossword":
        cells = gimmes.get("cells", {})
        if not isinstance(cells, dict):
            return {}
        return {str(key): str(value).upper()[:1] for key, value in cells.items() if str(value).strip()}

    if print_payload.get("kind") == "word_search":
        gimme_words = {str(word).upper() for word in gimmes.get("words", []) if str(word).strip()}
        cells = {}
        for placement in solution_payload.get("placements", []):
            word = str(placement.get("word", "")).upper()
            if word not in gimme_words:
                continue
            start = placement.get("start", [])
            direction = placement.get("direction", [])
            if len(start) != 2 or len(direction) != 2:
                continue
            x, y = int(start[0]), int(start[1])
            dx, dy = int(direction[0]), int(direction[1])
            for index, letter in enumerate(word):
                cells[f"{x + dx * index},{y + dy * index}"] = letter
        return cells

    return {}


def printable_gimme_words(puzzle: Puzzle) -> list[str]:
    print_payload = puzzle.print_payload if isinstance(puzzle.print_payload, dict) else {}
    solution_payload = puzzle.solution_payload if isinstance(puzzle.solution_payload, dict) else {}
    gimmes = print_payload.get("gimmes") if isinstance(print_payload.get("gimmes"), dict) else solution_payload.get("gimmes", {})
    if not isinstance(gimmes, dict):
        return []
    return sorted({str(word).upper() for word in gimmes.get("words", []) if str(word).strip()})


def serialize_tag(tag: PuzzleTag) -> dict:
    return {
        "id": tag.id,
        "type": tag.tag_type,
        "name": tag.name,
        "slug": tag.slug,
        "label": tag.label,
    }


def serialize_definition(definition: DictionaryDefinition) -> dict:
    return {
        "id": definition.id,
        "definition": definition.definition,
        "part_of_speech": definition.part_of_speech,
        "clues": tag_values(definition.tags, "Clue"),
        "example": definition.example,
        "source": definition.source,
        "position": definition.position,
        "properties": definition.properties or {},
        "archived": definition.archived_at is not None,
        "archived_at": definition.archived_at.isoformat() if definition.archived_at else None,
        "tags": [serialize_tag(tag) for tag in definition.tags],
    }


def serialize_dictionary_word(word: DictionaryWord) -> dict:
    return {
        "id": word.id,
        "word": word.word,
        "display_text": word.display_text,
        "clue": word.clue,
        "part_of_speech": word.part_of_speech,
        "properties": word.properties or {},
        "archived": word.archived_at is not None,
        "banned": word.banned_at is not None,
        "tags": [serialize_tag(tag) for tag in word.tags],
        "definitions": [serialize_definition(definition) for definition in word.definitions],
    }


def dictionary_export_payload(words: list[DictionaryWord]) -> dict:
    return {
        "schema": "puzzary.dictionary.v1",
        "exported_at": utcnow().isoformat(),
        "total": len(words),
        "items": [serialize_dictionary_word(word) for word in words],
    }


def serialize_word_bank_link(link: WordBankWord) -> dict:
    return {
        "id": link.id,
        "word_id": link.word_id,
        "definition_id": link.definition_id,
        "position": link.position,
        "word": serialize_dictionary_word(link.word) if link.word is not None else None,
        "definition": serialize_definition(link.definition) if link.definition is not None else None,
    }


def serialize_word_bank(bank: WordBank, include_words: bool = False) -> dict:
    payload = bank.payload if isinstance(bank.payload, dict) else {}
    data = {
        "id": bank.id,
        "name": bank.name,
        "slug": bank.slug,
        "description": bank.description,
        "mode": word_bank_mode(bank),
        "category_id": bank.category_id,
        "category": bank.category.name if bank.category else None,
        "tags": [serialize_tag(tag) for tag in bank.tags],
        "properties": payload.get("properties", {}),
        "archived": bank.archived_at is not None,
        "archived_at": bank.archived_at.isoformat() if bank.archived_at else None,
        "created_at": bank.created_at.isoformat() if bank.created_at else None,
    }
    if include_words:
        data["entries"] = [serialize_word_bank_link(link) for link in sorted(bank.word_links, key=lambda item: item.position)]
    else:
        data["entry_count"] = len(bank.word_links)
    return data


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or uuid.uuid4().hex[:8]


def unique_slug(db: Session, model: type, value: str, ignore_id: int | None = None) -> str:
    base_slug = slugify(value)
    slug = base_slug
    suffix = 2
    while True:
        existing = db.scalar(select(model).where(model.slug == slug))
        if existing is None or existing.id == ignore_id:
            return slug
        slug = f"{base_slug}_{suffix}"
        suffix += 1


def parse_words(value: str) -> list[str]:
    words = []
    for part in re.split(r"[\n,;]+", value):
        word = re.sub(r"[^A-Za-z0-9]+", "", part).upper()
        if word and word not in words:
            words.append(word)
    return words


def normalize_word(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", value).upper()


def normalize_dictionary_word(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", value).lower()


def normalize_definition_signature(definition: str, part_of_speech: str = "") -> str:
    normalized_definition = re.sub(r"\s+", " ", definition.strip().lower())
    normalized_part = re.sub(r"\s+", " ", part_of_speech.strip().lower())
    return f"{normalized_part}|{normalized_definition}"


def import_dictionary_words(
    db: Session,
    imported_words: list[ImportedWord],
    source_label: str = "",
    tag_ids: list[int] | None = None,
    dry_run: bool = True,
    max_items: int = 500,
) -> dict:
    import_label = source_label.strip() or "External Import"
    imported_at = datetime.now(timezone.utc).isoformat()
    selected_tags = tags_for_ids(db, tag_ids or [])
    source_tag = get_or_create_tag(db, "Source", import_label) if not dry_run else None
    if source_tag is not None and all(tag.id != source_tag.id for tag in selected_tags):
        selected_tags.append(source_tag)

    result = {
        "dry_run": dry_run,
        "source_label": import_label,
        "fetched_words": len(imported_words),
        "processed_words": 0,
        "added_words": 0,
        "merged_words": 0,
        "skipped_words": 0,
        "added_definitions": 0,
        "skipped_duplicate_definitions": 0,
        "sample_words": [],
        "errors": [],
    }

    for imported in imported_words[: max(1, min(max_items, 5000))]:
        word_key = normalize_dictionary_word(imported.word)
        if not word_key:
            result["skipped_words"] += 1
            continue
        result["processed_words"] += 1
        existing = db.scalar(select(DictionaryWord).where(DictionaryWord.word == word_key))
        word_exists = existing is not None
        if not dry_run and existing is None:
            existing = DictionaryWord(
                word=word_key,
                display_text=imported.display_text.strip() or imported.word.strip(),
                clue=imported.clue.strip(),
                part_of_speech=imported.part_of_speech.strip(),
                properties={**(imported.properties or {}), "import_source": import_label, "imported_at": imported_at},
            )
            existing.tags = list(selected_tags)
            db.add(existing)
            db.flush()
        elif not dry_run and existing is not None:
            if not existing.display_text and imported.display_text.strip():
                existing.display_text = imported.display_text.strip()
            if not existing.clue and imported.clue.strip():
                existing.clue = imported.clue.strip()
            if not existing.part_of_speech and imported.part_of_speech.strip():
                existing.part_of_speech = imported.part_of_speech.strip()
            existing_tag_ids = {tag.id for tag in existing.tags}
            for tag in selected_tags:
                if tag.id not in existing_tag_ids:
                    existing.tags.append(tag)
                    existing_tag_ids.add(tag.id)

        if word_exists:
            result["merged_words"] += 1
        else:
            result["added_words"] += 1
        if len(result["sample_words"]) < 10:
            result["sample_words"].append(
                {
                    "word": imported.display_text or imported.word,
                    "action": "merge" if word_exists else "add",
                    "definitions": len(imported.definitions),
                }
            )

        existing_signatures = set()
        if existing is not None:
            existing_signatures = {
                normalize_definition_signature(definition.definition, definition.part_of_speech)
                for definition in existing.definitions
            }
        seen_import_signatures: set[str] = set()
        for index, imported_definition in enumerate(imported.definitions, start=1):
            signature = normalize_definition_signature(
                imported_definition.definition,
                imported_definition.part_of_speech or imported.part_of_speech,
            )
            if not signature.strip("|") or signature in existing_signatures or signature in seen_import_signatures:
                result["skipped_duplicate_definitions"] += 1
                continue
            seen_import_signatures.add(signature)
            result["added_definitions"] += 1
            if dry_run or existing is None:
                continue
            definition = DictionaryDefinition(
                word=existing,
                definition=imported_definition.definition.strip(),
                part_of_speech=(imported_definition.part_of_speech or imported.part_of_speech).strip(),
                example=imported_definition.example.strip(),
                source=import_label[:120],
                position=max((item.position for item in existing.definitions), default=0) + index,
                properties={
                    **(imported_definition.properties or {}),
                    "import_source": import_label,
                    "imported_at": imported_at,
                },
            )
            definition.tags = definition_tags_from_form(
                db,
                [tag.id for tag in selected_tags],
                definition.part_of_speech,
                "\n".join(imported_definition.clues),
            )
            for tag_name in imported_definition.tags:
                tag = get_or_create_tag(db, "Imported", tag_name)
                if tag is not None and all(existing_tag.id != tag.id for existing_tag in definition.tags):
                    definition.tags.append(tag)
            db.add(definition)

    if not dry_run:
        db.commit()
    return result


def normalize_seed(value: str | None) -> str:
    if value and value.strip():
        return value.strip()
    return uuid.uuid4().hex[:12]


def word_bank_words(bank: WordBank | None) -> list[str]:
    if bank is None:
        return []
    linked_words = [link.word.word for link in sorted(bank.word_links, key=lambda item: item.position)]
    if linked_words:
        return linked_words
    words = bank.payload.get("words", [])
    if not isinstance(words, list):
        return []
    return [str(word).upper() for word in words if str(word).strip()]


def word_bank_mode(bank: WordBank | None) -> str:
    if bank is None or not isinstance(bank.payload, dict):
        return "manual"
    return str(bank.payload.get("mode") or "manual")


def definition_clue(definition: DictionaryDefinition | None) -> str:
    if definition is None:
        return ""
    return tag_value(definition.tags, "Clue") or definition.definition


templates.env.globals["word_bank_mode"] = word_bank_mode
templates.env.globals["definition_clue"] = definition_clue


def word_bank_entries(db: Session, bank: WordBank | None, excluded_tag_ids: set[int] | None = None) -> list[dict]:
    if bank is None:
        return []
    excluded_tag_ids = excluded_tag_ids or set()
    entries: list[dict] = []
    if word_bank_mode(bank) == "automatic":
        tag_ids = {tag.id for tag in bank.tags}
        if not tag_ids:
            return []
        words = db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all()
        for word in words:
            if word.archived_at is not None or word.banned_at is not None:
                continue
            word_tag_ids = {tag.id for tag in word.tags}
            definitions = sorted(
                [definition for definition in word.definitions if definition.archived_at is None],
                key=lambda item: item.position,
            )
            matched_definitions = [
                definition
                for definition in definitions
                if tag_ids & ({tag.id for tag in definition.tags} | word_tag_ids)
            ]
            if not matched_definitions and tag_ids & word_tag_ids:
                matched_definitions = [None]
            for definition in matched_definitions:
                if definition is not None and excluded_tag_ids & {tag.id for tag in definition.tags}:
                    continue
                if excluded_tag_ids & word_tag_ids:
                    continue
                entries.append({"word": word, "definition": definition})
        return entries

    for link in sorted(bank.word_links, key=lambda item: item.position):
        word = link.word
        if word.archived_at is not None or word.banned_at is not None:
            continue
        if excluded_tag_ids & {tag.id for tag in word.tags}:
            continue
        definition = link.definition
        if definition is not None and definition.archived_at is not None:
            continue
        if definition is not None and excluded_tag_ids & {tag.id for tag in definition.tags}:
            continue
        entries.append({"word": word, "definition": definition})
    return entries


def get_or_create_word(db: Session, value: str) -> DictionaryWord | None:
    word = normalize_word(value)
    if not word:
        return None
    existing = db.scalar(select(DictionaryWord).where(DictionaryWord.word == word))
    if existing is not None:
        return existing
    entry = DictionaryWord(word=word, display_text=word)
    db.add(entry)
    db.flush()
    return entry


def sync_word_bank_words(db: Session, bank: WordBank, words: list[str]) -> None:
    bank.word_links.clear()
    db.flush()
    for position, word_value in enumerate(words):
        entry = get_or_create_word(db, word_value)
        if entry is not None:
            bank.word_links.append(WordBankWord(word=entry, position=position))


def add_word_bank_link(db: Session, bank: WordBank, word: DictionaryWord, definition: DictionaryDefinition | None = None) -> None:
    if any(link.word_id == word.id for link in bank.word_links):
        return
    next_position = max((link.position for link in bank.word_links), default=-1) + 1
    bank.word_links.append(WordBankWord(word=word, definition=definition, position=next_position))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_local_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Archive date/time is not valid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=app_timezone())
    return parsed.astimezone(timezone.utc)


def apply_due_auto_archives(db: Session) -> int:
    now = utcnow()
    due_puzzles = db.scalars(
        select(Puzzle).where(
            Puzzle.archived_at.is_(None),
            Puzzle.auto_archive_at.is_not(None),
            Puzzle.auto_archive_at <= now,
        )
    ).all()
    for puzzle in due_puzzles:
        puzzle.archived_at = now
    if due_puzzles:
        db.commit()
    return len(due_puzzles)


def dictionary_import_source_summary(source: DictionaryImportSource) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "source_url": source.source_url,
        "source_type": source.source_type,
        "source_label": source.source_label,
        "sync_interval_hours": source.sync_interval_hours,
        "max_items": source.max_items,
        "enabled": source.enabled,
        "has_api_token": bool(source.source_api_token),
        "last_synced_at": source.last_synced_at.isoformat() if source.last_synced_at else None,
        "next_sync_at": source.next_sync_at.isoformat() if source.next_sync_at else None,
        "last_result": source.last_result or {},
        "created_at": source.created_at.isoformat() if source.created_at else None,
    }


def schedule_next_dictionary_import(source: DictionaryImportSource, now: datetime | None = None) -> None:
    base = now or utcnow()
    interval = max(1, min(int(source.sync_interval_hours or 24), 24 * 30))
    source.next_sync_at = base + timedelta(hours=interval)


def run_dictionary_import_source(db: Session, source: DictionaryImportSource, dry_run: bool = False) -> dict:
    now = utcnow()
    try:
        sources = fetch_dictionary_source(source.source_url, source.source_type, source.source_api_token)
        imported_words = parse_imported_words(sources)
        result = import_dictionary_words(
            db,
            imported_words,
            source_label=source.source_label or source.name or source.source_url,
            tag_ids=[],
            dry_run=dry_run,
            max_items=source.max_items,
        )
        result["source_count"] = len(sources)
        result["source_url"] = source.source_url
        result["source_type"] = source.source_type
        result["synced_at"] = now.isoformat()
    except ValueError as error:
        result = {
            "dry_run": dry_run,
            "source_url": source.source_url,
            "source_type": source.source_type,
            "errors": [str(error)],
            "synced_at": now.isoformat(),
        }
    source.last_synced_at = now
    source.last_result = result
    schedule_next_dictionary_import(source, now)
    db.commit()
    return result


def run_due_dictionary_import_sources(db: Session) -> int:
    now = utcnow()
    sources = db.scalars(
        select(DictionaryImportSource).where(
            DictionaryImportSource.enabled == True,  # noqa: E712
            (DictionaryImportSource.next_sync_at.is_(None)) | (DictionaryImportSource.next_sync_at <= now),
        )
    ).all()
    for source in sources:
        run_dictionary_import_source(db, source, dry_run=False)
    return len(sources)


async def auto_archive_loop() -> None:
    while True:
        with SessionLocal() as db:
            apply_due_auto_archives(db)
        await asyncio.sleep(60)


async def dictionary_import_source_loop() -> None:
    while True:
        with SessionLocal() as db:
            run_due_dictionary_import_sources(db)
        await asyncio.sleep(DICTIONARY_SYNC_CHECK_SECONDS)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_api_token() -> str:
    return f"pzy_{secrets.token_urlsafe(32)}"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS)
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${derived.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        scheme, iterations_text, salt, digest = stored_hash.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations_text))
        return hmac.compare_digest(derived.hex(), digest)
    except (TypeError, ValueError):
        return False


def password_error(password: str) -> str:
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() == password or password.upper() == password:
        return "Use a mix of uppercase and lowercase letters."
    if not any(char.isdigit() for char in password):
        return "Add at least one number."
    return ""


def issue_password_setup_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    user.reset_token_hash = hash_secret(token)
    user.reset_token_expires_at = utcnow() + timedelta(hours=RESET_TOKEN_TTL_HOURS)
    user.must_change_password = True
    user.session_version = (user.session_version or 1) + 1
    db.commit()
    return token


def issue_api_token(db: Session, user: User) -> str:
    token = new_api_token()
    user.api_token_hash = hash_secret(token)
    user.api_token_prefix = token[:10]
    user.api_token_last_used_at = None
    db.commit()
    return token


def api_token_from_request(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return request.headers.get("x-puzzary-api-token", "").strip()


def setup_url(token: str, db: Session | None = None) -> str:
    return f"{app_base_url(db)}/password/setup?token={token}"


def write_bootstrap_setup_link(user: User, token: str) -> None:
    path = UPLOAD_DIR.parent / "bootstrap-admin-setup.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Puzzary initial admin setup\n"
        f"Account: {user.display_name}\n"
        f"Setup link: {setup_url(token)}\n"
        f"Expires: {user.reset_token_expires_at}\n",
        encoding="utf-8",
    )


def login_rate_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.strip().lower()}"


def login_limited(request: Request, username: str) -> bool:
    key = login_rate_key(request, username)
    now = time.time()
    attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(key, []) if now - stamp < LOGIN_RATE_LIMIT_WINDOW]
    LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) >= LOGIN_RATE_LIMIT_MAX


def record_login_failure(request: Request, username: str) -> None:
    key = login_rate_key(request, username)
    LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


def clear_login_failures(request: Request, username: str) -> None:
    LOGIN_ATTEMPTS.pop(login_rate_key(request, username), None)


def find_login_user(db: Session, username: str) -> User | None:
    normalized = username.strip()
    if not normalized:
        return None
    email = normalized.lower()
    return db.scalar(
        select(User).where(
            (User.display_name == normalized) | (User.email == email)
        )
    )


def api_user_from_token(request: Request, db: Session) -> User | None:
    token = api_token_from_request(request)
    if not token:
        return None
    user = db.scalar(select(User).where(User.api_token_hash == hash_secret(token), User.is_active == True))  # noqa: E712
    if user is not None:
        user.api_token_last_used_at = utcnow()
        db.commit()
    return user


def require_api_user(request: Request, db: Session) -> User:
    user = api_user_from_token(request, db) or current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="API token required")
    return user


def active_admin_count(db: Session, excluding_user_id: int | None = None) -> int:
    users = db.scalars(select(User).where(User.is_admin == True, User.is_active == True)).all()  # noqa: E712
    return sum(1 for user in users if user.id != excluding_user_id)


def can_remove_admin(db: Session, user: User) -> bool:
    if not user.is_admin or not user.is_active:
        return True
    return active_admin_count(db, excluding_user_id=user.id) >= 1


def normalized_email(value: str) -> str | None:
    email = value.strip().lower()
    return email or None


def create_user_record(
    db: Session,
    display_name: str,
    email: str = "",
    is_admin: bool = False,
    password: str = "",
) -> tuple[User, str | None]:
    clean_name = display_name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Display name is required")
    if db.scalar(select(User).where(User.display_name == clean_name)):
        raise HTTPException(status_code=400, detail="That display name is already in use")
    clean_email = normalized_email(email)
    if clean_email and db.scalar(select(User).where(User.email == clean_email)):
        raise HTTPException(status_code=400, detail="That email is already in use")
    user = User(display_name=clean_name, email=clean_email, is_admin=is_admin, is_active=True)
    db.add(user)
    db.flush()
    setup_token = None
    if password.strip():
        error = password_error(password)
        if error:
            raise HTTPException(status_code=400, detail=error)
        user.password_hash = hash_password(password)
        user.must_change_password = True
    else:
        setup_token = issue_password_setup_token(db, user)
        db.refresh(user)
        return user, setup_token
    db.commit()
    db.refresh(user)
    return user, setup_token


def ensure_default_admin_account(db: Session) -> User:
    admin = db.scalar(select(User).where(User.display_name == "admin"))
    if admin is None:
        admin = User(display_name="admin", is_admin=True, is_active=True, password_hash=hash_password("admin"))
        db.add(admin)
        db.commit()
        db.refresh(admin)
        return admin
    changed = False
    if not admin.is_admin:
        admin.is_admin = True
        changed = True
    if not admin.is_active:
        admin.is_active = True
        changed = True
    if not admin.password_hash:
        admin.password_hash = hash_password("admin")
        admin.must_change_password = False
        admin.reset_token_hash = None
        admin.reset_token_expires_at = None
        changed = True
    if changed:
        db.commit()
        db.refresh(admin)
    return admin


def ensure_admin_user(db: Session) -> None:
    ensure_default_admin_account(db)
    has_admin = db.scalar(select(User).where(User.is_admin == True))  # noqa: E712
    first_user = db.scalar(select(User).order_by(User.created_at, User.id))
    if first_user is None:
        first_user = User(display_name=os.getenv("PUZZARY_BOOTSTRAP_ADMIN_NAME", "admin"), is_admin=True, is_active=True)
        db.add(first_user)
        db.flush()
    if has_admin is None:
        first_user.is_admin = True
        first_user.is_active = True
        admin_user = first_user
    else:
        admin_user = has_admin
    token_missing_or_expired = (
        not admin_user.reset_token_hash
        or not admin_user.reset_token_expires_at
        or admin_user.reset_token_expires_at <= utcnow()
    ) if admin_user is not None else False
    if admin_user is not None and admin_user.is_admin and not admin_user.password_hash and token_missing_or_expired:
        token = issue_password_setup_token(db, admin_user)
        write_bootstrap_setup_link(admin_user, token)
        return
    db.commit()


def seed_word_banks(db: Session) -> None:
    existing = set(db.scalars(select(WordBank.slug)).all())
    dictionaries = db.scalars(select(PuzzleDictionary).order_by(PuzzleDictionary.name)).all()
    for dictionary in dictionaries:
        if dictionary.slug in existing:
            continue
        db.add(
            bank := WordBank(
                name=dictionary.name,
                slug=dictionary.slug,
                description=dictionary.description,
                payload={"words": dictionary.words},
            )
        )
        sync_word_bank_words(db, bank, dictionary.words)
        existing.add(dictionary.slug)

    for slug, words in THEMES.items():
        if slug in existing:
            continue
        db.add(
            bank := WordBank(
                name=slug.replace("_", " ").title(),
                slug=slug,
                description="Seeded from the original Puzzary word list.",
                payload={"words": words},
            )
        )
        sync_word_bank_words(db, bank, words)
        existing.add(slug)
    for bank in db.scalars(select(WordBank)).all():
        if not bank.word_links:
            sync_word_bank_words(db, bank, word_bank_words(bank))
    db.commit()


def seed_word_definitions(db: Session) -> None:
    changed = False
    for word in db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all():
        if word.clue.strip() and not word.definitions:
            definition = DictionaryDefinition(
                definition=word.clue.strip(),
                part_of_speech=word.part_of_speech,
                position=0,
                source="legacy_clue",
            )
            definition.tags = definition_tags_from_form(db, [], word.part_of_speech, word.clue)
            word.definitions.append(definition)
            changed = True
    if changed:
        db.commit()


def ensure_definition_from_clue(db: Session, word: DictionaryWord) -> None:
    if word.clue.strip() and not word.definitions:
        definition = DictionaryDefinition(
            definition=word.clue.strip(),
            part_of_speech=word.part_of_speech,
            position=0,
            source="word_clue",
        )
        definition.tags = definition_tags_from_form(db, [], word.part_of_speech, word.clue)
        word.definitions.append(definition)


def word_bank_options(db: Session, include_archived: bool = False) -> list[WordBank]:
    statement = select(WordBank)
    if not include_archived:
        statement = statement.where(WordBank.archived_at.is_(None))
    return db.scalars(statement.order_by(WordBank.name)).all()


def word_bank_for_slug(db: Session, slug: str) -> WordBank | None:
    return db.scalar(select(WordBank).where(WordBank.slug == slug, WordBank.archived_at.is_(None)))


def word_banks_for_slugs(db: Session, slugs: list[str]) -> list[WordBank]:
    normalized = [slug.strip() for slug in slugs if slug.strip()]
    if not normalized:
        return []
    if "__all__" in normalized:
        return word_bank_options(db)
    banks = db.scalars(
        select(WordBank).where(WordBank.slug.in_(normalized), WordBank.archived_at.is_(None))
    ).all()
    by_slug = {bank.slug: bank for bank in banks}
    return [by_slug[slug] for slug in normalized if slug in by_slug]


def word_banks_for_ids(db: Session, bank_ids: list[int]) -> list[WordBank]:
    cleaned = [int(bank_id) for bank_id in bank_ids if int(bank_id) >= 0]
    if not cleaned:
        return []
    if 0 in cleaned:
        return word_bank_options(db)
    banks = db.scalars(
        select(WordBank).where(WordBank.id.in_(cleaned), WordBank.archived_at.is_(None))
    ).all()
    by_id = {bank.id: bank for bank in banks}
    return [by_id[bank_id] for bank_id in cleaned if bank_id in by_id]


def combined_word_bank_entries(db: Session, banks: list[WordBank], excluded_tag_ids: set[int] | None = None) -> list[dict]:
    entries = []
    seen = set()
    for bank in banks:
        for entry in word_bank_entries(db, bank, excluded_tag_ids):
            word = entry.get("word") if isinstance(entry, dict) else None
            definition = entry.get("definition") if isinstance(entry, dict) else None
            key = (getattr(word, "id", None), getattr(definition, "id", None) if definition is not None else None)
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return entries


def word_bank_source_theme(banks: list[WordBank], fallback: str = "") -> str:
    if len(banks) == 1:
        return banks[0].slug
    if len(banks) > 1:
        return "all_word_banks"
    return fallback


def tags_for_ids(db: Session, tag_ids: list[int]) -> list[PuzzleTag]:
    if not tag_ids:
        return []
    return db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all()


def normalize_tag_type(value: str) -> str:
    return value.strip() or "General"


def typed_tag_slug(tag_type: str, name: str) -> str:
    normalized_type = normalize_tag_type(tag_type)
    if normalized_type == "General":
        return slugify(name)
    return slugify(f"{normalized_type} {name}")


def get_or_create_tag(db: Session, tag_type: str, name: str) -> PuzzleTag | None:
    tag_name = name.strip()
    if not tag_name:
        return None
    normalized_type = normalize_tag_type(tag_type)
    existing = db.scalar(
        select(PuzzleTag).where(
            PuzzleTag.tag_type == normalized_type,
            PuzzleTag.name == tag_name,
        )
    )
    if existing is not None:
        return existing
    tag = PuzzleTag(
        name=tag_name,
        tag_type=normalized_type,
        slug=unique_slug(db, PuzzleTag, typed_tag_slug(normalized_type, tag_name)),
    )
    db.add(tag)
    db.flush()
    return tag


def definition_tags_from_form(
    db: Session,
    tag_ids: list[int],
    part_of_speech_tag: str = "",
    clue_tag: str = "",
) -> list[PuzzleTag]:
    tags = tags_for_ids(db, tag_ids)
    existing_ids = {tag.id for tag in tags}
    for tag_type, value in (("Part of Speech", part_of_speech_tag),):
        tag = get_or_create_tag(db, tag_type, value)
        if tag is not None and tag.id not in existing_ids:
            tags.append(tag)
            existing_ids.add(tag.id)
    for value in [line.strip() for line in clue_tag.splitlines() if line.strip()]:
        tag = get_or_create_tag(db, "Clue", value)
        if tag is not None and tag.id not in existing_ids:
            tags.append(tag)
            existing_ids.add(tag.id)
    return tags


def blocked_words_for_generation(db: Session, excluded_tag_ids: set[int]) -> list[str]:
    blocked = []
    for word in db.scalars(select(DictionaryWord)).all():
        has_excluded_tag = excluded_tag_ids and any(tag.id in excluded_tag_ids for tag in word.tags)
        if word.banned_at is not None or has_excluded_tag:
            blocked.append(word.word)
    return blocked


def require_admin_user(request: Request, db: Session) -> User | RedirectResponse:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_admin_api(request: Request, db: Session) -> User:
    user = require_api_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, int(user_id))
    if user is None or not user.is_active:
        request.session.clear()
        return None
    if int(request.session.get("session_version", 0)) != int(user.session_version or 1):
        request.session.clear()
        return None
    return user


def require_web_user(request: Request, db: Session) -> User | RedirectResponse:
    user = current_user(request, db)
    if user is None:
        target = safe_return_to(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(url=f"/login?return_to={quote(target)}", status_code=303)
    return user


def found_words_for_progress(progress: PuzzleProgress | None) -> list[str]:
    if progress is None:
        return []
    words = progress.progress_payload.get("found_words", [])
    if not isinstance(words, list):
        return []
    return [str(word).upper() for word in words]


def progress_response(puzzle: Puzzle, user: User, progress: PuzzleProgress | None) -> PuzzleProgressResponse:
    progress_payload = progress.progress_payload if progress is not None else {}
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None:
        return PuzzleProgressResponse(
            puzzle_id=puzzle.id,
            user_id=user.id,
            found_words=[],
            answers={},
            expected_count=0,
            found_count=0,
        )
    found_count, expected_count = puzzle_type.progress_counter(progress_payload, puzzle.solution_payload)
    return PuzzleProgressResponse(
        puzzle_id=puzzle.id,
        user_id=user.id,
        found_words=progress_payload.get("found_words", []) if isinstance(progress_payload.get("found_words", []), list) else [],
        answers=progress_payload.get("answers", {}) if isinstance(progress_payload.get("answers", {}), dict) else {},
        expected_count=expected_count,
        found_count=found_count,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "puzzary"}


def qr_svg_bytes(value: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=1, box_size=8)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage, attrib={"class": "puzzary-qr"})
    buffer = BytesIO()
    image.save(buffer)
    return buffer.getvalue()


@app.get("/puzzles/{puzzle_id}/qr.svg")
def puzzle_qr_svg(puzzle_id: str, db: Session = Depends(get_db)) -> Response:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return Response(
        content=qr_svg_bytes(puzzle_qr_short_url(puzzle.id, db)),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/q/{puzzle_id}")
def puzzle_qr_redirect(puzzle_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    metadata = dict(puzzle.puzzle_metadata or {})
    qr_stats = metadata.get("qr")
    if not isinstance(qr_stats, dict):
        qr_stats = {}
    qr_stats["scan_count"] = int(qr_stats.get("scan_count") or 0) + 1
    qr_stats["last_scanned_at"] = utcnow().isoformat()
    metadata["qr"] = qr_stats
    puzzle.puzzle_metadata = metadata
    db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}?source=qr", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return_to = safe_return_to(request.query_params.get("return_to", ""))
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "current_user": current_user(request, db),
            "error": request.query_params.get("error", ""),
            "message": request.query_params.get("message", ""),
            "return_to": return_to,
        },
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    return_to: str = Form("/library"),
    db: Session = Depends(get_db),
):
    safe_target = safe_return_to(return_to)
    login_error_suffix = f"&return_to={quote(safe_target)}" if safe_target != "/library" else ""
    if login_limited(request, username):
        return RedirectResponse(url=f"/login?error=Too%20many%20attempts.%20Try%20again%20soon.{login_error_suffix}", status_code=303)
    user = find_login_user(db, username)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        record_login_failure(request, username)
        return RedirectResponse(url=f"/login?error=Invalid%20username%20or%20password.{login_error_suffix}", status_code=303)

    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version or 1
    user.last_login_at = utcnow()
    db.commit()
    clear_login_failures(request, username)
    if user.must_change_password:
        return RedirectResponse(url="/account/password?message=Please%20set%20a%20new%20password.", status_code=303)
    return RedirectResponse(url=safe_target, status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/password/setup", response_class=HTMLResponse)
def password_setup_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    token_hash = hash_secret(token)
    user = db.scalar(select(User).where(User.reset_token_hash == token_hash))
    token_valid = bool(user and user.reset_token_expires_at and user.reset_token_expires_at > utcnow())
    return templates.TemplateResponse(
        "password_setup.html",
        {
            "request": request,
            "current_user": current_user(request, db),
            "token": token,
            "token_valid": token_valid,
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/password/setup")
def password_setup(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    token_hash = hash_secret(token)
    user = db.scalar(select(User).where(User.reset_token_hash == token_hash))
    if user is None or not user.reset_token_expires_at or user.reset_token_expires_at <= utcnow():
        return RedirectResponse(url="/password/setup?error=That%20setup%20link%20is%20invalid%20or%20expired.", status_code=303)
    if password != confirm_password:
        return RedirectResponse(url=f"/password/setup?token={quote(token)}&error=Passwords%20do%20not%20match.", status_code=303)
    error = password_error(password)
    if error:
        return RedirectResponse(url=f"/password/setup?token={quote(token)}&error={quote(error)}", status_code=303)
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    user.is_active = True
    user.session_version = (user.session_version or 1) + 1
    user.last_login_at = utcnow()
    db.commit()
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version
    return RedirectResponse(url="/library", status_code=303)


@app.get("/account/password", response_class=HTMLResponse)
def account_password_page(request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "account_password.html",
        {
            "request": request,
            "current_user": user,
            "error": request.query_params.get("error", ""),
            "message": request.query_params.get("message", ""),
        },
    )


@app.post("/account/password")
def account_password_update(
    request: Request,
    current_password: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.password_hash and not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/account/password?error=Current%20password%20is%20not%20correct.", status_code=303)
    if password != confirm_password:
        return RedirectResponse(url="/account/password?error=Passwords%20do%20not%20match.", status_code=303)
    error = password_error(password)
    if error:
        return RedirectResponse(url=f"/account/password?error={quote(error)}", status_code=303)
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.session_version = (user.session_version or 1) + 1
    db.commit()
    request.session["session_version"] = user.session_version
    return RedirectResponse(url="/account/password?message=Password%20updated.", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return library(request, db)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    library_data = filtered_library(db, request, user, include_archived_for_admin=user.is_admin)
    active_chips = active_filter_chips(library_data["filters"], library_data["options"])
    for chip in active_chips:
        chip["remove_url"] = query_without(request, chip["param"], chip["value"])
    next_pairs = list(request.query_params.multi_items())
    next_pairs = [(key, value) for key, value in next_pairs if key != "limit"]
    next_pairs.append(("limit", str(library_data["next_limit"])))
    next_url = f"/library?{urlencode(next_pairs, doseq=True)}"
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "puzzles": library_data["puzzles"],
            "current_user": user,
            "progress_by_puzzle": library_data["progress_by_puzzle"],
            "previews_by_puzzle": library_data["previews_by_puzzle"],
            "labels_by_type": library_data["labels_by_type"],
            "filters": library_data["filters"],
            "filter_options": library_data["options"],
            "active_chips": active_chips,
            "result_count": library_data["result_count"],
            "all_count": library_data["all_count"],
            "has_more": library_data["has_more"],
            "next_url": next_url,
            "library_return_to": quote(str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")),
        },
    )


@app.get("/generate", response_class=HTMLResponse)
def generate_page(request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "generate.html",
        {
            "request": request,
            "current_user": user,
            "word_banks": word_bank_options(db),
            "puzzle_types": list_enabled_puzzle_types(db),
            "generation_schemas": puzzle_type_generation_schemas(db),
        },
    )


@app.post("/web/puzzles/generate")
async def web_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    theme: str = Form(""),
    word_bank_slugs: list[str] = Form(default=[]),
    difficulty: str = Form("easy"),
    seed: str = Form(""),
    auto_archive_at: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    puzzle_type_config = get_puzzle_type(puzzle_type)
    if puzzle_type_config is None or not is_puzzle_type_enabled(db, puzzle_type):
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    needs_word_bank = bool(puzzle_type_config and "word_bank" in puzzle_type_config.source_requirements)
    selected_banks = word_banks_for_slugs(db, word_bank_slugs) if needs_word_bank else []
    generation_theme = word_bank_source_theme(selected_banks, theme) if needs_word_bank else (
        theme if (puzzle_type_config is None or puzzle_type_config.theme_mode != "none") else ""
    )
    puzzle = create_puzzle_record(
        PuzzleGenerateRequest(
            puzzle_type=puzzle_type,
            theme=generation_theme,
            word_bank_slugs=[bank.slug for bank in selected_banks],
            difficulty=difficulty,
            seed=seed or None,
            generation_options=generation_options_from_form(form),
        ),
        db,
        creator=user,
    )
    saved_puzzle = db.get(Puzzle, puzzle.id)
    if saved_puzzle:
        saved_puzzle.auto_archive_at = parse_local_datetime(auto_archive_at)
        db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/web/users")
def web_create_user(request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/admin/users", status_code=303)


def admin_context(
    request: Request,
    db: Session,
    user: User,
    section: str,
    selected_word: DictionaryWord | None = None,
    selected_word_bank: WordBank | None = None,
    selected_tag: PuzzleTag | None = None,
    selected_puzzle: Puzzle | None = None,
    manual_form_error: str = "",
) -> dict:
    puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at))).all()
    all_word_banks = db.scalars(select(WordBank).order_by(WordBank.name)).all()
    word_banks = [bank for bank in all_word_banks if bank.archived_at is None]
    categories = db.scalars(select(PuzzleCategory).order_by(PuzzleCategory.name)).all()
    tags = db.scalars(select(PuzzleTag).order_by(PuzzleTag.tag_type, PuzzleTag.name)).all()
    tag_groups: dict[str, list[PuzzleTag]] = {}
    tag_usage: dict[int, dict[str, int]] = {}
    for tag in tags:
        tag_groups.setdefault(tag.tag_type or "General", []).append(tag)
        tag_usage[tag.id] = {
            "puzzles": len(tag.puzzles),
            "word_banks": len(tag.word_banks),
            "words": len(tag.dictionary_words),
            "definitions": len(tag.dictionary_definitions),
        }
        tag_usage[tag.id]["total"] = sum(tag_usage[tag.id].values())
    attempts = db.scalars(select(PuzzleAttempt).order_by(desc(PuzzleAttempt.created_at)).limit(20)).all()
    progress_rows = db.scalars(select(PuzzleProgress).order_by(desc(PuzzleProgress.updated_at)).limit(20)).all()
    photos = db.scalars(select(PuzzlePhoto).order_by(desc(PuzzlePhoto.created_at)).limit(20)).all()
    dictionary_words = db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all()
    recent_words = db.scalars(select(DictionaryWord).order_by(desc(DictionaryWord.created_at)).limit(8)).all()
    recent_word_banks = db.scalars(select(WordBank).order_by(desc(WordBank.created_at)).limit(8)).all()
    dictionary_import_sources = db.scalars(select(DictionaryImportSource).order_by(DictionaryImportSource.name)).all()
    admin_users = db.scalars(select(User).order_by(User.display_name)).all()
    dashboard_review = dashboard_review_context(db, puzzles, dictionary_words, all_word_banks, tags)
    return {
        "request": request,
        "current_user": user,
        "section": section,
        "issues": db.scalars(select(PublishedIssue).order_by(desc(PublishedIssue.created_at))).all() if section == "publishing" else [],
        "selected_issue": None,
        "publishing_puzzles": [
            {"id": puzzle.id, "title": puzzle.title, "puzzle_type": puzzle.puzzle_type,
             "archived": puzzle.archived_at is not None}
            for puzzle in puzzles
            if not (puzzle.puzzle_metadata or {}).get("draft")
            and (plugin := get_puzzle_type(puzzle.puzzle_type)) is not None
            and plugin.supports_print and not plugin.digital_only
        ] if section == "publishing" else [],
        "app_settings": {
            "public_base_url": app_base_url(db),
            "default_base_url": DEFAULT_APP_BASE_URL,
            "disabled_puzzle_types": sorted(disabled_puzzle_type_slugs(db)),
        },
        "puzzles": puzzles,
        "live_puzzles": [puzzle for puzzle in puzzles if puzzle.archived_at is None],
        "archived_puzzles": [puzzle for puzzle in puzzles if puzzle.archived_at is not None],
        "all_word_banks": all_word_banks,
        "word_banks": word_banks,
        "word_bank_texts": {bank.id: word_bank_words(bank) for bank in all_word_banks},
        "word_bank_entries": {bank.id: word_bank_entries(db, bank) for bank in all_word_banks},
        "categories": categories,
        "tags": tags,
        "assignable_tags": [tag for tag in tags if tag.tag_type != "Clue"],
        "tag_groups": tag_groups,
        "tag_usage": tag_usage,
        "attempts": attempts,
        "progress_rows": progress_rows,
        "photos": photos,
        "dictionary_words": dictionary_words,
        "recent_words": recent_words,
        "recent_word_banks": recent_word_banks,
        "dictionary_import_sources": dictionary_import_sources,
        "admin_users": admin_users,
        "dashboard_review": dashboard_review,
        "selected_word": selected_word,
        "selected_word_bank": selected_word_bank,
        "selected_tag": selected_tag,
        "selected_puzzle": selected_puzzle,
        "manual_form_error": manual_form_error,
        "puzzle_types": list_enabled_puzzle_types(db),
        "all_puzzle_types": list_puzzle_types(),
        "generation_schemas": puzzle_type_generation_schemas(db),
    }


def render_admin(
    request: Request,
    db: Session,
    section: str,
    selected_word: DictionaryWord | None = None,
    selected_word_bank: WordBank | None = None,
    selected_tag: PuzzleTag | None = None,
    selected_puzzle: Puzzle | None = None,
    manual_form_error: str = "",
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "admin.html",
        admin_context(
            request,
            db,
            user,
            section,
            selected_word,
            selected_word_bank,
            selected_tag,
            selected_puzzle,
            manual_form_error,
        ),
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "dashboard")


@app.get("/admin/puzzles", response_class=HTMLResponse)
def admin_puzzles_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "puzzles")


@app.get("/admin/puzzles/manual", response_class=HTMLResponse)
def admin_manual_puzzle_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "puzzle_edit")


@app.get("/admin/puzzles/{puzzle_id}/edit", response_class=HTMLResponse)
def admin_edit_puzzle_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return render_admin(request, db, "puzzle_edit", selected_puzzle=puzzle)


@app.get("/admin/generate", response_class=HTMLResponse)
def admin_generate_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "generate")


@app.get("/admin/word-banks", response_class=HTMLResponse)
def admin_word_banks_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "word_banks")


@app.get("/admin/word-banks/{bank_id}", response_class=HTMLResponse)
def admin_word_bank_detail_page(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    return templates.TemplateResponse(
        "admin.html",
        admin_context(request, db, user, "word_bank_detail", selected_word_bank=bank),
    )


@app.get("/admin/dictionary", response_class=HTMLResponse)
def admin_dictionary_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "dictionary")


@app.get("/admin/dictionary/{word_id}", response_class=HTMLResponse)
def admin_dictionary_word_page(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Word not found")
    return templates.TemplateResponse(
        "admin.html",
        admin_context(request, db, user, "word_detail", word),
    )


@app.get("/admin/taxonomy", response_class=HTMLResponse)
def admin_taxonomy_page(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.get("/admin/tags", response_class=HTMLResponse)
def admin_tags_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "tags")


@app.get("/admin/tags/{tag_id}", response_class=HTMLResponse)
def admin_tag_detail_page(tag_id: int, request: Request, db: Session = Depends(get_db)):
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return render_admin(request, db, "tag_detail", selected_tag=tag)


@app.get("/admin/activity", response_class=HTMLResponse)
def admin_activity_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "activity")


@app.get("/admin/publishing", response_class=HTMLResponse)
def admin_publishing_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "publishing")


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "users")


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "settings")


@app.post("/admin/settings")
def admin_update_settings(
    request: Request,
    public_base_url: str = Form(""),
    disabled_puzzle_types: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        base_url = normalize_base_url(public_base_url) or DEFAULT_APP_BASE_URL
    except ValueError as error:
        return RedirectResponse(url=f"/admin/settings?error={quote(str(error))}", status_code=303)
    set_app_setting(db, "public_base_url", base_url)
    set_disabled_puzzle_type_slugs(db, disabled_puzzle_types)
    db.commit()
    return RedirectResponse(url="/admin/settings?message=Settings%20saved.", status_code=303)


@app.post("/admin/users")
def admin_create_user(
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    is_admin: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    new_user, setup_token = create_user_record(db, display_name, email, is_admin == "on", password)
    if setup_token:
        return RedirectResponse(
            url=f"/admin/users?setup_user={quote(new_user.display_name)}&setup_token={quote(setup_token)}",
            status_code=303,
        )
    return RedirectResponse(url="/admin/users?message=User%20created.", status_code=303)


@app.post("/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    is_admin: str = Form(""),
    is_active: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    requested_admin = is_admin == "on"
    requested_active = is_active == "on"
    if (user.is_admin and not requested_admin) or (user.is_active and not requested_active):
        if not can_remove_admin(db, user):
            return RedirectResponse(url="/admin/users?error=Keep%20at%20least%20one%20active%20admin.", status_code=303)
    clean_name = display_name.strip()
    if not clean_name:
        return RedirectResponse(url="/admin/users?error=Display%20name%20is%20required.", status_code=303)
    existing_name = db.scalar(select(User).where(User.display_name == clean_name, User.id != user.id))
    if existing_name:
        return RedirectResponse(url="/admin/users?error=That%20display%20name%20is%20already%20in%20use.", status_code=303)
    clean_email = normalized_email(email)
    if clean_email:
        existing_email = db.scalar(select(User).where(User.email == clean_email, User.id != user.id))
        if existing_email:
            return RedirectResponse(url="/admin/users?error=That%20email%20is%20already%20in%20use.", status_code=303)
    user.display_name = clean_name
    user.email = clean_email
    user.is_admin = requested_admin
    if user.is_active != requested_active:
        user.session_version = (user.session_version or 1) + 1
    user.is_active = requested_active
    db.commit()
    return RedirectResponse(url="/admin/users?message=User%20updated.", status_code=303)


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_user_password(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    token = issue_password_setup_token(db, user)
    return RedirectResponse(
        url=f"/admin/users?setup_user={quote(user.display_name)}&setup_token={quote(token)}",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_user_sessions(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.session_version = (user.session_version or 1) + 1
    db.commit()
    return RedirectResponse(url="/admin/users?message=Sessions%20revoked.", status_code=303)


@app.post("/admin/puzzles/manual")
async def admin_create_manual_puzzle(
    request: Request,
    puzzle_type: str = Form(...),
    title: str = Form(""),
    theme: str = Form("manual"),
    difficulty: str = Form("manual"),
    editor_mode: str = Form("raw"),
    prompt: str = Form(""),
    print_payload: str = Form("{}"),
    play_payload: str = Form("{}"),
    solution_payload: str = Form("{}"),
    metadata: str = Form("{}"),
    hints: str = Form(""),
    draft: str = Form("true"),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        form = await request.form()
        if editor_mode == "helper":
            payload = build_manual_payload_from_helper(db, puzzle_type, title, theme, difficulty, draft, form)
        else:
            payload = manual_payload_from_form(
                puzzle_type,
                title,
                theme,
                difficulty,
                prompt,
                print_payload,
                play_payload,
                solution_payload,
                metadata,
                hints,
                draft,
            )
        puzzle = write_manual_puzzle(db, payload, user)
    except ValueError as error:
        return render_admin(request, db, "puzzle_edit", manual_form_error=str(error))
    return RedirectResponse(url=f"/admin/puzzles/{puzzle.id}/edit?message=Manual%20puzzle%20saved.", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/edit")
async def admin_update_manual_puzzle(
    puzzle_id: str,
    request: Request,
    puzzle_type: str = Form(...),
    title: str = Form(""),
    theme: str = Form("manual"),
    difficulty: str = Form("manual"),
    editor_mode: str = Form("raw"),
    prompt: str = Form(""),
    print_payload: str = Form("{}"),
    play_payload: str = Form("{}"),
    solution_payload: str = Form("{}"),
    metadata: str = Form("{}"),
    hints: str = Form(""),
    draft: str = Form("true"),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    try:
        form = await request.form()
        if editor_mode == "helper":
            payload = build_manual_payload_from_helper(db, puzzle_type, title, theme, difficulty, draft, form)
        else:
            payload = manual_payload_from_form(
                puzzle_type,
                title,
                theme,
                difficulty,
                prompt,
                print_payload,
                play_payload,
                solution_payload,
                metadata,
                hints,
                draft,
            )
        puzzle = write_manual_puzzle(db, payload, user, puzzle)
    except ValueError as error:
        return render_admin(request, db, "puzzle_edit", selected_puzzle=puzzle, manual_form_error=str(error))
    return RedirectResponse(url=f"/admin/puzzles/{puzzle.id}/edit?message=Manual%20puzzle%20saved.", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/publish")
def admin_publish_manual_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    payload = PuzzleManualWrite(
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        theme=puzzle.theme,
        difficulty=puzzle.difficulty,
        prompt=puzzle.prompt,
        print_payload=puzzle.print_payload or {},
        play_payload=puzzle.play_payload or {},
        solution_payload=puzzle.solution_payload or {},
        metadata=puzzle.puzzle_metadata or {},
        hints=[hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
        draft=False,
    )
    valid, error = validate_manual_puzzle_payload(db, payload, allow_draft=False)
    if not valid:
        return render_admin(request, db, "puzzle_edit", selected_puzzle=puzzle, manual_form_error=error)
    metadata = dict(puzzle.puzzle_metadata or {})
    metadata["manual"] = True
    metadata["draft"] = False
    metadata["manual_published_at"] = utcnow().isoformat()
    puzzle.puzzle_metadata = metadata
    puzzle.archived_at = None
    if puzzle.auto_archive_at is None:
        puzzle.auto_archive_at = default_auto_archive_at()
    db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/admin/users/{user_id}/api-token")
def admin_issue_user_api_token(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin_user(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    token = issue_api_token(db, user)
    return RedirectResponse(
        url=f"/admin/users?api_user={quote(user.display_name)}&api_token={quote(token)}",
        status_code=303,
    )


@app.post("/admin/puzzles/{puzzle_id}/archive")
def admin_archive_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        puzzle.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/unarchive")
def admin_unarchive_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        puzzle.archived_at = None
        if puzzle.auto_archive_at is not None and puzzle.auto_archive_at <= utcnow():
            puzzle.auto_archive_at = None
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/puzzles/{puzzle_id}/delete")
def admin_delete_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        puzzle.tags = []
        db.delete(puzzle)
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/puzzles/import")
async def admin_import_puzzles(
    request: Request,
    source_url: str = Form(""),
    source_text: str = Form(""),
    source_api_token: str = Form(""),
    dry_run: bool = Form(True),
    duplicate_policy: str = Form("skip"),
    upload: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    uploaded_text = ""
    if upload is not None and upload.filename:
        uploaded_text = (await upload.read(1_000_000 + 1)).decode("utf-8", errors="replace")
        if len(uploaded_text) > 1_000_000:
            raise HTTPException(status_code=400, detail="Uploaded puzzle file is too large")
    context = admin_context(request, db, user, "puzzles")
    try:
        items = parse_puzzle_import_items(
            source_text=uploaded_text or source_text,
            source_url=source_url,
            api_token=source_api_token,
        )
        result = import_portable_puzzles(
            db,
            items,
            creator=user,
            dry_run=dry_run,
            duplicate_policy=duplicate_policy,
        )
    except ValueError as error:
        result = {
            "dry_run": dry_run,
            "errors": [str(error)],
            "items": [],
            "processed": 0,
            "imported": 0,
            "skipped": 0,
        }
    context["puzzle_import_result"] = result
    return templates.TemplateResponse("admin.html", context)


@app.post("/admin/puzzles/{puzzle_id}/metadata")
def admin_update_puzzle_metadata(
    puzzle_id: str,
    request: Request,
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    auto_archive_at: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        puzzle.category_id = category_id or None
        puzzle.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
        puzzle.auto_archive_at = parse_local_datetime(auto_archive_at)
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/generate")
async def admin_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    bank_id: int = Form(0),
    bank_ids: list[int] = Form(default=[]),
    theme: str = Form(""),
    difficulty: str = Form("easy"),
    seed: str = Form(""),
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    excluded_tag_ids: list[int] = Form(default=[]),
    auto_archive_at: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle_type_config = get_puzzle_type(puzzle_type)
    if puzzle_type_config is None or not is_puzzle_type_enabled(db, puzzle_type):
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    needs_word_bank = "word_bank" in puzzle_type_config.source_requirements
    selected_bank_ids = bank_ids or ([bank_id] if bank_id else [])
    banks = word_banks_for_ids(db, selected_bank_ids) if needs_word_bank else []
    if needs_word_bank and not banks:
        raise HTTPException(status_code=404, detail="Word bank not found")
    generation_theme = ""
    if banks:
        generation_theme = word_bank_source_theme(banks)
    elif puzzle_type_config.theme_mode != "none":
        generation_theme = slugify(theme) if theme.strip() else puzzle_type_config.label
    form = await request.form()
    puzzle = create_puzzle_record(
        PuzzleGenerateRequest(
            puzzle_type=puzzle_type,
            theme=generation_theme,
            word_bank_slugs=[bank.slug for bank in banks],
            difficulty=difficulty,
            excluded_tag_ids=excluded_tag_ids,
            seed=seed or None,
            generation_options=generation_options_from_form(form),
        ),
        db,
        creator=user,
    )
    saved_puzzle = db.get(Puzzle, puzzle.id)
    if saved_puzzle is not None:
        source_category = banks[0].category_id if len(banks) == 1 else None
        source_tags = list(banks[0].tags) if len(banks) == 1 else []
        saved_puzzle.category_id = category_id or source_category
        saved_puzzle.tags = tags_for_ids(db, tag_ids) if tag_ids else source_tags
        saved_puzzle.auto_archive_at = parse_local_datetime(auto_archive_at)
        db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/admin/word-banks")
def admin_create_word_bank(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    mode: str = Form("manual"),
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = WordBank(
        name=name.strip(),
        slug=unique_slug(db, WordBank, slug or name),
        category_id=category_id or None,
        description=description.strip(),
        payload={"mode": mode if mode == "automatic" else "manual", "words": parse_words(words)},
    )
    bank.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
    db.add(bank)
    db.flush()
    sync_word_bank_words(db, bank, parse_words(words))
    db.commit()
    db.refresh(bank)
    return RedirectResponse(url=f"/admin/word-banks/{bank.id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}")
def admin_update_word_bank(
    bank_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    mode: str = Form("manual"),
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.name = name.strip()
        bank.slug = unique_slug(db, WordBank, slug or name, bank.id)
        bank.category_id = category_id or None
        bank.description = description.strip()
        parsed_words = parse_words(words)
        selected_mode = mode if mode == "automatic" else "manual"
        bank.payload = {"mode": selected_mode, "words": parsed_words}
        bank.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
        if selected_mode == "manual" and parsed_words:
            sync_word_bank_words(db, bank, parsed_words)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/words")
def admin_add_word_bank_word(
    bank_id: int,
    request: Request,
    word_definition: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        word_text, definition_text = word_definition.split(":", 1)
        word_id = int(word_text)
        definition_id = int(definition_text or 0)
    except ValueError:
        return RedirectResponse(url="/admin/word-banks", status_code=303)
    bank = db.get(WordBank, bank_id)
    word = db.get(DictionaryWord, word_id)
    definition = db.get(DictionaryDefinition, definition_id) if definition_id else None
    if bank is not None and word is not None:
        if definition is not None and definition.word_id != word.id:
            definition = None
        payload = bank.payload if isinstance(bank.payload, dict) else {}
        payload["mode"] = "manual"
        bank.payload = payload
        add_word_bank_link(db, bank, word, definition)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/words/{link_id}/delete")
def admin_delete_word_bank_word(
    bank_id: int,
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    link = db.get(WordBankWord, link_id)
    if link is not None and link.word_bank_id == bank_id:
        db.delete(link)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/archive")
def admin_archive_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/unarchive")
def admin_unarchive_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.archived_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/word-banks/{bank_id}", status_code=303)


@app.post("/admin/word-banks/{bank_id}/delete")
def admin_delete_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        for puzzle in bank.puzzles:
            puzzle.word_bank_id = None
        bank.tags = []
        db.delete(bank)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/dictionary")
def admin_create_dictionary_word(
    request: Request,
    word: str = Form(...),
    display_text: str = Form(""),
    clue: str = Form(""),
    part_of_speech: str = Form(""),
    tag_ids: list[int] = Form(default=[]),
    properties: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = get_or_create_word(db, word)
    if entry is not None:
        entry.display_text = display_text.strip() or normalize_word(word)
        entry.clue = clue.strip()
        entry.part_of_speech = part_of_speech.strip()
        entry.tags = tags_for_ids(db, tag_ids)
        entry.properties = {"notes": properties.strip()} if properties.strip() else {}
        ensure_definition_from_clue(db, entry)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{entry.id}" if entry is not None else "/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import")
def admin_import_dictionary(
    request: Request,
    source_url: str = Form(...),
    source_type: str = Form("auto"),
    source_label: str = Form(""),
    source_api_token: str = Form(""),
    dry_run: bool = Form(True),
    tag_ids: list[int] = Form(default=[]),
    max_items: int = Form(500),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    context = admin_context(request, db, user, "dictionary")
    try:
        sources = fetch_dictionary_source(source_url, source_type, source_api_token)
        imported_words = parse_imported_words(sources)
        result = import_dictionary_words(
            db,
            imported_words,
            source_label=source_label or source_url,
            tag_ids=tag_ids,
            dry_run=dry_run,
            max_items=max_items,
        )
        result["source_count"] = len(sources)
        result["source_url"] = source_url
        result["source_type"] = source_type
    except ValueError as error:
        result = {
            "dry_run": dry_run,
            "source_url": source_url,
            "source_type": source_type,
            "errors": [str(error)],
        }
    context["dictionary_import_result"] = result
    return templates.TemplateResponse("admin.html", context)


@app.post("/admin/dictionary/import-sources")
def admin_create_dictionary_import_source(
    request: Request,
    name: str = Form(...),
    source_url: str = Form(...),
    source_type: str = Form("puzzary"),
    source_label: str = Form(""),
    source_api_token: str = Form(""),
    sync_interval_hours: int = Form(24),
    max_items: int = Form(500),
    enabled: bool = Form(True),
    sync_now: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = DictionaryImportSource(
        name=name.strip(),
        source_url=source_url.strip(),
        source_type=source_type.strip() or "auto",
        source_label=source_label.strip(),
        source_api_token=source_api_token.strip(),
        sync_interval_hours=max(1, min(sync_interval_hours, 24 * 30)),
        max_items=max(1, min(max_items, 5000)),
        enabled=enabled,
    )
    schedule_next_dictionary_import(source, utcnow())
    db.add(source)
    db.commit()
    db.refresh(source)
    if sync_now:
        run_dictionary_import_source(db, source, dry_run=False)
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import-sources/{source_id}/sync")
def admin_sync_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = db.get(DictionaryImportSource, source_id)
    if source is not None:
        run_dictionary_import_source(db, source, dry_run=False)
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import-sources/{source_id}/toggle")
def admin_toggle_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = db.get(DictionaryImportSource, source_id)
    if source is not None:
        source.enabled = not source.enabled
        if source.enabled and source.next_sync_at is None:
            schedule_next_dictionary_import(source, utcnow())
        db.commit()
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/import-sources/{source_id}/delete")
def admin_delete_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = db.get(DictionaryImportSource, source_id)
    if source is not None:
        db.delete(source)
        db.commit()
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/{word_id}")
def admin_update_dictionary_word(
    word_id: int,
    request: Request,
    display_text: str = Form(""),
    clue: str = Form(""),
    part_of_speech: str = Form(""),
    tag_ids: list[int] = Form(default=[]),
    properties: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.display_text = display_text.strip() or entry.word
        entry.clue = clue.strip()
        entry.part_of_speech = part_of_speech.strip()
        entry.tags = tags_for_ids(db, tag_ids)
        entry.properties = {"notes": properties.strip()} if properties.strip() else {}
        ensure_definition_from_clue(db, entry)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/archive")
def admin_archive_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/unarchive")
def admin_unarchive_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.archived_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/ban")
def admin_ban_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.banned_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/unban")
def admin_unban_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        entry.banned_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/delete")
def admin_delete_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryWord, word_id)
    if entry is not None:
        linked_banks = [link.word_bank for link in entry.bank_links]
        for bank in linked_banks:
            payload_words = bank.payload.get("words", []) if isinstance(bank.payload, dict) else []
            if isinstance(payload_words, list):
                bank.payload = {"words": [word for word in payload_words if normalize_word(str(word)) != entry.word]}
        for definition in entry.definitions:
            definition.tags = []
        entry.tags = []
        entry.bank_links.clear()
        db.delete(entry)
        db.commit()
    return RedirectResponse(url="/admin/dictionary", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions")
def admin_create_dictionary_definition(
    word_id: int,
    request: Request,
    definition: str = Form(...),
    part_of_speech: str = Form(""),
    part_of_speech_tag: str = Form(""),
    clue_tag: str = Form(""),
    example: str = Form(""),
    tag_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Word not found")
    next_position = max((item.position for item in word.definitions), default=-1) + 1
    entry = DictionaryDefinition(
        definition=definition.strip(),
        part_of_speech=(part_of_speech_tag or part_of_speech).strip(),
        example=example.strip(),
        position=next_position,
    )
    entry.tags = definition_tags_from_form(db, tag_ids, part_of_speech_tag or part_of_speech, clue_tag)
    word.definitions.append(entry)
    db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}")
def admin_update_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    definition: str = Form(...),
    part_of_speech: str = Form(""),
    part_of_speech_tag: str = Form(""),
    clue_tag: str = Form(""),
    example: str = Form(""),
    position: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.definition = definition.strip()
        entry.part_of_speech = (part_of_speech_tag or part_of_speech).strip()
        entry.example = example.strip()
        entry.position = position
        entry.tags = definition_tags_from_form(db, tag_ids, part_of_speech_tag or part_of_speech, clue_tag)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}/delete")
def admin_delete_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.tags = []
        db.delete(entry)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}/archive")
def admin_archive_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionary/{word_id}/definitions/{definition_id}/unarchive")
def admin_unarchive_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    entry = db.get(DictionaryDefinition, definition_id)
    if entry is not None and entry.word_id == word_id:
        entry.archived_at = None
        db.commit()
    return RedirectResponse(url=f"/admin/dictionary/{word_id}", status_code=303)


@app.post("/admin/dictionaries")
def admin_create_dictionary(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dictionary = PuzzleDictionary(
        name=name.strip(),
        slug=unique_slug(db, PuzzleDictionary, slug or name),
        description=description.strip(),
        words=parse_words(words),
    )
    db.add(dictionary)
    db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/dictionaries/{dictionary_id}")
def admin_update_dictionary(
    dictionary_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    words: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dictionary = db.get(PuzzleDictionary, dictionary_id)
    if dictionary is not None:
        dictionary.name = name.strip()
        dictionary.slug = unique_slug(db, PuzzleDictionary, slug or name, dictionary.id)
        dictionary.description = description.strip()
        dictionary.words = parse_words(words)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/dictionaries/{dictionary_id}/delete")
def admin_delete_dictionary(dictionary_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dictionary = db.get(PuzzleDictionary, dictionary_id)
    if dictionary is not None:
        db.delete(dictionary)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/categories")
def admin_create_category(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    db.add(PuzzleCategory(name=name.strip(), slug=unique_slug(db, PuzzleCategory, slug or name), description=description.strip()))
    db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.post("/admin/categories/{category_id}")
def admin_update_category(
    category_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    category = db.get(PuzzleCategory, category_id)
    if category is not None:
        category.name = name.strip()
        category.slug = unique_slug(db, PuzzleCategory, slug or name, category.id)
        category.description = description.strip()
        db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.post("/admin/categories/{category_id}/delete")
def admin_delete_category(category_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    category = db.get(PuzzleCategory, category_id)
    if category is not None:
        for puzzle in category.puzzles:
            puzzle.category_id = None
        for bank in category.word_banks:
            bank.category_id = None
        db.delete(category)
        db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.post("/admin/tags")
def admin_create_tag(
    request: Request,
    name: str = Form(...),
    tag_type: str = Form("General"),
    slug: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    normalized_type = normalize_tag_type(tag_type)
    tag = PuzzleTag(
        name=name.strip(),
        tag_type=normalized_type,
        slug=unique_slug(db, PuzzleTag, slug or typed_tag_slug(normalized_type, name)),
    )
    db.add(tag)
    db.commit()
    return RedirectResponse(url=f"/admin/tags/{tag.id}", status_code=303)


@app.post("/admin/tags/{tag_id}")
def admin_update_tag(
    tag_id: int,
    request: Request,
    name: str = Form(...),
    tag_type: str = Form("General"),
    slug: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    tag = db.get(PuzzleTag, tag_id)
    if tag is not None:
        normalized_type = normalize_tag_type(tag_type)
        tag.name = name.strip()
        tag.tag_type = normalized_type
        tag.slug = unique_slug(db, PuzzleTag, slug or typed_tag_slug(normalized_type, name), tag.id)
        db.commit()
    return RedirectResponse(url=f"/admin/tags/{tag_id}", status_code=303)


@app.post("/admin/tags/{tag_id}/delete")
def admin_delete_tag(tag_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    tag = db.get(PuzzleTag, tag_id)
    if tag is not None:
        tag.puzzles = []
        tag.word_banks = []
        tag.dictionary_words = []
        tag.dictionary_definitions = []
        db.delete(tag)
        db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


@app.get("/puzzles/{puzzle_id}", response_class=HTMLResponse)
def puzzle_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    users = db.scalars(select(User).order_by(User.display_name)).all()
    attempts = db.scalars(
        select(PuzzleAttempt).where(PuzzleAttempt.puzzle_id == puzzle_id).order_by(desc(PuzzleAttempt.created_at)).limit(10)
    ).all()
    photos = db.scalars(
        select(PuzzlePhoto).where(PuzzlePhoto.puzzle_id == puzzle_id).order_by(desc(PuzzlePhoto.created_at)).limit(10)
    ).all()
    progress = db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id == puzzle_id, PuzzleProgress.user_id == user.id))
    progress_payload = progress.progress_payload if progress is not None else {}
    progress_answers = progress_payload.get("answers", {})
    if not isinstance(progress_answers, dict):
        progress_answers = {}
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    puzzle_template_name = f"puzzle_types/{puzzle.puzzle_type}.html"
    solution_template_name = f"puzzle_solutions/{puzzle.puzzle_type}.html"
    has_puzzle_template = template_exists(puzzle_template_name)
    has_solution_template = template_exists(solution_template_name)
    return_to = safe_library_return_to(request.query_params.get("return_to", "/library"))
    return templates.TemplateResponse(
        "puzzle.html",
        {
            "request": request,
            "puzzle": puzzle,
            "users": users,
            "attempts": attempts,
            "photos": photos,
            "current_user": user,
            "progress_found_words": found_words_for_progress(progress),
            "progress_payload": progress_payload,
            "progress_answers": progress_answers,
            "puzzle_type_definition": puzzle_type,
            "puzzle_template": puzzle_template_name,
            "solution_template": solution_template_name,
            "has_puzzle_template": has_puzzle_template,
            "has_solution_template": has_solution_template,
            "playable_puzzle_type": puzzle_type is not None and puzzle_type.supports_web_play and has_puzzle_template,
            "return_to": return_to,
        },
    )


@app.get("/puzzles/{puzzle_id}/print", response_class=HTMLResponse)
def puzzle_print_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render_puzzle_print_page(puzzle_id, request, db, user, show_toolbar=True)


@app.get("/puzzles/{puzzle_id}/print/render", response_class=HTMLResponse)
def puzzle_print_render_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    return render_puzzle_print_page(puzzle_id, request, db, None, show_toolbar=False)


@app.get("/puzzles/{puzzle_id}/print.pdf")
def puzzle_pdf_download(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return puzzle_pdf_response(puzzle_id, request, db, user)


@app.get("/api/puzzles/{puzzle_id}/print.pdf")
def api_puzzle_pdf(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_api_user(request, db)
    return puzzle_pdf_response(puzzle_id, request, db, user)


def puzzle_pdf_response(puzzle_id: str, request: Request, db: Session, user: User) -> Response:
    rendered = render_puzzle_print_page(puzzle_id, request, db, user, show_toolbar=False, pdf_mode=True)
    layout = rendered.context["print_layout"]
    try:
        pdf = render_pdf(rendered.body.decode("utf-8"), layout)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "10"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(status_code=503, detail="PDF rendering failed. Please retry.") from exc
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="puzzary-{puzzle_id}-{layout["page_size"]}.pdf"',
        "Cache-Control": "private, no-store",
    })


def render_puzzle_print_page(
    puzzle_id: str,
    request: Request,
    db: Session,
    user: User | None,
    show_toolbar: bool,
    pdf_mode: bool = False,
) -> HTMLResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only:
        raise HTTPException(status_code=404, detail="This puzzle does not support printing")
    print_layout = apply_print_request_layout(print_layout_context(puzzle, puzzle_type, db), request)
    return templates.TemplateResponse(
        "print.html",
        {
            "request": request,
            "puzzle": puzzle,
            "current_user": user,
            "puzzle_type_definition": puzzle_type,
            "print_payload": puzzle.print_payload or {},
            "has_module_print_template": template_exists(f"puzzle_print/{puzzle.puzzle_type}.html"),
            "print_layout": print_layout,
            "print_gimme_cells": printable_gimme_cells(puzzle),
            "print_gimme_words": printable_gimme_words(puzzle),
            "show_print_toolbar": show_toolbar,
            "hide_app_chrome": not show_toolbar,
            "print_pdf_css": (BASE_DIR / "static" / "styles.css").read_text(encoding="utf-8") if pdf_mode else None,
        },
    )


@app.post("/web/puzzles/{puzzle_id}/attempts")
def web_attempt_puzzle(
    puzzle_id: str,
    request: Request,
    answer: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    result = submit_puzzle_attempt(puzzle_id, answer, user.id, db)
    celebration = "?celebrate=1" if result.is_correct else ""
    return RedirectResponse(url=f"/puzzles/{puzzle_id}{celebration}", status_code=303)


@app.post("/web/puzzles/{puzzle_id}/photos")
def web_upload_photo(
    puzzle_id: str,
    request: Request,
    notes: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    save_photo(puzzle_id, file, notes, user.id, db)
    return RedirectResponse(url=f"/puzzles/{puzzle_id}", status_code=303)


@app.post("/web/puzzles/{puzzle_id}/reset")
def web_reset_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    reset_user_puzzle(puzzle_id, user.id, db)
    return RedirectResponse(url=f"/puzzles/{puzzle_id}", status_code=303)


@app.post("/api/users")
def create_user(payload: UserCreate, request: Request, db: Session = Depends(get_db)) -> dict[str, int | str | bool]:
    require_admin_api(request, db)
    user, setup_token = create_user_record(db, payload.display_name)
    return {
        "id": user.id,
        "display_name": user.display_name,
        "requires_password_setup": bool(setup_token),
        "setup_url": setup_url(setup_token, db) if setup_token else "",
    }


def apply_definition_payload(
    db: Session,
    definition: DictionaryDefinition,
    payload: DictionaryDefinitionWrite | DictionaryDefinitionPatch,
) -> None:
    if payload.definition is not None:
        definition.definition = payload.definition.strip()
    if payload.part_of_speech is not None:
        definition.part_of_speech = payload.part_of_speech.strip()
    if payload.example is not None:
        definition.example = payload.example.strip()
    if payload.position is not None:
        definition.position = payload.position
    if payload.properties is not None:
        definition.properties = payload.properties
    if isinstance(payload, DictionaryDefinitionPatch) and payload.archived is not None:
        definition.archived_at = datetime.now(timezone.utc) if payload.archived else None
    if payload.tag_ids is not None or payload.clues is not None or payload.part_of_speech is not None:
        tag_ids = payload.tag_ids if payload.tag_ids is not None else [tag.id for tag in definition.tags if tag.tag_type != "Clue"]
        clues = payload.clues if payload.clues is not None else tag_values(definition.tags, "Clue")
        part_of_speech = payload.part_of_speech if payload.part_of_speech is not None else definition.part_of_speech
        definition.tags = definition_tags_from_form(db, tag_ids, part_of_speech, "\n".join(clues))


def apply_word_bank_payload(db: Session, bank: WordBank, payload: WordBankWrite | WordBankPatch) -> None:
    existing_payload = bank.payload if isinstance(bank.payload, dict) else {}
    if payload.name is not None:
        bank.name = payload.name.strip()
    if payload.slug is not None:
        bank.slug = unique_slug(db, WordBank, payload.slug or bank.name, bank.id)
    if payload.description is not None:
        bank.description = payload.description.strip()
    if payload.mode is not None:
        existing_payload["mode"] = payload.mode if payload.mode == "automatic" else "manual"
    if payload.properties is not None:
        existing_payload["properties"] = payload.properties
    if payload.words is not None:
        parsed_words = parse_words("\n".join(payload.words))
        existing_payload["words"] = parsed_words
        if existing_payload.get("mode") != "automatic":
            sync_word_bank_words(db, bank, parsed_words)
    if payload.category_id is not None:
        bank.category_id = payload.category_id or None
    if payload.tag_ids is not None:
        bank.tags = tags_for_ids(db, payload.tag_ids)
    if isinstance(payload, WordBankPatch) and payload.archived is not None:
        bank.archived_at = datetime.now(timezone.utc) if payload.archived else None
    bank.payload = existing_payload


@app.get("/api/dictionary")
def api_list_dictionary(
    request: Request,
    search: str = "",
    include_archived: bool = False,
    include_banned: bool = True,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    query = select(DictionaryWord).order_by(DictionaryWord.word)
    words = db.scalars(query).all()
    if search:
        search_text = search.strip().lower()
        words = [word for word in words if search_text in word.word.lower() or search_text in word.display_text.lower()]
    if not include_archived:
        words = [word for word in words if word.archived_at is None]
    if not include_banned:
        words = [word for word in words if word.banned_at is None]
    total = len(words)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_dictionary_word(word) for word in words[offset : offset + limit]],
    }


@app.post("/api/dictionary/import")
def api_import_dictionary(payload: DictionaryImportRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    try:
        sources = fetch_dictionary_source(payload.source_url, payload.source_type, payload.source_api_token)
        imported_words = parse_imported_words(sources)
        result = import_dictionary_words(
            db,
            imported_words,
            source_label=payload.source_label or payload.source_url,
            tag_ids=payload.tag_ids,
            dry_run=payload.dry_run,
            max_items=payload.max_items,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    result["source_count"] = len(sources)
    result["source_url"] = payload.source_url
    result["source_type"] = payload.source_type
    return result


@app.get("/api/dictionary/export")
def api_export_dictionary(
    request: Request,
    include_archived: bool = False,
    include_banned: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    words = db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all()
    if not include_archived:
        words = [word for word in words if word.archived_at is None]
    if not include_banned:
        words = [word for word in words if word.banned_at is None]
    return dictionary_export_payload(words)


@app.get("/api/dictionary/import-sources")
def api_list_dictionary_import_sources(request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    sources = db.scalars(select(DictionaryImportSource).order_by(DictionaryImportSource.name)).all()
    return {"items": [dictionary_import_source_summary(source) for source in sources]}


@app.post("/api/dictionary/import-sources")
def api_create_dictionary_import_source(
    payload: DictionaryImportSourceWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    source = DictionaryImportSource(
        name=payload.name.strip(),
        source_url=payload.source_url.strip(),
        source_type=payload.source_type.strip() or "auto",
        source_label=payload.source_label.strip(),
        source_api_token=payload.source_api_token.strip(),
        sync_interval_hours=max(1, min(payload.sync_interval_hours, 24 * 30)),
        max_items=max(1, min(payload.max_items, 5000)),
        enabled=payload.enabled,
    )
    schedule_next_dictionary_import(source, utcnow())
    db.add(source)
    db.commit()
    db.refresh(source)
    if payload.sync_now:
        run_dictionary_import_source(db, source, dry_run=False)
        db.refresh(source)
    return dictionary_import_source_summary(source)


@app.post("/api/dictionary/import-sources/{source_id}/sync")
def api_sync_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    source = db.get(DictionaryImportSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Dictionary import source not found")
    result = run_dictionary_import_source(db, source, dry_run=False)
    return {"source": dictionary_import_source_summary(source), "result": result}


@app.delete("/api/dictionary/import-sources/{source_id}")
def api_delete_dictionary_import_source(source_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    source = db.get(DictionaryImportSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Dictionary import source not found")
    db.delete(source)
    db.commit()
    return {"deleted": True, "id": source_id}


@app.post("/api/dictionary")
def api_create_dictionary_word(payload: DictionaryWordWrite, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    word_key = normalize_dictionary_word(payload.word)
    if not word_key:
        raise HTTPException(status_code=400, detail="Word must contain at least one letter or number")
    existing = db.scalar(select(DictionaryWord).where(DictionaryWord.word == word_key))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Dictionary word already exists")
    word = DictionaryWord(
        word=word_key,
        display_text=payload.display_text.strip() or payload.word.strip(),
        clue=payload.clue.strip(),
        part_of_speech=payload.part_of_speech.strip(),
        properties=payload.properties,
    )
    word.tags = tags_for_ids(db, payload.tag_ids)
    db.add(word)
    db.flush()
    for index, definition_payload in enumerate(payload.definitions, start=1):
        definition = DictionaryDefinition(
            word=word,
            definition=definition_payload.definition.strip(),
            part_of_speech=definition_payload.part_of_speech.strip(),
            example=definition_payload.example.strip(),
            source="api",
            position=definition_payload.position or index,
            properties=definition_payload.properties,
        )
        db.add(definition)
        db.flush()
        apply_definition_payload(db, definition, definition_payload)
    db.commit()
    db.refresh(word)
    return serialize_dictionary_word(word)


@app.get("/api/dictionary/{word_id}")
def api_get_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    return serialize_dictionary_word(word)


@app.patch("/api/dictionary/{word_id}")
def api_update_dictionary_word(
    word_id: int,
    payload: DictionaryWordPatch,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    if payload.display_text is not None:
        word.display_text = payload.display_text.strip() or word.display_text
    if payload.clue is not None:
        word.clue = payload.clue.strip()
    if payload.part_of_speech is not None:
        word.part_of_speech = payload.part_of_speech.strip()
    if payload.properties is not None:
        word.properties = payload.properties
    if payload.tag_ids is not None:
        word.tags = tags_for_ids(db, payload.tag_ids)
    if payload.archived is not None:
        word.archived_at = datetime.now(timezone.utc) if payload.archived else None
    if payload.banned is not None:
        word.banned_at = datetime.now(timezone.utc) if payload.banned else None
    db.commit()
    db.refresh(word)
    return serialize_dictionary_word(word)


@app.delete("/api/dictionary/{word_id}")
def api_delete_dictionary_word(word_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    for definition in word.definitions:
        definition.tags = []
    word.tags = []
    db.delete(word)
    db.commit()
    return {"deleted": True, "id": word_id}


@app.post("/api/dictionary/{word_id}/definitions")
def api_create_dictionary_definition(
    word_id: int,
    payload: DictionaryDefinitionWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    word = db.get(DictionaryWord, word_id)
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    definition = DictionaryDefinition(
        word=word,
        definition=payload.definition.strip(),
        part_of_speech=payload.part_of_speech.strip(),
        example=payload.example.strip(),
        source="api",
        position=payload.position,
        properties=payload.properties,
    )
    db.add(definition)
    db.flush()
    apply_definition_payload(db, definition, payload)
    db.commit()
    db.refresh(definition)
    return serialize_definition(definition)


@app.patch("/api/dictionary/{word_id}/definitions/{definition_id}")
def api_update_dictionary_definition(
    word_id: int,
    definition_id: int,
    payload: DictionaryDefinitionPatch,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    definition = db.get(DictionaryDefinition, definition_id)
    if definition is None or definition.word_id != word_id:
        raise HTTPException(status_code=404, detail="Dictionary definition not found")
    apply_definition_payload(db, definition, payload)
    db.commit()
    db.refresh(definition)
    return serialize_definition(definition)


@app.delete("/api/dictionary/{word_id}/definitions/{definition_id}")
def api_delete_dictionary_definition(
    word_id: int,
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    definition = db.get(DictionaryDefinition, definition_id)
    if definition is None or definition.word_id != word_id:
        raise HTTPException(status_code=404, detail="Dictionary definition not found")
    definition.tags = []
    db.delete(definition)
    db.commit()
    return {"deleted": True, "id": definition_id}


@app.get("/api/tags")
def api_list_tags(
    request: Request,
    search: str = "",
    tag_type: str = "",
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    tags = db.scalars(select(PuzzleTag).order_by(PuzzleTag.tag_type, PuzzleTag.name)).all()
    if tag_type.strip():
        normalized_type = normalize_tag_type(tag_type)
        tags = [tag for tag in tags if tag.tag_type == normalized_type]
    if search.strip():
        search_text = search.strip().lower()
        tags = [
            tag for tag in tags
            if search_text in tag.name.lower() or search_text in tag.slug.lower() or search_text in tag.label.lower()
        ]
    total = len(tags)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_tag(tag) for tag in tags[offset : offset + limit]],
    }


@app.post("/api/tags")
def api_create_tag(payload: PuzzleTagWrite, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    normalized_type = normalize_tag_type(payload.tag_type)
    tag = PuzzleTag(
        name=payload.name.strip(),
        tag_type=normalized_type,
        slug=unique_slug(db, PuzzleTag, payload.slug or typed_tag_slug(normalized_type, payload.name)),
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return serialize_tag(tag)


@app.get("/api/tags/{tag_id}")
def api_get_tag(tag_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return serialize_tag(tag)


@app.patch("/api/tags/{tag_id}")
def api_update_tag(tag_id: int, payload: PuzzleTagPatch, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    new_name = payload.name.strip() if payload.name is not None else tag.name
    new_type = normalize_tag_type(payload.tag_type) if payload.tag_type is not None else tag.tag_type
    tag.name = new_name
    tag.tag_type = new_type
    if payload.slug is not None or payload.name is not None or payload.tag_type is not None:
        tag.slug = unique_slug(db, PuzzleTag, payload.slug or typed_tag_slug(new_type, new_name), tag.id)
    db.commit()
    db.refresh(tag)
    return serialize_tag(tag)


@app.delete("/api/tags/{tag_id}")
def api_delete_tag(tag_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    tag = db.get(PuzzleTag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag.puzzles = []
    tag.word_banks = []
    tag.dictionary_words = []
    tag.dictionary_definitions = []
    db.delete(tag)
    db.commit()
    return {"deleted": True, "id": tag_id}


@app.get("/api/word-banks")
def api_list_word_banks(
    request: Request,
    search: str = "",
    include_archived: bool = False,
    include_words: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    query = select(WordBank).order_by(WordBank.name)
    if not include_archived:
        query = query.where(WordBank.archived_at.is_(None))
    banks = db.scalars(query).all()
    if search.strip():
        search_text = search.strip().lower()
        banks = [
            bank for bank in banks
            if search_text in bank.name.lower() or search_text in bank.slug.lower() or search_text in bank.description.lower()
        ]
    total = len(banks)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [serialize_word_bank(bank, include_words=include_words) for bank in banks[offset : offset + limit]],
    }


@app.post("/api/word-banks")
def api_create_word_bank(payload: WordBankWrite, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    mode = payload.mode if payload.mode == "automatic" else "manual"
    bank = WordBank(
        name=payload.name.strip(),
        slug=unique_slug(db, WordBank, payload.slug or payload.name),
        description=payload.description.strip(),
        category_id=payload.category_id or None,
        payload={"mode": mode, "words": [], "properties": payload.properties},
    )
    bank.tags = tags_for_ids(db, payload.tag_ids)
    db.add(bank)
    db.flush()
    if payload.words:
        apply_word_bank_payload(db, bank, payload)
    db.commit()
    db.refresh(bank)
    return serialize_word_bank(bank, include_words=True)


@app.get("/api/word-banks/{bank_id}")
def api_get_word_bank(
    bank_id: int,
    request: Request,
    include_words: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    return serialize_word_bank(bank, include_words=include_words)


@app.patch("/api/word-banks/{bank_id}")
def api_update_word_bank(
    bank_id: int,
    payload: WordBankPatch,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    apply_word_bank_payload(db, bank, payload)
    db.commit()
    db.refresh(bank)
    return serialize_word_bank(bank, include_words=True)


@app.delete("/api/word-banks/{bank_id}")
def api_delete_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    for puzzle in bank.puzzles:
        puzzle.word_bank_id = None
    bank.tags = []
    db.delete(bank)
    db.commit()
    return {"deleted": True, "id": bank_id}


@app.post("/api/word-banks/{bank_id}/words")
def api_add_word_bank_word(
    bank_id: int,
    payload: WordBankEntryWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    bank = db.get(WordBank, bank_id)
    word = db.get(DictionaryWord, payload.word_id)
    definition = db.get(DictionaryDefinition, payload.definition_id) if payload.definition_id else None
    if bank is None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    if word is None:
        raise HTTPException(status_code=404, detail="Dictionary word not found")
    if definition is not None and definition.word_id != word.id:
        raise HTTPException(status_code=400, detail="Definition does not belong to the supplied word")
    existing = db.scalar(
        select(WordBankWord).where(WordBankWord.word_bank_id == bank_id, WordBankWord.word_id == word.id)
    )
    if existing is None:
        next_position = payload.position if payload.position is not None else len(bank.word_links) + 1
        existing = WordBankWord(word_bank=bank, word=word, definition=definition, position=next_position)
        db.add(existing)
    else:
        existing.definition = definition
        if payload.position is not None:
            existing.position = payload.position
    bank_payload = bank.payload if isinstance(bank.payload, dict) else {}
    bank_payload["mode"] = "manual"
    bank.payload = bank_payload
    db.commit()
    db.refresh(existing)
    return serialize_word_bank_link(existing)


@app.delete("/api/word-banks/{bank_id}/words/{link_id}")
def api_delete_word_bank_word(
    bank_id: int,
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    link = db.get(WordBankWord, link_id)
    if link is None or link.word_bank_id != bank_id:
        raise HTTPException(status_code=404, detail="Word bank entry not found")
    db.delete(link)
    db.commit()
    return {"deleted": True, "id": link_id}


def create_puzzle_record(payload: PuzzleGenerateRequest, db: Session, creator: User | None = None) -> PuzzleSummary:
    puzzle_type = get_puzzle_type(payload.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    if not is_puzzle_type_enabled(db, payload.puzzle_type):
        raise HTTPException(status_code=400, detail="This puzzle type is disabled")
    generation_options = validate_generation_options(puzzle_type, payload.generation_options)

    needs_word_bank = "word_bank" in puzzle_type.source_requirements
    selected_banks = []
    if needs_word_bank:
        selected_banks = word_banks_for_slugs(db, payload.word_bank_slugs)
        if not selected_banks and payload.theme:
            selected_banks = word_banks_for_slugs(db, [payload.theme])
    word_bank = selected_banks[0] if len(selected_banks) == 1 else None
    if needs_word_bank and not selected_banks:
        raise HTTPException(status_code=400, detail="This puzzle type requires a word bank source")
    generation_theme = word_bank_source_theme(selected_banks, payload.theme) if selected_banks else payload.theme
    excluded_tag_ids = set(payload.excluded_tag_ids)
    excluded_tags = tags_for_ids(db, payload.excluded_tag_ids)
    source_entries = combined_word_bank_entries(db, selected_banks, excluded_tag_ids) if selected_banks else []
    source_data = puzzle_type.source_builder(source_entries, payload.difficulty, excluded_tag_ids) if selected_banks else None
    if selected_banks and source_data is None:
        raise HTTPException(status_code=400, detail="No usable words remain after archive, ban, and tag exclusions")
    if payload.puzzle_type == "word_search":
        source_data = {
            "words": source_data,
            "blocked_words": blocked_words_for_generation(db, excluded_tag_ids),
        }
    generation_seed = normalize_seed(payload.seed)
    try:
        generated = puzzle_type.generator(generation_theme, payload.difficulty, source_data, generation_seed, generation_options)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    generated.metadata["excluded_tag_ids"] = sorted(excluded_tag_ids)
    generated.metadata["excluded_tag_slugs"] = [tag.slug for tag in excluded_tags]
    generated.metadata["seed"] = generation_seed
    if selected_banks:
        generated.metadata["word_bank_slugs"] = [bank.slug for bank in selected_banks]
        generated.metadata["word_bank_names"] = [bank.name for bank in selected_banks]
    puzzle = Puzzle(
        id=uuid.uuid4().hex[:12],
        puzzle_type=payload.puzzle_type,
        title=generated.title,
        theme=generation_theme,
        difficulty=payload.difficulty,
        word_bank_id=word_bank.id if word_bank is not None else None,
        prompt=generated.prompt,
        print_payload=generated.print_payload,
        play_payload=generated.play_payload,
        solution_payload=generated.solution_payload,
        puzzle_metadata=generated.metadata,
        auto_archive_at=default_auto_archive_at(),
        created_by_id=creator.id if creator is not None else None,
    )
    db.add(puzzle)
    db.flush()
    for index, hint_text in enumerate(generated.hints, start=1):
        db.add(PuzzleHint(puzzle_id=puzzle.id, order=index, text=hint_text))
    db.commit()
    db.refresh(puzzle)
    return serialize_puzzle(puzzle, db)


@app.post("/api/puzzles/generate", response_model=PuzzleSummary)
def create_puzzle(payload: PuzzleGenerateRequest, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    user = require_admin_api(request, db)
    return create_puzzle_record(payload, db, creator=user)


@app.get("/api/puzzle-types")
def list_puzzle_type_contracts(
    request: Request,
    include_disabled: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    return {"puzzle_types": list(puzzle_type_generation_schemas(db, include_disabled=include_disabled).values())}


@app.get("/api/puzzles")
def list_puzzles(request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_api_user(request, db)
    library_data = filtered_library(db, request, user, include_archived_for_admin=user.is_admin)
    return {
        "items": [
            serialize_library_item(
                puzzle,
                user,
                library_data["previews_by_puzzle"].get(puzzle.id, fallback_preview(puzzle.puzzle_type, puzzle.puzzle_type)),
                library_data["progress_by_puzzle"].get(puzzle.id),
                db,
            )
            for puzzle in library_data["puzzles"]
        ],
        "count": library_data["result_count"],
        "returned": len(library_data["puzzles"]),
        "has_more": library_data["has_more"],
        "next_limit": library_data["next_limit"] if library_data["has_more"] else None,
        "filters": library_data["filters"],
    }


@app.get("/api/puzzles/latest", response_model=PuzzleSummary)
def get_latest_puzzle(request: Request, printable: bool = False, db: Session = Depends(get_db)) -> PuzzleSummary:
    require_api_user(request, db)
    puzzles = db.scalars(select(Puzzle).where(Puzzle.archived_at.is_(None)).order_by(desc(Puzzle.created_at))).all()
    for puzzle in puzzles:
        puzzle_type = get_puzzle_type(puzzle.puzzle_type)
        if printable and (puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only):
            continue
        return serialize_puzzle(puzzle, db)
    raise HTTPException(status_code=404, detail="No printable puzzle found" if printable else "No puzzle found")


@app.get("/api/puzzles/export")
def api_export_puzzles(
    request: Request,
    ids: str = "",
    include_archived: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    require_admin_api(request, db)
    puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at))).all()
    if ids.strip():
        selected = {item.strip() for item in ids.split(",") if item.strip()}
        puzzles = [puzzle for puzzle in puzzles if puzzle.id in selected]
    if not include_archived:
        puzzles = [puzzle for puzzle in puzzles if puzzle.archived_at is None]
    return portable_puzzle_bundle(puzzles, db)


@app.post("/api/puzzles/import")
def api_import_puzzles(payload: PuzzleImportRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    user = require_admin_api(request, db)
    try:
        items = parse_puzzle_import_items(
            source_text=payload.source_text,
            source_url=payload.source_url,
            api_token=payload.source_api_token,
        )
        return import_portable_puzzles(
            db,
            items,
            creator=user,
            dry_run=payload.dry_run,
            duplicate_policy=payload.duplicate_policy,
            import_ids=payload.import_ids,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/puzzles/manual", response_model=PuzzleSummary)
def api_create_manual_puzzle(payload: PuzzleManualWrite, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    user = require_admin_api(request, db)
    try:
        puzzle = write_manual_puzzle(db, payload, user)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_puzzle(puzzle, db)


@app.patch("/api/puzzles/{puzzle_id}/manual", response_model=PuzzleSummary)
def api_update_manual_puzzle(
    puzzle_id: str,
    payload: PuzzleManualWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> PuzzleSummary:
    user = require_admin_api(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    try:
        puzzle = write_manual_puzzle(db, payload, user, puzzle)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return serialize_puzzle(puzzle, db)


@app.get("/api/puzzles/{puzzle_id}/export")
def api_export_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_api(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return portable_puzzle_item(puzzle, db)


@app.get("/api/puzzles/{puzzle_id}", response_model=PuzzleSummary)
def get_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return serialize_puzzle(puzzle, db)


@app.get("/api/puzzles/{puzzle_id}/print-preview", response_model=PuzzlePrintPreview)
def get_puzzle_print_preview(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzlePrintPreview:
    require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    printable = bool(puzzle_type is not None and puzzle_type.supports_print and not puzzle_type.digital_only)
    if not printable or puzzle_type is None:
        raise HTTPException(status_code=404, detail="This puzzle does not support printing")
    return PuzzlePrintPreview(
        id=puzzle.id,
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        printable=printable,
        layout=print_layout_context(puzzle, puzzle_type, db),
        url=puzzle_url(puzzle.id, db),
        print_url=puzzle_print_url(puzzle.id, db),
        render_url=puzzle_print_render_url(puzzle.id, db),
        pdf_url=f"{app_base_url(db)}/api/puzzles/{puzzle.id}/print.pdf",
    )


@app.get("/api/puzzles/{puzzle_id}/progress", response_model=PuzzleProgressResponse)
def get_progress(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleProgressResponse:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    progress = db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id == puzzle_id, PuzzleProgress.user_id == user.id))
    return progress_response(puzzle, user, progress)


@app.post("/api/puzzles/{puzzle_id}/progress", response_model=PuzzleProgressResponse)
def save_progress(
    puzzle_id: str,
    payload: PuzzleProgressUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> PuzzleProgressResponse:
    user = require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    progress = db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id == puzzle_id, PuzzleProgress.user_id == user.id))
    if progress is None:
        progress = PuzzleProgress(puzzle_id=puzzle_id, user_id=user.id, progress_payload={})
        db.add(progress)
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    progress.progress_payload = puzzle_type.progress_normalizer(payload, puzzle.solution_payload)
    db.commit()
    db.refresh(progress)
    return progress_response(puzzle, user, progress)


@app.post("/api/puzzles/{puzzle_id}/reset", response_model=PuzzleProgressResponse)
def reset_progress(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleProgressResponse:
    user = require_api_user(request, db)
    puzzle = reset_user_puzzle(puzzle_id, user.id, db)
    return progress_response(puzzle, user, None)


@app.post("/api/puzzles/{puzzle_id}/attempts", response_model=PuzzleAttemptResponse)
def submit_attempt(
    puzzle_id: str,
    payload: PuzzleAttemptCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> PuzzleAttemptResponse:
    user = require_api_user(request, db)
    user_id = payload.user_id if user.is_admin and payload.user_id is not None else user.id
    return submit_puzzle_attempt(puzzle_id, payload.answer, user_id, db)


def submit_puzzle_attempt(
    puzzle_id: str,
    answer: str,
    user_id: int | None,
    db: Session,
) -> PuzzleAttemptResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")
    is_correct, expected_count, matched_count = puzzle_type.answer_checker(answer, puzzle.solution_payload)

    db.add(
        PuzzleAttempt(
            puzzle_id=puzzle_id,
            user_id=user_id,
            answer=answer,
            is_correct=is_correct,
            attempt_metadata={"matched_count": matched_count, "expected_count": expected_count},
        )
    )
    db.commit()
    return PuzzleAttemptResponse(
        is_correct=is_correct,
        message="Correct." if is_correct else "Not quite. Try again.",
        expected_count=expected_count,
        matched_count=matched_count,
    )


@app.post("/api/puzzles/{puzzle_id}/photos")
def upload_photo(
    puzzle_id: str,
    request: Request,
    user_id: int | None = Form(None),
    notes: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, str | int]:
    user = require_api_user(request, db)
    photo_user_id = user_id if user.is_admin and user_id is not None else user.id
    return save_photo(puzzle_id, file, notes, photo_user_id, db)


def reset_user_puzzle(puzzle_id: str, user_id: int, db: Session) -> Puzzle:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    progress = db.scalar(select(PuzzleProgress).where(PuzzleProgress.puzzle_id == puzzle_id, PuzzleProgress.user_id == user_id))
    if progress is not None:
        db.delete(progress)
    attempts = db.scalars(select(PuzzleAttempt).where(PuzzleAttempt.puzzle_id == puzzle_id, PuzzleAttempt.user_id == user_id)).all()
    for attempt in attempts:
        db.delete(attempt)
    db.commit()
    return puzzle


def save_photo(
    puzzle_id: str,
    file: UploadFile,
    notes: str,
    user_id: int | None,
    db: Session,
) -> dict[str, str | int]:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    suffix = Path(file.filename or "upload.bin").suffix[:20]
    target_dir = UPLOAD_DIR / puzzle_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}{suffix}"
    with target.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    photo = PuzzlePhoto(
        puzzle_id=puzzle_id,
        user_id=user_id,
        file_path=str(target),
        original_filename=file.filename or "upload",
        notes=notes,
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)
    return {"id": photo.id, "status": "uploaded"}
