"""restore the default on feast.status

b3f8c21a77d5 added `feast.status` NOT NULL, backfilled it, then dropped the
server default again. That left the column unwritable by any build that does
not name it — which is every build from before that migration.

A schema migration lands before the code that needs it, so for the length of a
deploy the old code is talking to the new schema. Here it could not: creating a
feast omits `status`, the column had no default, and every POST /feasts failed
with a not-null violation until the new build took over.

Putting the default back is what makes the column safe to write from either
side of a deploy. It changes no existing row: they are all `planned` already.

Revision ID: c9d4e88b12f3
Revises: b3f8c21a77d5
Create Date: 2026-09-21 00:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d4e88b12f3'
down_revision: Union[str, Sequence[str], None] = 'b3f8c21a77d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

feast_status_enum = sa.Enum('planned', 'rescued', 'failed', name='feaststatus')


def upgrade() -> None:
    """Give feast.status back its default.

    Safe to run against a database that already has one: setting a default to
    the value it already holds is a no-op.
    """
    with op.batch_alter_table('feast') as batch:
        batch.alter_column(
            'status',
            existing_type=feast_status_enum,
            existing_nullable=False,
            server_default='planned',
        )


def downgrade() -> None:
    """Drop it again, restoring the state b3f8c21a77d5 left behind."""
    with op.batch_alter_table('feast') as batch:
        batch.alter_column(
            'status',
            existing_type=feast_status_enum,
            existing_nullable=False,
            server_default=None,
        )
