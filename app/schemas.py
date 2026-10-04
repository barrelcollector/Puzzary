from __future__ import annotations

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=80)


class PuzzleGenerateRequest(BaseModel):
    puzzle_type: str = "word_search"
    theme: str = "daily_stripe"
    word_bank_slugs: list[str] = Field(default_factory=list)
    difficulty: str = "easy"
    excluded_tag_ids: list[int] = Field(default_factory=list)
    seed: str | None = None
    generation_options: dict = Field(default_factory=dict)


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
    metadata: dict = Field(default_factory=dict)
    hints: list[str] = []


class PuzzlePrintPreview(BaseModel):
    id: str
    puzzle_type: str
    title: str
    printable: bool
    layout: dict = Field(default_factory=dict)
    url: str
    print_url: str
    render_url: str


class DictionaryDefinitionWrite(BaseModel):
    definition: str = Field(..., min_length=1)
    part_of_speech: str = ""
    clues: list[str] = Field(default_factory=list)
    example: str = ""
    position: int = 0
    tag_ids: list[int] = Field(default_factory=list)
    properties: dict = Field(default_factory=dict)


class DictionaryWordWrite(BaseModel):
    word: str = Field(..., min_length=1, max_length=80)
    display_text: str = ""
    clue: str = ""
    part_of_speech: str = ""
    tag_ids: list[int] = Field(default_factory=list)
    properties: dict = Field(default_factory=dict)
    definitions: list[DictionaryDefinitionWrite] = Field(default_factory=list)


class DictionaryWordPatch(BaseModel):
    display_text: str | None = None
    clue: str | None = None
    part_of_speech: str | None = None
    tag_ids: list[int] | None = None
    properties: dict | None = None
    archived: bool | None = None
    banned: bool | None = None


class DictionaryDefinitionPatch(BaseModel):
    definition: str | None = None
    part_of_speech: str | None = None
    clues: list[str] | None = None
    example: str | None = None
    position: int | None = None
    tag_ids: list[int] | None = None
    properties: dict | None = None
    archived: bool | None = None


class PuzzleTagWrite(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    tag_type: str = "General"
    slug: str = ""


class PuzzleTagPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    tag_type: str | None = None
    slug: str | None = None


class WordBankWrite(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: str = ""
    description: str = ""
    mode: str = "manual"
    category_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)
    words: list[str] = Field(default_factory=list)
    properties: dict = Field(default_factory=dict)


class WordBankPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = None
    description: str | None = None
    mode: str | None = None
    category_id: int | None = None
    tag_ids: list[int] | None = None
    words: list[str] | None = None
    properties: dict | None = None
    archived: bool | None = None


class WordBankEntryWrite(BaseModel):
    word_id: int
    definition_id: int | None = None
    position: int | None = None
