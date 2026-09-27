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


content_source_tag_links = Table(
    "content_source_tag_links",
    Base.metadata,
    Column("content_source_id", ForeignKey("content_sources.id"), primary_key=True),
    Column("tag_id", ForeignKey("puzzle_tags.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
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
    content_source_id: Mapped[int | None] = mapped_column(ForeignKey("content_sources.id"), nullable=True, index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    print_payload: Mapped[dict] = mapped_column(JSON)
    play_payload: Mapped[dict] = mapped_column(JSON)
    solution_payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(80), default="local")
    puzzle_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[PuzzleCategory | None] = relationship(back_populates="puzzles")
    content_source: Mapped[ContentSource | None] = relationship(back_populates="puzzles")
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


class ContentSource(Base):
    __tablename__ = "content_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(60), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("puzzle_categories.id"), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[PuzzleCategory | None] = relationship(back_populates="content_sources")
    tags: Mapped[list[PuzzleTag]] = relationship(secondary=content_source_tag_links, back_populates="content_sources")
    puzzles: Mapped[list[Puzzle]] = relationship(back_populates="content_source")


class PuzzleCategory(Base):
    __tablename__ = "puzzle_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzles: Mapped[list[Puzzle]] = relationship(back_populates="category")
    content_sources: Mapped[list[ContentSource]] = relationship(back_populates="category")


class PuzzleTag(Base):
    __tablename__ = "puzzle_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    puzzles: Mapped[list[Puzzle]] = relationship(secondary=puzzle_tag_links, back_populates="tags")
    content_sources: Mapped[list[ContentSource]] = relationship(secondary=content_source_tag_links, back_populates="tags")


class ContentItem(Base):
    __tablename__ = "content_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_type: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), default="local")
    item_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
