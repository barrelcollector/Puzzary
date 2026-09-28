from __future__ import annotations

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=80)


class PuzzleGenerateRequest(BaseModel):
    puzzle_type: str = "word_search"
    theme: str = "daily_stripe"
    difficulty: str = "easy"


class PuzzleAttemptCreate(BaseModel):
    answer: str = Field(..., min_length=1)
    user_id: int | None = None


class PuzzleAttemptResponse(BaseModel):
    is_correct: bool
    message: str
    expected_count: int | None = None
    matched_count: int | None = None


class PuzzleProgressUpdate(BaseModel):
    found_words: list[str] = []
    answers: dict[str, str] = {}


class PuzzleProgressResponse(BaseModel):
    puzzle_id: str
    user_id: int
    found_words: list[str] = []
    answers: dict[str, str] = {}
    expected_count: int
    found_count: int


class PuzzleSummary(BaseModel):
    id: str
    puzzle_type: str
    title: str
    theme: str
    difficulty: str
    url: str
    print_payload: dict
    play_payload: dict
    hints: list[str] = []
