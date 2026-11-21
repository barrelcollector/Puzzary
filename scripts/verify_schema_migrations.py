"""Verify fresh, populated, and representative historical database upgrades."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from qa_runtime import qa_directory


def child_check(scenario: str) -> None:
    from alembic import command
    from sqlalchemy import inspect, select, text

    from app.database import Base, SessionLocal, engine
    from app.models import DictionaryWord, Puzzle, User, WordBank, WordBankWord
    from app.schema_migrations import alembic_config, upgrade_database

    if scenario == "fresh":
        upgrade_database()
        assert {"users", "puzzles", "alembic_version"} <= set(inspect(engine).get_table_names())
    else:
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            owner = User(display_name=f"{scenario} owner", is_admin=True)
            bank = WordBank(name=f"{scenario} bank", slug=f"{scenario}-bank")
            word = DictionaryWord(word=f"{scenario}-word", display_text="Preserved")
            db.add_all([owner, bank, word])
            db.flush()
            puzzle = Puzzle(
                id=f"{scenario}-puzzle",
                puzzle_type="migration-test",
                title="Preserved puzzle",
                theme="",
                difficulty="easy",
                created_by_id=owner.id,
                word_bank_id=bank.id,
                print_payload={},
                play_payload={"answer": "preserve-me"},
                solution_payload={},
            )
            db.add(puzzle)
            db.flush()
            db.add(WordBankWord(word_bank_id=bank.id, word_id=word.id))
            db.commit()

        if scenario == "legacy":
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users DROP COLUMN permissions_initialized"))
                connection.execute(text("ALTER TABLE users DROP COLUMN permission_overrides"))
                connection.execute(text("ALTER TABLE users DROP COLUMN appearance_theme"))
                connection.execute(text("ALTER TABLE puzzles DROP COLUMN completion_message"))
                connection.execute(text("DROP INDEX ix_puzzles_publish_at"))
                connection.execute(text("ALTER TABLE puzzles DROP COLUMN publish_at"))
                connection.execute(text("DROP INDEX ix_puzzles_auto_archive_at"))
                connection.execute(text("ALTER TABLE puzzles DROP COLUMN auto_archive_at"))
                connection.execute(text("ALTER TABLE word_bank_words RENAME COLUMN word_bank_id TO content_source_id"))
                connection.execute(text("ALTER TABLE word_banks RENAME TO content_sources"))
                connection.execute(text("DROP TABLE dictionary_import_sources"))

        upgrade_database()
        upgrade_database()
        columns = {column["name"] for column in inspect(engine).get_columns("users")}
        assert {"permissions_initialized", "permission_overrides", "appearance_theme"} <= columns
        puzzle_columns = {column["name"] for column in inspect(engine).get_columns("puzzles")}
        assert {"completion_message", "publish_at", "auto_archive_at"} <= puzzle_columns
        assert "dictionary_import_sources" in inspect(engine).get_table_names()
        with SessionLocal() as db:
            puzzle = db.scalar(select(Puzzle).where(Puzzle.id == f"{scenario}-puzzle"))
            assert puzzle is not None and puzzle.play_payload == {"answer": "preserve-me"}
            assert db.scalar(select(WordBankWord).join(DictionaryWord).where(DictionaryWord.display_text == "Preserved")) is not None

    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_legacy_bridge"
    command.check(alembic_config())


def parent_check() -> None:
    root = Path(__file__).resolve().parents[1]
    database_source = (root / "app" / "database.py").read_text(encoding="utf-8")
    startup_source = (root / "app" / "main.py").read_text(encoding="utf-8")
    assert "metadata.create_all" not in database_source
    assert "ALTER TABLE" not in database_source
    assert "upgrade_database" not in startup_source
    bridge_source = (root / "migrations" / "versions" / "0002_legacy_schema_bridge.py").read_text(encoding="utf-8")
    assert "app.models" not in bridge_source and "app.database" not in bridge_source
    qa_root = qa_directory("schema-migrations")
    for scenario in ("fresh", "current", "legacy"):
        database = qa_root / f"{scenario}.sqlite"
        database.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
        environment["PYTHONPATH"] = str(root)
        subprocess.run(
            [sys.executable, __file__, "--child", scenario],
            cwd=root,
            env=environment,
            check=True,
        )
    print("Fresh installs, populated baselines, historical upgrades, repeat runs, revision tracking, and data preservation passed.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child_check(sys.argv[2])
    else:
        parent_check()
