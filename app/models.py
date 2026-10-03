from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Table, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


puzzle_tag_links = Table(
    "puzzle_tag_links",
    Base.metadata,
    Column("puzzle_id", ForeignKey("puzzles.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


word_bank_tag_links = Table(
    "word_bank_tag_links",
    Base.metadata,
    Column("word_bank_id", ForeignKey("word_banks.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


dictionary_word_tag_links = Table(
    "dictionary_word_tag_links",
    Base.metadata,
    Column("word_id", ForeignKey("dictionary_words.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


dictionary_definition_tag_links = Table(
    "dictionary_definition_tag_links",
    Base.metadata,
    Column("definition_id", ForeignKey("dictionary_definitions.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    reset_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    api_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    api_token_prefix: Mapped[str] = mapped_column(String(12), default="")
    api_token_last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    attempts: Mapped[list[PuzzleAttempt]] = relationship(back_populates="user")
    progress: Mapped[list[PuzzleProgress]] = relationship(back_populates="user")
    photos: Mapped[list[PuzzlePhoto]] = relationship(back_populates="user")


class Puzzle(Base):
    __tablename__ = "puzzles"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    puzzle_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(160))
    theme: Mapped[str] = mapped_column(String(80), index=True)
    difficulty: Mapped[str] = mapped_column(String(40), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("puzzle_categories.id"), nullable=True, index=True)
    word_bank_id: Mapped[int | None] = mapped_column(ForeignKey("word_banks.id"), nullable=True, index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    print_payload: Mapped[dict] = mapped_column(JSON)
    play_payload: Mapped[dict] = mapped_column(JSON)
    solution_payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(80), default="local")
    puzzle_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[PuzzleCategory | None] = relationship(back_populates="puzzles")
    word_bank: Mapped[WordBank | None] = relationship(back_populates="puzzles")
    tags: Mapped[list[PuzzleTag]] = relationship(secondary=puzzle_tag_links, back_populates="puzzles")
    hints: Mapped[list[PuzzleHint]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")
    attempts: Mapped[list[PuzzleAttempt]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")
    progress: Mapped[list[PuzzleProgress]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")
    photos: Mapped[list[PuzzlePhoto]] = relationship(back_populates="puzzle", cascade="all, delete-orphan")


class PuzzleHint(Base):
    __tablename__ = "puzzle_hints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    order: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="hints")


class PuzzleAttempt(Base):
    __tablename__ = "puzzle_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    answer: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="attempts")
    user: Mapped[User | None] = relationship(back_populates="attempts")


class PuzzleProgress(Base):
    __tablename__ = "puzzle_progress"
    __table_args__ = (UniqueConstraint("puzzle_id", "user_id", name="uq_puzzle_progress_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    progress_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="progress")
    user: Mapped[User] = relationship(back_populates="progress")


class PuzzlePhoto(Base):
    __tablename__ = "puzzle_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[str] = mapped_column(ForeignKey("puzzles.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzle: Mapped[Puzzle] = relationship(back_populates="photos")
    user: Mapped[User | None] = relationship(back_populates="photos")


class PuzzleDictionary(Base):
    __tablename__ = "puzzle_dictionaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    words: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WordBank(Base):
    __tablename__ = "word_banks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("puzzle_categories.id"), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[PuzzleCategory | None] = relationship(back_populates="word_banks")
    tags: Mapped[list[PuzzleTag]] = relationship(secondary=word_bank_tag_links, back_populates="word_banks")
    word_links: Mapped[list[WordBankWord]] = relationship(
        back_populates="word_bank",
        cascade="all, delete-orphan",
        order_by="WordBankWord.position",
    )
    puzzles: Mapped[list[Puzzle]] = relationship(back_populates="word_bank")


class DictionaryWord(Base):
    __tablename__ = "dictionary_words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_text: Mapped[str] = mapped_column(String(80))
    clue: Mapped[str] = mapped_column(Text, default="")
    part_of_speech: Mapped[str] = mapped_column(String(40), default="")
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tags: Mapped[list[PuzzleTag]] = relationship(secondary=dictionary_word_tag_links, back_populates="dictionary_words")
    bank_links: Mapped[list[WordBankWord]] = relationship(back_populates="word", cascade="all, delete-orphan")
    definitions: Mapped[list[DictionaryDefinition]] = relationship(
        back_populates="word",
        cascade="all, delete-orphan",
        order_by="DictionaryDefinition.position",
    )


class DictionaryDefinition(Base):
    __tablename__ = "dictionary_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("dictionary_words.id"), index=True)
    definition: Mapped[str] = mapped_column(Text)
    part_of_speech: Mapped[str] = mapped_column(String(40), default="")
    example: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(120), default="local")
    position: Mapped[int] = mapped_column(Integer, default=0)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    word: Mapped[DictionaryWord] = relationship(back_populates="definitions")
    tags: Mapped[list[PuzzleTag]] = relationship(secondary=dictionary_definition_tag_links, back_populates="dictionary_definitions")


class WordBankWord(Base):
    __tablename__ = "word_bank_words"
    __table_args__ = (UniqueConstraint("word_bank_id", "word_id", name="uq_word_bank_word"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word_bank_id: Mapped[int] = mapped_column(ForeignKey("word_banks.id"), index=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("dictionary_words.id"), index=True)
    definition_id: Mapped[int | None] = mapped_column(ForeignKey("dictionary_definitions.id"), nullable=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    word_bank: Mapped[WordBank] = relationship(back_populates="word_links")
    word: Mapped[DictionaryWord] = relationship(back_populates="bank_links")
    definition: Mapped[DictionaryDefinition | None] = relationship()


class PuzzleCategory(Base):
    __tablename__ = "puzzle_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzles: Mapped[list[Puzzle]] = relationship(back_populates="category")
    word_banks: Mapped[list[WordBank]] = relationship(back_populates="category")


class PuzzleTag(Base):
    __tablename__ = "puzzle_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    tag_type: Mapped[str] = mapped_column(String(80), default="General", index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzles: Mapped[list[Puzzle]] = relationship(secondary=puzzle_tag_links, back_populates="tags")
    word_banks: Mapped[list[WordBank]] = relationship(secondary=word_bank_tag_links, back_populates="tags")
    dictionary_words: Mapped[list[DictionaryWord]] = relationship(secondary=dictionary_word_tag_links, back_populates="tags")
    dictionary_definitions: Mapped[list[DictionaryDefinition]] = relationship(
        secondary=dictionary_definition_tag_links,
        back_populates="tags",
    )

    @property
    def label(self) -> str:
        if not self.tag_type or self.tag_type == "General":
            return self.name
        return f"{self.tag_type}: {self.name}"


class ContentItem(Base):
    __tablename__ = "content_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_type: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), default="local")
    item_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
