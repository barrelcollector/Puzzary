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
    if "puzzles" not in inspector.get_table_names():
        return

    puzzle_columns = {column["name"] for column in inspector.get_columns("puzzles")}
    with engine.begin() as connection:
        if "archived_at" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE"))
        if "category_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN category_id INTEGER"))
