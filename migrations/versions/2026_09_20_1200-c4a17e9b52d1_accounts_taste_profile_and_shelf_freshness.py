"""accounts, taste profile and shelf freshness

Adds what the join, get-to-know-you and shelf screens need:
  * app_user: password_hash, buddy and the three taste-profile lists; email
    becomes optional, since the join screen never asks for one; and `username`
    widens from 32 to the 40 characters the model allows.
  * grocery_item: price, shelf_life_days, spoilage_profile and shelf_buddy.

`username` itself arrived in d82a3e9a4925, which this revision follows.

Existing rows keep working: every new column either has a default or is
nullable. Existing accounts get no password, which `verify_password` treats as
"cannot log in" — they set one through the password endpoint.

Revision ID: c4a17e9b52d1
Revises: d82a3e9a4925
Create Date: 2026-09-20 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4a17e9b52d1'
down_revision: Union[str, Sequence[str], None] = 'd82a3e9a4925'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite cannot ALTER a column, so alembic's batch mode rebuilds the table:
# new table, copy, DROP old, rename. That DROP fires grocery_item's
# ON DELETE CASCADE and takes every item with it, so foreign keys are switched
# off for the duration. Postgres issues a plain ALTER and needs none of this.
def _sqlite_fks(enabled: bool) -> None:
    """Toggle SQLite's foreign key enforcement. A no-op on any other dialect.

    The PRAGMA has to run outside a transaction — inside one SQLite ignores it
    silently — hence the autocommit block.
    """
    context = op.get_context()
    if context.dialect.name != 'sqlite':
        return
    with context.autocommit_block():
        op.execute('PRAGMA foreign_keys=' + ('ON' if enabled else 'OFF'))


class _fks_off:
    """No-op except on SQLite, where a table rebuild would cascade deletes."""

    def __enter__(self):
        _sqlite_fks(False)
        return self

    def __exit__(self, *exc):
        _sqlite_fks(True)
        return False


JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

buddy_enum = sa.Enum('sammy', 'milo', 'eddie', 'carl', 'bella', name='buddy')
spoilage_enum = sa.Enum('gradual', 'sudden', 'stable', name='spoilageprofile')


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    # Postgres needs the types to exist before a column can use them; on SQLite
    # these are VARCHAR + CHECK and create() is a no-op.
    buddy_enum.create(bind, checkfirst=True)
    spoilage_enum.create(bind, checkfirst=True)

    # ---- app_user ----
    op.add_column('app_user', sa.Column('password_hash', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True))
    op.add_column('app_user', sa.Column('buddy', buddy_enum, nullable=False, server_default='sammy'))
    op.add_column('app_user', sa.Column('diets', JSON_TYPE, nullable=False, server_default='[]'))
    op.add_column('app_user', sa.Column('avoid_allergens', JSON_TYPE, nullable=False, server_default='[]'))
    op.add_column('app_user', sa.Column('favorite_cuisines', JSON_TYPE, nullable=False, server_default='[]'))

    with _fks_off(), op.batch_alter_table('app_user') as batch:
        # d82a3e9a4925 created this as VARCHAR(32); the model allows 40.
        # Widening is safe — no stored value can fail to fit.
        batch.alter_column('username', existing_type=sa.String(length=32),
                           type_=sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False)
        # The join screen collects no email, so an account may not have one.
        batch.alter_column('email', existing_type=sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True)

    # ---- grocery_item ----
    op.add_column('grocery_item', sa.Column('price', sa.Float(), nullable=True))
    op.add_column('grocery_item', sa.Column('shelf_life_days', sa.Integer(), nullable=True))
    op.add_column('grocery_item', sa.Column('spoilage_profile', spoilage_enum, nullable=False, server_default='gradual'))
    op.add_column('grocery_item', sa.Column('shelf_buddy', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False, server_default='leaf'))
    op.create_index(op.f('ix_grocery_item_spoilage_profile'), 'grocery_item', ['spoilage_profile'], unique=False)

    # Existing items keep their expiry; shelf_life_days is only recorded where
    # a purchase date makes it meaningful.
    op.execute("""
        UPDATE grocery_item
           SET shelf_life_days = CAST(julianday(expires_on) - julianday(purchased_on) AS INTEGER)
         WHERE expires_on IS NOT NULL AND purchased_on IS NOT NULL
    """ if bind.dialect.name == 'sqlite' else """
        UPDATE grocery_item
           SET shelf_life_days = (expires_on - purchased_on)
         WHERE expires_on IS NOT NULL AND purchased_on IS NOT NULL
    """)

    # The defaults were only needed to backfill; new rows get theirs from the app.
    with _fks_off(), op.batch_alter_table('app_user') as batch:
        batch.alter_column('buddy', existing_type=buddy_enum, server_default=None)
    with _fks_off(), op.batch_alter_table('grocery_item') as batch:
        batch.alter_column('spoilage_profile', existing_type=spoilage_enum, server_default=None)
        batch.alter_column('shelf_buddy', existing_type=sqlmodel.sql.sqltypes.AutoString(length=32), server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()

    op.drop_index(op.f('ix_grocery_item_spoilage_profile'), table_name='grocery_item')
    op.drop_column('grocery_item', 'shelf_buddy')
    op.drop_column('grocery_item', 'spoilage_profile')
    op.drop_column('grocery_item', 'shelf_life_days')
    op.drop_column('grocery_item', 'price')

    op.drop_column('app_user', 'favorite_cuisines')
    op.drop_column('app_user', 'avoid_allergens')
    op.drop_column('app_user', 'diets')
    op.drop_column('app_user', 'buddy')
    op.drop_column('app_user', 'password_hash')

    # Users created through the join screen have no email, and the column was
    # NOT NULL before this migration — give them a placeholder rather than
    # failing the downgrade half-way through.
    op.execute("UPDATE app_user SET email = 'user-' || id || '@placeholder.invalid' WHERE email IS NULL")
    with _fks_off(), op.batch_alter_table('app_user') as batch:
        batch.alter_column('email', existing_type=sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False)
        batch.alter_column('username', existing_type=sqlmodel.sql.sqltypes.AutoString(length=40),
                           type_=sa.String(length=32), nullable=False)

    spoilage_enum.drop(bind, checkfirst=True)
    buddy_enum.drop(bind, checkfirst=True)
