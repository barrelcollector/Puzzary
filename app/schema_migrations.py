from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.database import engine


ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "0001_current_schema"
CORE_TABLES = {"users", "puzzles"}


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    database_url = engine.url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_database() -> None:
    """Bring a fresh or existing Puzzary database to the current revision."""
    tables = set(inspect(engine).get_table_names())
    config = alembic_config()
    if "alembic_version" not in tables and tables:
        if not CORE_TABLES.issubset(tables):
            found = ", ".join(sorted(tables))
            raise RuntimeError(
                "Refusing to baseline a non-empty database that is not recognizably Puzzary "
                f"(found tables: {found})."
            )
        # Historical Puzzary installations predate Alembic. The baseline creates
        # the current model only for empty databases; populated databases are
        # stamped in place and normalized by the next ordered revision.
        command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")


if __name__ == "__main__":
    upgrade_database()
