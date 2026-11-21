"""Normalize pre-Alembic installations and create later model tables."""
from __future__ import annotations

from alembic import op

from migrations.legacy_models import Base
from migrations.legacy_schema import ensure_schema


revision = "0002_legacy_bridge"
down_revision = "0001_current_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This is the frozen bridge for every schema supported before issue #97.
    # It retains the proven, data-preserving compatibility transformations;
    # all changes after this revision must be a new Alembic revision.
    ensure_schema()
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError(
        "The legacy compatibility migration is not reversible in place. "
        "Restore the matching pre-upgrade database backup."
    )
