from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.models import Puzzle, PuzzleAttempt, PuzzleHint, PuzzlePhoto, User
from app.puzzles import check_word_search_answer, generate_word_search
from app.schemas import PuzzleAttemptCreate, PuzzleAttemptResponse, PuzzleGenerateRequest, PuzzleSummary, UserCreate


APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8790").rstrip("/")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-local-session-secret")
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Puzzary", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "puzzary"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    users = db.scalars(select(User).order_by(User.display_name)).all()
    return templates.TemplateResponse("login.html", {"request": request, "users": users})


@app.post("/login")
def login(
    request: Request,
    user_id: str = Form(""),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
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
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return library(request, db)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, db: Session = Depends(get_db)) -> HTMLResponse | RedirectResponse:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzles = db.scalars(select(Puzzle).order_by(desc(Puzzle.created_at)).limit(50)).all()
    users = db.scalars(select(User).order_by(User.display_name)).all()
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "puzzles": puzzles, "users": users, "current_user": user},
    )


@app.post("/web/puzzles/generate")
def web_generate_puzzle(
    request: Request,
    puzzle_type: str = Form("word_search"),
    theme: str = Form("daily_stripe"),
    difficulty: str = Form("easy"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    puzzle = create_puzzle(PuzzleGenerateRequest(puzzle_type=puzzle_type, theme=theme, difficulty=difficulty), db)
    return RedirectResponse(url=f"/puzzles/{puzzle.id}", status_code=303)


@app.post("/web/users")
def web_create_user(display_name: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    create_user(UserCreate(display_name=display_name), db)
    return RedirectResponse(url="/library", status_code=303)


@app.get("/puzzles/{puzzle_id}", response_class=HTMLResponse)
def puzzle_page(puzzle_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse | RedirectResponse:
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
    return templates.TemplateResponse(
        "puzzle.html",
        {"request": request, "puzzle": puzzle, "users": users, "attempts": attempts, "photos": photos, "current_user": user},
    )


@app.post("/web/puzzles/{puzzle_id}/attempts")
def web_attempt_puzzle(
    puzzle_id: str,
    request: Request,
    answer: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
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
) -> RedirectResponse:
    user = require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    save_photo(puzzle_id, file, notes, user.id, db)
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
    if payload.puzzle_type != "word_search":
        raise HTTPException(status_code=400, detail="Only word_search is implemented in the MVP")

    generated = generate_word_search(payload.theme, payload.difficulty)
    puzzle = Puzzle(
        id=uuid.uuid4().hex[:12],
        puzzle_type=payload.puzzle_type,
        title=generated.title,
        theme=payload.theme,
        difficulty=payload.difficulty,
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


@app.post("/api/puzzles/{puzzle_id}/attempts", response_model=PuzzleAttemptResponse)
def submit_attempt(
    puzzle_id: str,
    payload: PuzzleAttemptCreate,
    db: Session = Depends(get_db),
) -> PuzzleAttemptResponse:
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(status_code=404, detail="Puzzle not found")

    if puzzle.puzzle_type == "word_search":
        is_correct, expected_count, matched_count = check_word_search_answer(payload.answer, puzzle.solution_payload)
    else:
        raise HTTPException(status_code=400, detail="Unsupported puzzle type")

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
