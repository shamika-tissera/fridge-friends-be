"""add username to app_user

Brought over from the other working copy of this repo, where it was written and
applied to the shared database without being committed. It has to live here:
the database is at this revision, so nothing downstream of it can run until
alembic can see it.

One fix on import: the SQLite branch below rebuilds app_user by dropping it,
which fires grocery_item's ON DELETE CASCADE and silently empties that table
(and feasts, and notifications). Postgres takes the plain-ALTER branch and was
never affected, so this changes nothing about the schema this revision
produces — only whether a local SQLite database survives it.

Revision ID: d82a3e9a4925
Revises: 21bb049b161b
Create Date: 2026-09-19 17:50:02.782655

"""
from typing import Sequence, Union

from alembic import op
import re

import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd82a3e9a4925'
down_revision: Union[str, Sequence[str], None] = '21bb049b161b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _sqlite_fks(enabled: bool) -> None:
    """Toggle SQLite foreign key enforcement; a no-op on any other dialect.

    The PRAGMA must run outside a transaction — inside one SQLite ignores it
    silently — hence the autocommit block.
    """
    context = op.get_context()
    if context.dialect.name != "sqlite":
        return
    with context.autocommit_block():
        op.execute("PRAGMA foreign_keys=" + ("ON" if enabled else "OFF"))


def slugify(value: str) -> str:
    """Turn an email local-part into something the username rules accept."""
    cleaned = re.sub(r"[^a-z0-9._-]", "", (value or "").lower())
    cleaned = cleaned.lstrip("._-")            # must start with a letter or digit
    return cleaned[:32]


def upgrade() -> None:
    """Add app_user.username, backfilled from each user's email.

    Three steps, because the column is NOT NULL and unique: add it nullable,
    fill every existing row with a distinct value, then apply the constraints.
    Doing it in one step would fail on any table that already has rows.
    """
    bind = op.get_bind()

    op.add_column("app_user", sa.Column("username", sa.String(length=32), nullable=True))

    rows = bind.execute(sa.text("SELECT id, email, name FROM app_user ORDER BY id")).all()
    taken: set[str] = set()
    for user_id, email, name in rows:
        base = slugify((email or "").split("@")[0]) or slugify(name) or f"user{user_id}"
        if len(base) < 3:
            base = f"{base}{user_id}".ljust(3, "0")
        candidate, suffix = base, 1
        while candidate in taken:
            suffix += 1
            tail = str(suffix)
            candidate = f"{base[:32 - len(tail)]}{tail}"
        taken.add(candidate)
        bind.execute(
            sa.text("UPDATE app_user SET username = :u WHERE id = :i"),
            {"u": candidate, "i": user_id},
        )

    if bind.dialect.name == "sqlite":
        # SQLite cannot ALTER a column to NOT NULL; batch mode rebuilds the
        # table, so foreign keys go off around it (see the module docstring).
        _sqlite_fks(False)
        try:
            with op.batch_alter_table("app_user") as batch:
                batch.alter_column("username", existing_type=sa.String(length=32), nullable=False)
        finally:
            _sqlite_fks(True)
    else:
        op.alter_column("app_user", "username", existing_type=sa.String(length=32),
                        nullable=False)

    op.create_index("ix_app_user_username", "app_user", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_app_user_username", table_name="app_user")
    op.drop_column("app_user", "username")
