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
    completion_message: str = Field("", max_length=500)


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


class PuzzlePlayAction(BaseModel):
    kind: str = Field(..., min_length=1, max_length=40)
    target: str = Field("", max_length=120)
    changes: dict[str, str] = Field(default_factory=dict)


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
    completion_message: str = ""
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
    pdf_url: str = ""


class PuzzleImportRequest(BaseModel):
    source_url: str = ""
    source_text: str = ""
    source_api_token: str = ""
    dry_run: bool = True
    duplicate_policy: str = "skip"
    import_ids: list[str] = Field(default_factory=list)


class PuzzleManualWrite(BaseModel):
    puzzle_type: str = Field(..., min_length=1, max_length=40)
    title: str = Field(..., min_length=1, max_length=160)
    theme: str = Field("manual", max_length=80)
    difficulty: str = Field("manual", max_length=40)
    prompt: str = ""
    completion_message: str = Field("", max_length=500)
    print_payload: dict = Field(default_factory=dict)
    play_payload: dict = Field(default_factory=dict)
    solution_payload: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)
    draft: bool = True


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


class DictionaryImportRequest(BaseModel):
    source_url: str = Field(..., min_length=1)
    source_type: str = "auto"
    source_label: str = ""
    source_api_token: str = ""
    dry_run: bool = True
    tag_ids: list[int] = Field(default_factory=list)
    max_items: int = 500


class DictionaryImportSourceWrite(DictionaryImportRequest):
    name: str = Field(..., min_length=1, max_length=120)
    sync_interval_hours: int = 24
    enabled: bool = True
    sync_now: bool = False


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
