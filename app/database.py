from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://puzzary:puzzary@db:5432/puzzary")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def ensure_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "content_sources" in table_names and "word_banks" not in table_names:
            connection.execute(text("ALTER TABLE content_sources RENAME TO word_banks"))
        if "content_source_words" in table_names and "word_bank_words" not in table_names:
            connection.execute(text("ALTER TABLE content_source_words RENAME TO word_bank_words"))
        if "content_source_tag_links" in table_names and "word_bank_tag_links" not in table_names:
            connection.execute(text("ALTER TABLE content_source_tag_links RENAME TO word_bank_tag_links"))

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "puzzles" not in table_names:
        return

    puzzle_columns = {column["name"] for column in inspector.get_columns("puzzles")}
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    dictionary_word_columns = {column["name"] for column in inspector.get_columns("dictionary_words")} if "dictionary_words" in table_names else set()
    with engine.begin() as connection:
        if "archived_at" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE"))
        if "category_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN category_id INTEGER"))
        if "content_source_id" in puzzle_columns and "word_bank_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles RENAME COLUMN content_source_id TO word_bank_id"))
        elif "word_bank_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN word_bank_id INTEGER"))
        if "is_admin" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"))
        if "dictionary_words" in table_names:
            if "archived_at" not in dictionary_word_columns:
                connection.execute(text("ALTER TABLE dictionary_words ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE"))
            if "banned_at" not in dictionary_word_columns:
                connection.execute(text("ALTER TABLE dictionary_words ADD COLUMN banned_at TIMESTAMP WITH TIME ZONE"))

        if "word_banks" in table_names:
            word_bank_columns = {column["name"] for column in inspector.get_columns("word_banks")}
            if "source_type" in word_bank_columns:
                connection.execute(text("ALTER TABLE word_banks DROP COLUMN source_type"))

        if "word_bank_words" in table_names:
            word_bank_word_columns = {column["name"] for column in inspector.get_columns("word_bank_words")}
            if "content_source_id" in word_bank_word_columns and "word_bank_id" not in word_bank_word_columns:
                connection.execute(text("ALTER TABLE word_bank_words RENAME COLUMN content_source_id TO word_bank_id"))

        if "word_bank_tag_links" in table_names:
            word_bank_tag_columns = {column["name"] for column in inspector.get_columns("word_bank_tag_links")}
            if "content_source_id" in word_bank_tag_columns and "word_bank_id" not in word_bank_tag_columns:
                connection.execute(text("ALTER TABLE word_bank_tag_links RENAME COLUMN content_source_id TO word_bank_id"))
