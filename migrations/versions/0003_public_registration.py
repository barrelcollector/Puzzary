"""Add verified public-registration account state.

Revision ID: 0003_public_registration
Revises: 0002_legacy_bridge
"""
from __future__ import annotations

import unicodedata

import sqlalchemy as sa
from alembic import op


revision = "0003_public_registration"
down_revision = "0002_legacy_bridge"
branch_labels = None
depends_on = None


def _identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("users")}

    def add_column(column: sa.Column) -> None:
        if column.name not in columns:
            op.add_column("users", column)
            columns.add(column.name)

    add_column(sa.Column("display_name_normalized", sa.String(length=160), nullable=True))
    add_column(sa.Column("email_normalized", sa.String(length=320), nullable=True))
    add_column(sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    add_column(sa.Column("email_verification_token_hash", sa.String(length=64), nullable=True))
    add_column(sa.Column("email_verification_expires_at", sa.DateTime(timezone=True), nullable=True))
    add_column(sa.Column("email_verification_sent_at", sa.DateTime(timezone=True), nullable=True))
    add_column(sa.Column("registration_source", sa.String(length=24), nullable=False, server_default="admin"))
    add_column(sa.Column("registration_created_at", sa.DateTime(timezone=True), nullable=True))

    rows = connection.execute(sa.text("SELECT id, display_name, email FROM users ORDER BY id")).mappings().all()
    display_names: set[str] = set()
    email_addresses: set[str] = set()
    for row in rows:
        base = _identity(row["display_name"] or "") or f"user-{row['id']}"
        display_identity = base
        if display_identity in display_names:
            suffix = f"~legacy-{row['id']}"
            display_identity = f"{base[:160 - len(suffix)]}{suffix}"
        display_names.add(display_identity)

        email_identity = _identity(row["email"] or "") or None
        if email_identity in email_addresses:
            # Legacy databases permitted duplicate email addresses. Preserve the
            # accounts but reserve email login/verification for the first one.
            email_identity = None
        if email_identity:
            email_addresses.add(email_identity)

        connection.execute(
            sa.text(
                "UPDATE users SET display_name_normalized=:display_name_normalized, "
                "email_normalized=:email_normalized, "
                "email_verified_at=CASE WHEN email IS NOT NULL AND email <> '' THEN CURRENT_TIMESTAMP ELSE NULL END "
                "WHERE id=:user_id"
            ),
            {
                "display_name_normalized": display_identity,
                "email_normalized": email_identity,
                "user_id": row["id"],
            },
        )

    indexes = inspector.get_indexes("users")

    def ensure_index(name: str, column: str, unique: bool) -> None:
        matching = any(
            index.get("column_names") == [column] and bool(index.get("unique")) == unique
            for index in indexes
        )
        if not matching:
            op.create_index(name, "users", [column], unique=unique)
            indexes.append({"name": name, "column_names": [column], "unique": unique})

    ensure_index("ix_users_display_name_normalized", "display_name_normalized", True)
    ensure_index("ix_users_email_normalized", "email_normalized", True)
    ensure_index("ix_users_email_verification_token_hash", "email_verification_token_hash", True)
    ensure_index("ix_users_registration_created_at", "registration_created_at", False)


def downgrade() -> None:
    raise RuntimeError(
        "Public-registration account state is not downgraded in place. "
        "Restore the matching pre-upgrade database backup."
    )
