from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.content import THEMES
from app.database import SessionLocal, ensure_schema, get_db, init_db
from app.models import (
    DictionaryDefinition,
    DictionaryWord,
    Puzzle,
    PuzzleAttempt,
    PuzzleCategory,
    PuzzleDictionary,
    PuzzleHint,
    PuzzlePhoto,
    PuzzleProgress,
    PuzzleTag,
    User,
    WordBank,
    WordBankWord,
)
from app.puzzles import get_puzzle_type, list_puzzle_types
from app.schemas import (
    DictionaryDefinitionPatch,
    DictionaryDefinitionWrite,
    DictionaryWordPatch,
    DictionaryWordWrite,
    PuzzleAttemptCreate,
    PuzzleAttemptResponse,
    PuzzleGenerateRequest,
    PuzzleProgressResponse,
    PuzzleProgressUpdate,
    PuzzleSummary,
    UserCreate,
)


APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8790").rstrip("/")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-local-session-secret")
PUZZARY_TIMEZONE = os.getenv("PUZZARY_TIMEZONE", "America/New_York")
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Puzzary", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", max_age=60 * 60 * 24 * 7)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

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


templates.env.globals["local_datetime_value"] = local_datetime_value
templates.env.globals["local_datetime_label"] = local_datetime_label
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
    app.state.auto_archive_task = asyncio.create_task(auto_archive_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    task = getattr(app.state, "auto_archive_task", None)
    if task is not None:
        task.cancel()


def puzzle_url(puzzle_id: str) -> str:
    return f"{APP_BASE_URL}/puzzles/{puzzle_id}"


def serialize_puzzle(puzzle: Puzzle) -> PuzzleSummary:
    return PuzzleSummary(
        id=puzzle.id,
        puzzle_type=puzzle.puzzle_type,
        title=puzzle.title,
        theme=puzzle.theme,
        difficulty=puzzle.difficulty,
        url=puzzle_url(puzzle.id),
        print_payload=puzzle.print_payload,
        play_payload=puzzle.play_payload,
        metadata=puzzle.puzzle_metadata or {},
        hints=[hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
    )


def serialize_generation_field(field) -> dict:
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
    }


def puzzle_type_generation_schemas() -> dict[str, dict]:
    schemas = {}
    for puzzle_type in list_puzzle_types():
        needs_word_bank = "word_bank" in puzzle_type.source_requirements
        schemas[puzzle_type.slug] = {
            "slug": puzzle_type.slug,
            "label": puzzle_type.label,
            "description": puzzle_type.description,
            "needs_word_bank": needs_word_bank,
            "theme_mode": puzzle_type.theme_mode,
            "source_requirements": list(puzzle_type.source_requirements),
            "generation_fields": [serialize_generation_field(field) for field in puzzle_type.generation_fields],
        }
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


async def auto_archive_loop() -> None:
    while True:
        with SessionLocal() as db:
            apply_due_auto_archives(db)
        await asyncio.sleep(60)


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


def setup_url(token: str) -> str:
    return f"{APP_BASE_URL}/password/setup?token={token}"


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
        return RedirectResponse(url="/login", status_code=303)
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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "current_user": current_user(request, db),
            "error": request.query_params.get("error", ""),
            "message": request.query_params.get("message", ""),
        },
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    if login_limited(request, username):
        return RedirectResponse(url="/login?error=Too%20many%20attempts.%20Try%20again%20soon.", status_code=303)
    user = find_login_user(db, username)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        record_login_failure(request, username)
        return RedirectResponse(url="/login?error=Invalid%20username%20or%20password.", status_code=303)

    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version or 1
    user.last_login_at = utcnow()
    db.commit()
    clear_login_failures(request, username)
    if user.must_change_password:
        return RedirectResponse(url="/account/password?message=Please%20set%20a%20new%20password.", status_code=303)
    return RedirectResponse(url="/library", status_code=303)


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
    puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at)).limit(50)).all()
    puzzles = [puzzle for puzzle in puzzles if puzzle.archived_at is None]
    progress_rows = db.scalars(select(PuzzleProgress).where(PuzzleProgress.user_id == user.id)).all()
    progress_by_puzzle = {
        puzzle.id: {"found": 0, "total": progress_response(puzzle, user, None).expected_count}
        for puzzle in puzzles
        if get_puzzle_type(puzzle.puzzle_type) is not None
    }
    for progress in progress_rows:
        if progress.puzzle_id in progress_by_puzzle:
            puzzle = next((item for item in puzzles if item.id == progress.puzzle_id), None)
            if puzzle is not None:
                progress_by_puzzle[progress.puzzle_id]["found"] = progress_response(puzzle, user, progress).found_count
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "puzzles": puzzles,
            "current_user": user,
            "progress_by_puzzle": progress_by_puzzle,
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
            "puzzle_types": list_puzzle_types(),
            "generation_schemas": puzzle_type_generation_schemas(),
        },
    )


@app.post("/web/puzzles/generate")
async def web_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    theme: str = Form(""),
    word_bank_slugs: list[str] = Form(default=[]),
    difficulty: str = Form("easy"),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    puzzle_type_config = get_puzzle_type(puzzle_type)
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
            generation_options=generation_options_from_form(form),
        ),
        db,
    )
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
    admin_users = db.scalars(select(User).order_by(User.display_name)).all()
    return {
        "request": request,
        "current_user": user,
        "section": section,
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
        "admin_users": admin_users,
        "selected_word": selected_word,
        "selected_word_bank": selected_word_bank,
        "selected_tag": selected_tag,
        "puzzle_types": list_puzzle_types(),
        "generation_schemas": puzzle_type_generation_schemas(),
    }


def render_admin(
    request: Request,
    db: Session,
    section: str,
    selected_word: DictionaryWord | None = None,
    selected_word_bank: WordBank | None = None,
    selected_tag: PuzzleTag | None = None,
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "admin.html",
        admin_context(request, db, user, section, selected_word, selected_word_bank, selected_tag),
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "dashboard")


@app.get("/admin/puzzles", response_class=HTMLResponse)
def admin_puzzles_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "puzzles")


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
    if puzzle_type_config is None:
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


def render_puzzle_print_page(
    puzzle_id: str,
    request: Request,
    db: Session,
    user: User | None,
    show_toolbar: bool,
) -> HTMLResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    puzzle_type = get_puzzle_type(puzzle.puzzle_type)
    if puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only:
        raise HTTPException(status_code=404, detail="This puzzle does not support printing")
    return templates.TemplateResponse(
        "print.html",
        {
            "request": request,
            "puzzle": puzzle,
            "current_user": user,
            "puzzle_type_definition": puzzle_type,
            "print_payload": puzzle.print_payload or {},
            "print_gimme_cells": printable_gimme_cells(puzzle),
            "print_gimme_words": printable_gimme_words(puzzle),
            "show_print_toolbar": show_toolbar,
            "hide_app_chrome": not show_toolbar,
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
    submit_puzzle_attempt(puzzle_id, answer, user.id, db)
    return RedirectResponse(url=f"/puzzles/{puzzle_id}", status_code=303)


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
        "setup_url": setup_url(setup_token) if setup_token else "",
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
    db.delete(definition)
    db.commit()
    return {"deleted": True, "id": definition_id}


def create_puzzle_record(payload: PuzzleGenerateRequest, db: Session) -> PuzzleSummary:
    puzzle_type = get_puzzle_type(payload.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")

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
        generated = puzzle_type.generator(generation_theme, payload.difficulty, source_data, generation_seed, payload.generation_options)
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
    )
    db.add(puzzle)
    db.flush()
    for index, hint_text in enumerate(generated.hints, start=1):
        db.add(PuzzleHint(puzzle_id=puzzle.id, order=index, text=hint_text))
    db.commit()
    db.refresh(puzzle)
    return serialize_puzzle(puzzle)


@app.post("/api/puzzles/generate", response_model=PuzzleSummary)
def create_puzzle(payload: PuzzleGenerateRequest, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    require_admin_api(request, db)
    return create_puzzle_record(payload, db)


@app.get("/api/puzzles/latest", response_model=PuzzleSummary)
def get_latest_puzzle(request: Request, printable: bool = False, db: Session = Depends(get_db)) -> PuzzleSummary:
    require_api_user(request, db)
    puzzles = db.scalars(select(Puzzle).where(Puzzle.archived_at.is_(None)).order_by(desc(Puzzle.created_at))).all()
    for puzzle in puzzles:
        puzzle_type = get_puzzle_type(puzzle.puzzle_type)
        if printable and (puzzle_type is None or not puzzle_type.supports_print or puzzle_type.digital_only):
            continue
        return serialize_puzzle(puzzle)
    raise HTTPException(status_code=404, detail="No printable puzzle found" if printable else "No puzzle found")


@app.get("/api/puzzles/{puzzle_id}", response_model=PuzzleSummary)
def get_puzzle(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleSummary:
    require_api_user(request, db)
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return serialize_puzzle(puzzle)


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
    is_correct, expected_count, matched_count = puzzle_type.answer_checker(payload.answer, puzzle.solution_payload)

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
