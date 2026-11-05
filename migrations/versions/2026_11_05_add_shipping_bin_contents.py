"""add_shipping_bin_contents

Revision ID: 2026_11_05_shipping_bin_contents
Revises: 2026_10_31_move_history
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2026_11_05_shipping_bin_contents'
down_revision: Union[str, None] = '2026_10_31_move_history'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # What each shipping bin held when its job was completed. items.shipping_bin_id
    # is moved or cleared when a bin is cleared, an item is refiled, or an item
    # ships again, so a completed job's manifest reads this table instead.
    op.create_table(
        'shipping_bin_contents',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'shipping_bin_id', sa.BigInteger(),
            sa.ForeignKey('shipping_bins.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('item_id', sa.BigInteger(), sa.ForeignKey('items.id'), nullable=True),
        sa.Column('non_tray_item_id', sa.BigInteger(), sa.ForeignKey('non_tray_items.id'), nullable=True),
        sa.Column('create_dt', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('update_dt', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            '(item_id IS NULL) <> (non_tray_item_id IS NULL)',
            name='ck_shipping_bin_contents_one_item',
        ),
        sa.UniqueConstraint('shipping_bin_id', 'item_id', name='uq_shipping_bin_contents_item'),
        sa.UniqueConstraint('shipping_bin_id', 'non_tray_item_id', name='uq_shipping_bin_contents_non_tray_item'),
    )
    op.create_index('ix_shipping_bin_contents_shipping_bin_id', 'shipping_bin_contents', ['shipping_bin_id'])
    op.create_index('ix_shipping_bin_contents_item_id', 'shipping_bin_contents', ['item_id'])
    op.create_index('ix_shipping_bin_contents_non_tray_item_id', 'shipping_bin_contents', ['non_tray_item_id'])

    # Backfill completed jobs from the links still in place. Bins already cleared,
    # and items since refiled or shipped again, can't be recovered here.
    for column, table in (('item_id', 'items'), ('non_tray_item_id', 'non_tray_items')):
        op.execute(f"""
            INSERT INTO shipping_bin_contents (shipping_bin_id, {column})
            SELECT t.shipping_bin_id, t.id
            FROM {table} t
            JOIN shipping_bins b ON b.id = t.shipping_bin_id
            JOIN shipping_jobs j ON j.id = b.shipping_job_id
            WHERE j.status = 'Completed'
        """)


def downgrade() -> None:
    op.drop_index('ix_shipping_bin_contents_non_tray_item_id', table_name='shipping_bin_contents')
    op.drop_index('ix_shipping_bin_contents_item_id', table_name='shipping_bin_contents')
    op.drop_index('ix_shipping_bin_contents_shipping_bin_id', table_name='shipping_bin_contents')
    op.drop_table('shipping_bin_contents')
