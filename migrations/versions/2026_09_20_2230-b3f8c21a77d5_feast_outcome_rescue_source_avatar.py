"""feast outcome, rescue source and avatar url

Three additive columns, none of which change an existing response:
  * grocery_item.rescued_feast_id  which feast ate it; null means solo
  * feast.status / feast.outcome_at  planned / rescued / failed
  * app_user.avatar_url            a link to a profile photo

Every existing rescue predates feasts being tracked, so they all read as solo —
which is what they were. Nothing is backfilled.

Revision ID: b3f8c21a77d5
Revises: a7b2c9d14e88
Create Date: 2026-09-20 22:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3f8c21a77d5'
down_revision: Union[str, Sequence[str], None] = 'a7b2c9d14e88'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

feast_status_enum = sa.Enum('planned', 'rescued', 'failed', name='feaststatus')


def _sqlite_fks(enabled: bool) -> None:
    """Toggle SQLite foreign key enforcement; a no-op on any other dialect.

    Batch mode rebuilds a table by dropping it, which fires the ON DELETE
    CASCADEs pointing at it. The PRAGMA must run outside a transaction —
    inside one SQLite ignores it silently — hence the autocommit block.
    """
    context = op.get_context()
    if context.dialect.name != 'sqlite':
        return
    with context.autocommit_block():
        op.execute('PRAGMA foreign_keys=' + ('ON' if enabled else 'OFF'))


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    feast_status_enum.create(bind, checkfirst=True)

    # ---- feast ----
    op.add_column('feast', sa.Column('status', feast_status_enum, nullable=False,
                                     server_default='planned'))
    op.add_column('feast', sa.Column('outcome_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_feast_status'), 'feast', ['status'])

    # ---- grocery_item ----
    # SET NULL, not CASCADE: deleting a feast must never delete somebody's
    # grocery history. The rescue survives it and reads as solo.
    op.add_column('grocery_item', sa.Column('rescued_feast_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_grocery_item_rescued_feast_id'), 'grocery_item',
                    ['rescued_feast_id'])
    with _fks_off(), op.batch_alter_table('grocery_item') as batch:
        batch.create_foreign_key(
            'fk_grocery_item_rescued_feast_id', 'feast',
            ['rescued_feast_id'], ['id'], ondelete='SET NULL',
        )

    # ---- app_user ----
    op.add_column('app_user', sa.Column(
        'avatar_url', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True))

    # The default stays. It backfills existing rows, and it keeps the column
    # writable by a build that does not know about it yet, which is what a
    # rolling deploy has for as long as it takes to swap over.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('app_user', 'avatar_url')

    with _fks_off(), op.batch_alter_table('grocery_item') as batch:
        batch.drop_constraint('fk_grocery_item_rescued_feast_id', type_='foreignkey')
    op.drop_index(op.f('ix_grocery_item_rescued_feast_id'), table_name='grocery_item')
    op.drop_column('grocery_item', 'rescued_feast_id')

    op.drop_index(op.f('ix_feast_status'), table_name='feast')
    op.drop_column('feast', 'outcome_at')
    op.drop_column('feast', 'status')
    feast_status_enum.drop(op.get_bind(), checkfirst=True)


class _fks_off:
    """No-op except on SQLite, where a table rebuild would cascade deletes."""

    def __enter__(self):
        _sqlite_fks(False)
        return self

    def __exit__(self, *exc):
        _sqlite_fks(True)
        return False
