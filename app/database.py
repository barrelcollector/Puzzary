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
    dictionary_definition_columns = {column["name"] for column in inspector.get_columns("dictionary_definitions")} if "dictionary_definitions" in table_names else set()
    tag_columns = {column["name"] for column in inspector.get_columns("puzzle_tags")} if "puzzle_tags" in table_names else set()
    with engine.begin() as connection:
        if 'permissions_initialized' not in user_columns:
            connection.execute(text('ALTER TABLE users ADD COLUMN permissions_initialized BOOLEAN NOT NULL DEFAULT FALSE'))
        if 'permission_overrides' not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN permission_overrides JSON NOT NULL DEFAULT '{}'"))
        if 'appearance_theme' not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN appearance_theme VARCHAR(80) NOT NULL DEFAULT ''"))
        if "group_solves" in table_names:
            group_columns = {c["name"] for c in inspector.get_columns("group_solves")}
            if "puzzle_states" not in group_columns:
                connection.execute(text("ALTER TABLE group_solves ADD COLUMN puzzle_states JSON NOT NULL DEFAULT '{}'"))
            if "puzzle_started_revision" not in group_columns:
                connection.execute(text("ALTER TABLE group_solves ADD COLUMN puzzle_started_revision INTEGER NOT NULL DEFAULT 0"))
        if "group_solves" in table_names and "allow_guests" not in {c["name"] for c in inspector.get_columns("group_solves")}:
            connection.execute(text("ALTER TABLE group_solves ADD COLUMN allow_guests BOOLEAN NOT NULL DEFAULT FALSE"))
        if "group_solves" in table_names and "password_hash" not in {c["name"] for c in inspector.get_columns("group_solves")}:
            connection.execute(text("ALTER TABLE group_solves ADD COLUMN password_hash VARCHAR(255)"))
        for table, additions in {
            "group_members": {"guest_name": "VARCHAR(80)", "guest_token_hash": "VARCHAR(64)"},
            "group_actions": {"guest_member_id": "INTEGER", "target": "VARCHAR(80) NOT NULL DEFAULT ''", "contributions": "JSON NOT NULL DEFAULT '[]'"},
        }.items():
            if table not in table_names:
                continue
            columns = {c["name"]: c for c in inspector.get_columns(table)}
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
            if not columns["user_id"]["nullable"]:
                if engine.dialect.name == "sqlite":
                    # SQLite cannot drop NOT NULL in place. Rebuild these leaf tables,
                    # preserving every existing row and recreating model constraints.
                    from sqlalchemy.schema import CreateTable, CreateIndex
                    from sqlalchemy import MetaData
                    from app.models import GroupMember, GroupAction
                    model = GroupMember if table == "group_members" else GroupAction
                    metadata = MetaData()
                    for original in Base.metadata.sorted_tables:
                        original.to_metadata(metadata)
                    temporary = model.__table__.to_metadata(metadata, name=f"{table}_guest_migration")
                    connection.execute(CreateTable(temporary))
                    old_names = [c["name"] for c in inspect(connection).get_columns(table)]
                    names = ", ".join(old_names)
                    connection.execute(text(f"INSERT INTO {temporary.name} ({names}) SELECT {names} FROM {table}"))
                    connection.execute(text(f"DROP TABLE {table}"))
                    connection.execute(text(f"ALTER TABLE {temporary.name} RENAME TO {table}"))
                    for index in model.__table__.indexes:
                        connection.execute(CreateIndex(index))
                else:
                    connection.execute(text(f"ALTER TABLE {table} ALTER COLUMN user_id DROP NOT NULL"))
        if "group_members" in table_names:
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_group_guest_token ON group_members (guest_token_hash)"))
        if "group_members" in table_names and "presence" not in {c["name"] for c in inspect(connection).get_columns("group_members")}:
            connection.execute(text("ALTER TABLE group_members ADD COLUMN presence JSON"))
        if "archived_at" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE"))
        if "auto_archive_at" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN auto_archive_at TIMESTAMP WITH TIME ZONE"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_puzzles_auto_archive_at ON puzzles (auto_archive_at)"))
        if "created_by_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN created_by_id INTEGER"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_puzzles_created_by_id ON puzzles (created_by_id)"))
        if "completion_message" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN completion_message TEXT NOT NULL DEFAULT ''"))
        if "category_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN category_id INTEGER"))
        if "content_source_id" in puzzle_columns and "word_bank_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles RENAME COLUMN content_source_id TO word_bank_id"))
        elif "word_bank_id" not in puzzle_columns:
            connection.execute(text("ALTER TABLE puzzles ADD COLUMN word_bank_id INTEGER"))
        if "is_admin" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"))
        if "email" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(255)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"))
        if "password_hash" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))
        if "is_active" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE"))
        if "must_change_password" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE"))
        if "reset_token_hash" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN reset_token_hash VARCHAR(255)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_reset_token_hash ON users (reset_token_hash)"))
        if "reset_token_expires_at" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN reset_token_expires_at TIMESTAMP WITH TIME ZONE"))
        if "api_token_hash" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN api_token_hash VARCHAR(255)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_api_token_hash ON users (api_token_hash)"))
        if "api_token_prefix" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN api_token_prefix VARCHAR(12) NOT NULL DEFAULT ''"))
        if "api_token_last_used_at" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN api_token_last_used_at TIMESTAMP WITH TIME ZONE"))
        if "session_version" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"))
        if "last_login_at" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP WITH TIME ZONE"))
        if "dictionary_words" in table_names:
            if "archived_at" not in dictionary_word_columns:
                connection.execute(text("ALTER TABLE dictionary_words ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE"))
            if "banned_at" not in dictionary_word_columns:
                connection.execute(text("ALTER TABLE dictionary_words ADD COLUMN banned_at TIMESTAMP WITH TIME ZONE"))
        if "dictionary_definitions" in table_names and "archived_at" not in dictionary_definition_columns:
            connection.execute(text("ALTER TABLE dictionary_definitions ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dictionary_definitions_archived_at ON dictionary_definitions (archived_at)"))
        if "puzzle_tags" in table_names and "tag_type" not in tag_columns:
            connection.execute(text("ALTER TABLE puzzle_tags ADD COLUMN tag_type VARCHAR(80) NOT NULL DEFAULT 'General'"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_puzzle_tags_tag_type ON puzzle_tags (tag_type)"))

        if "word_banks" in table_names:
            word_bank_columns = {column["name"] for column in inspector.get_columns("word_banks")}
            if "source_type" in word_bank_columns:
                connection.execute(text("ALTER TABLE word_banks DROP COLUMN source_type"))

        if "word_bank_words" in table_names:
            word_bank_word_columns = {column["name"] for column in inspector.get_columns("word_bank_words")}
            if "content_source_id" in word_bank_word_columns and "word_bank_id" not in word_bank_word_columns:
                connection.execute(text("ALTER TABLE word_bank_words RENAME COLUMN content_source_id TO word_bank_id"))
            if "definition_id" not in word_bank_word_columns:
                connection.execute(text("ALTER TABLE word_bank_words ADD COLUMN definition_id INTEGER"))
                connection.execute(text("CREATE INDEX IF NOT EXISTS ix_word_bank_words_definition_id ON word_bank_words (definition_id)"))

        if "word_bank_tag_links" in table_names:
            word_bank_tag_columns = {column["name"] for column in inspector.get_columns("word_bank_tag_links")}
            if "content_source_id" in word_bank_tag_columns and "word_bank_id" not in word_bank_tag_columns:
                connection.execute(text("ALTER TABLE word_bank_tag_links RENAME COLUMN content_source_id TO word_bank_id"))

        if "dictionary_import_sources" not in table_names:
            connection.execute(text("""
                CREATE TABLE dictionary_import_sources (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    source_url TEXT NOT NULL,
                    source_type VARCHAR(40) NOT NULL DEFAULT 'auto',
                    source_label VARCHAR(160) NOT NULL DEFAULT '',
                    source_api_token TEXT NOT NULL DEFAULT '',
                    sync_interval_hours INTEGER NOT NULL DEFAULT 24,
                    max_items INTEGER NOT NULL DEFAULT 500,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    last_synced_at TIMESTAMP WITH TIME ZONE,
                    next_sync_at TIMESTAMP WITH TIME ZONE,
                    last_result JSON,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dictionary_import_sources_enabled ON dictionary_import_sources (enabled)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dictionary_import_sources_last_synced_at ON dictionary_import_sources (last_synced_at)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dictionary_import_sources_next_sync_at ON dictionary_import_sources (next_sync_at)"))
