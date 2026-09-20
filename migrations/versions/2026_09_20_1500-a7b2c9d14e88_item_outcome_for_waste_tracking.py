"""item outcome, for waste and spending

The You screen reports what was eaten against what was binned, and `consumed`
cannot tell those apart. Adds:
  * grocery_item.outcome      on_shelf / used / wasted
  * grocery_item.resolved_on  when it left the shelf
  * grocery_item.rescued      used while already in the red

Existing rows are backfilled from `consumed`: anything consumed becomes `used`,
dated from its expiry or purchase, since the real date was never recorded.
Nothing is backfilled as `wasted` — no existing row carries evidence that it
was thrown away, and inventing waste would put a number on the chart that never
happened.

Revision ID: a7b2c9d14e88
Revises: c4a17e9b52d1
Create Date: 2026-09-20 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b2c9d14e88'
down_revision: Union[str, Sequence[str], None] = 'c4a17e9b52d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

outcome_enum = sa.Enum('on_shelf', 'used', 'wasted', name='itemoutcome')


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    outcome_enum.create(bind, checkfirst=True)

    op.add_column('grocery_item', sa.Column('outcome', outcome_enum, nullable=False,
                                            server_default='on_shelf'))
    op.add_column('grocery_item', sa.Column('resolved_on', sa.Date(), nullable=True))
    op.add_column('grocery_item', sa.Column('rescued', sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    op.create_index(op.f('ix_grocery_item_outcome'), 'grocery_item', ['outcome'])
    op.create_index(op.f('ix_grocery_item_resolved_on'), 'grocery_item', ['resolved_on'])
    op.create_index(op.f('ix_grocery_item_rescued'), 'grocery_item', ['rescued'])

    # `consumed` means it is gone, and the only thing we know about how is that
    # somebody marked it off — treat that as eaten. COALESCE picks the best
    # date available; a row with neither stays null rather than guessing.
    op.execute("""
        UPDATE grocery_item
           SET outcome = 'used',
               resolved_on = COALESCE(expires_on, purchased_on)
         WHERE consumed = true
    """ if bind.dialect.name != 'sqlite' else """
        UPDATE grocery_item
           SET outcome = 'used',
               resolved_on = COALESCE(expires_on, purchased_on)
         WHERE consumed = 1
    """)

    # Defaults were for the backfill only; new rows get theirs from the app.
    with op.batch_alter_table('grocery_item') as batch:
        batch.alter_column('outcome', existing_type=outcome_enum, server_default=None)
        batch.alter_column('rescued', existing_type=sa.Boolean(), server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_grocery_item_rescued'), table_name='grocery_item')
    op.drop_index(op.f('ix_grocery_item_resolved_on'), table_name='grocery_item')
    op.drop_index(op.f('ix_grocery_item_outcome'), table_name='grocery_item')
    op.drop_column('grocery_item', 'rescued')
    op.drop_column('grocery_item', 'resolved_on')
    op.drop_column('grocery_item', 'outcome')
    outcome_enum.drop(op.get_bind(), checkfirst=True)
