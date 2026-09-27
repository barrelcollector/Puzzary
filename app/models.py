from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    prompt: Mapped[str] = mapped_column(Text, default="")
    print_payload: Mapped[dict] = mapped_column(JSON)
    play_payload: Mapped[dict] = mapped_column(JSON)
    solution_payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(80), default="local")
    puzzle_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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


class ContentItem(Base):
    __tablename__ = "content_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_type: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), default="local")
    item_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
