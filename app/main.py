from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Puzzary", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def tag_value(tags: list[PuzzleTag], tag_type: str) -> str:
    for tag in tags:
        if tag.tag_type == tag_type:
            return tag.name
    return ""


templates.env.globals["tag_value"] = tag_value


@app.on_event("startup")
def startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ensure_schema()
    init_db()
    with SessionLocal() as db:
        ensure_admin_user(db)
        seed_word_banks(db)
        seed_word_definitions(db)


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
        hints=[hint.text for hint in sorted(puzzle.hints, key=lambda item: item.order)],
    )


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


def ensure_admin_user(db: Session) -> None:
    has_admin = db.scalar(select(User).where(User.is_admin == True))  # noqa: E712
    if has_admin is not None:
        return
    first_user = db.scalar(select(User).order_by(User.created_at, User.id))
    if first_user is not None:
        first_user.is_admin = True
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
    for tag_type, value in (("Part of Speech", part_of_speech_tag), ("Clue", clue_tag)):
        tag = get_or_create_tag(db, tag_type, value)
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


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, int(user_id))


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
    users = db.scalars(select(User).order_by(User.display_name)).all()
    return templates.TemplateResponse("login.html", {"request": request, "users": users})


@app.post("/login")
def login(
    request: Request,
    user_id: str = Form(""),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
):
    user: User | None = None
    if user_id:
        user = db.get(User, int(user_id))
    elif display_name.strip():
        result = create_user(UserCreate(display_name=display_name.strip()), db)
        user = db.get(User, int(result["id"]))

    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    request.session["user_id"] = user.id
    return RedirectResponse(url="/library", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


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
    users = db.scalars(select(User).order_by(User.display_name)).all()
    word_banks = word_bank_options(db)
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
            "users": users,
            "current_user": user,
            "progress_by_puzzle": progress_by_puzzle,
            "word_banks": word_banks,
            "puzzle_types": list_puzzle_types(),
        },
    )


@app.post("/web/puzzles/generate")
def web_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    theme: str = Form("daily_stripe"),
    difficulty: str = Form("easy"),
    db: Session = Depends(get_db),
):
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = create_puzzle(PuzzleGenerateRequest(puzzle_type=puzzle_type, theme=theme, difficulty=difficulty), db)
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/web/users")
def web_create_user(display_name: str = Form(...), db: Session = Depends(get_db)):
    create_user(UserCreate(display_name=display_name), db)
    return RedirectResponse(url="/library", status_code=303)


def admin_context(request: Request, db: Session, user: User, section: str, selected_word: DictionaryWord | None = None) -> dict:
    puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at))).all()
    all_word_banks = db.scalars(select(WordBank).order_by(WordBank.name)).all()
    word_banks = [bank for bank in all_word_banks if bank.archived_at is None]
    categories = db.scalars(select(PuzzleCategory).order_by(PuzzleCategory.name)).all()
    tags = db.scalars(select(PuzzleTag).order_by(PuzzleTag.tag_type, PuzzleTag.name)).all()
    attempts = db.scalars(select(PuzzleAttempt).order_by(desc(PuzzleAttempt.created_at)).limit(20)).all()
    progress_rows = db.scalars(select(PuzzleProgress).order_by(desc(PuzzleProgress.updated_at)).limit(20)).all()
    photos = db.scalars(select(PuzzlePhoto).order_by(desc(PuzzlePhoto.created_at)).limit(20)).all()
    dictionary_words = db.scalars(select(DictionaryWord).order_by(DictionaryWord.word)).all()
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
        "categories": categories,
        "tags": tags,
        "attempts": attempts,
        "progress_rows": progress_rows,
        "photos": photos,
        "dictionary_words": dictionary_words,
        "selected_word": selected_word,
        "puzzle_types": list_puzzle_types(),
    }


def render_admin(request: Request, db: Session, section: str, selected_word: DictionaryWord | None = None):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "admin.html",
        admin_context(request, db, user, section, selected_word),
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


@app.get("/admin/activity", response_class=HTMLResponse)
def admin_activity_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "activity")


@app.get("/admin/publishing", response_class=HTMLResponse)
def admin_publishing_page(request: Request, db: Session = Depends(get_db)):
    return render_admin(request, db, "publishing")


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
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is not None:
        puzzle.category_id = category_id or None
        puzzle.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
        db.commit()
    return RedirectResponse(url="/admin/puzzles", status_code=303)


@app.post("/admin/generate")
def admin_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    bank_id: int = Form(...),
    difficulty: str = Form("easy"),
    category_id: int = Form(0),
    tag_ids: list[int] = Form(default=[]),
    excluded_tag_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is None or bank.archived_at is not None:
        raise HTTPException(status_code=404, detail="Word bank not found")
    puzzle = create_puzzle(
        PuzzleGenerateRequest(
            puzzle_type=puzzle_type,
            theme=bank.slug,
            difficulty=difficulty,
            excluded_tag_ids=excluded_tag_ids,
        ),
        db,
    )
    saved_puzzle = db.get(Puzzle, puzzle.id)
    if saved_puzzle is not None:
        saved_puzzle.category_id = category_id or bank.category_id
        saved_puzzle.tags = tags_for_ids(db, tag_ids) if tag_ids else list(bank.tags)
        db.commit()
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/admin/word-banks")
def admin_create_word_bank(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
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
        payload={"words": parse_words(words)},
    )
    bank.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
    db.add(bank)
    db.flush()
    sync_word_bank_words(db, bank, parse_words(words))
    db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/word-banks/{bank_id}")
def admin_update_word_bank(
    bank_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
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
        bank.payload = {"words": parsed_words}
        bank.tags = db.scalars(select(PuzzleTag).where(PuzzleTag.id.in_(tag_ids))).all() if tag_ids else []
        sync_word_bank_words(db, bank, parsed_words)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/word-banks/{bank_id}/archive")
def admin_archive_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.archived_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


@app.post("/admin/word-banks/{bank_id}/unarchive")
def admin_unarchive_word_bank(bank_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    bank = db.get(WordBank, bank_id)
    if bank is not None:
        bank.archived_at = None
        db.commit()
    return RedirectResponse(url="/admin/word-banks", status_code=303)


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
    db.add(
        PuzzleTag(
            name=name.strip(),
            tag_type=normalized_type,
            slug=unique_slug(db, PuzzleTag, slug or typed_tag_slug(normalized_type, name)),
        )
    )
    db.commit()
    return RedirectResponse(url="/admin/tags", status_code=303)


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
    return RedirectResponse(url="/admin/tags", status_code=303)


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
            "playable_puzzle_type": get_puzzle_type(puzzle.puzzle_type) is not None,
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
    submit_attempt(puzzle_id, PuzzleAttemptCreate(answer=answer, user_id=user.id), db)
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
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> dict[str, int | str]:
    existing = db.scalar(select(User).where(User.display_name == payload.display_name))
    if existing:
        return {"id": existing.id, "display_name": existing.display_name}
    user = User(display_name=payload.display_name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "display_name": user.display_name}


@app.post("/api/puzzles/generate", response_model=PuzzleSummary)
def create_puzzle(payload: PuzzleGenerateRequest, db: Session = Depends(get_db)) -> PuzzleSummary:
    puzzle_type = get_puzzle_type(payload.puzzle_type)
    if puzzle_type is None:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")

    word_bank = word_bank_for_slug(db, payload.theme)
    excluded_tag_ids = set(payload.excluded_tag_ids)
    excluded_tags = tags_for_ids(db, payload.excluded_tag_ids)
    source_data = puzzle_type.source_builder(word_bank, payload.difficulty, excluded_tag_ids)
    if word_bank is not None and source_data is None:
        raise HTTPException(status_code=400, detail="No usable words remain after archive, ban, and tag exclusions")
    if payload.puzzle_type == "word_search":
        source_data = {
            "words": source_data,
            "blocked_words": blocked_words_for_generation(db, excluded_tag_ids),
        }
    generated = puzzle_type.generator(payload.theme, payload.difficulty, source_data)
    generated.metadata["excluded_tag_ids"] = sorted(excluded_tag_ids)
    generated.metadata["excluded_tag_slugs"] = [tag.slug for tag in excluded_tags]
    puzzle = Puzzle(
        id=uuid.uuid4().hex[:12],
        puzzle_type=payload.puzzle_type,
        title=generated.title,
        theme=payload.theme,
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


@app.get("/api/puzzles/{puzzle_id}", response_model=PuzzleSummary)
def get_puzzle(puzzle_id: str, db: Session = Depends(get_db)) -> PuzzleSummary:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")
    return serialize_puzzle(puzzle)


@app.get("/api/puzzles/{puzzle_id}/progress", response_model=PuzzleProgressResponse)
def get_progress(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> PuzzleProgressResponse:
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
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
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
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
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    puzzle = reset_user_puzzle(puzzle_id, user.id, db)
    return progress_response(puzzle, user, None)


@app.post("/api/puzzles/{puzzle_id}/attempts", response_model=PuzzleAttemptResponse)
def submit_attempt(
    puzzle_id: str,
    payload: PuzzleAttemptCreate,
    db: Session = Depends(get_db),
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
            user_id=payload.user_id,
            answer=payload.answer,
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
    user_id: int | None = Form(None),
    notes: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, str | int]:
    return save_photo(puzzle_id, file, notes, user_id, db)


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
