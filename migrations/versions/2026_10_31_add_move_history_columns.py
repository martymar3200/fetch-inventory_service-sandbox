"""add_move_history_columns

Revision ID: 2026_10_31_move_history
Revises: 2026_08_19_merge_ils_heads
Create Date: 2026-10-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2026_10_31_move_history'
down_revision: Union[str, None] = '2026_08_19_merge_ils_heads'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (column, referenced table, constraint name)
_FOREIGN_KEYS = [
    ('original_shelf_position_id', 'shelf_positions', 'fk_sjc_original_shelf_position_id'),
    ('source_tray_id', 'trays', 'fk_sjc_source_tray_id'),
    ('moved_by_user_id', 'users', 'fk_sjc_moved_by_user_id'),
]


def upgrade() -> None:
    # Move history: where a moved container came from and who moved it.
    # Each move in a Move job is one shelving_job_containers row. The text
    # columns keep the "from" readable after a position is re-addressed or a
    # source tray is deleted, so the foreign keys are SET NULL instead of
    # blocking those deletes.
    op.add_column('shelving_job_containers', sa.Column('original_shelf_position_id', sa.Integer(), nullable=True))
    op.add_column('shelving_job_containers', sa.Column('original_location', sa.String(length=175), nullable=True))
    op.add_column('shelving_job_containers', sa.Column('source_tray_id', sa.BigInteger(), nullable=True))
    op.add_column('shelving_job_containers', sa.Column('source_tray_barcode', sa.String(length=255), nullable=True))
    op.add_column('shelving_job_containers', sa.Column('moved_by_user_id', sa.Integer(), nullable=True))
    for column, table, name in _FOREIGN_KEYS:
        op.create_foreign_key(name, 'shelving_job_containers', table, [column], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    for _, _, name in _FOREIGN_KEYS:
        op.drop_constraint(name, 'shelving_job_containers', type_='foreignkey')
    op.drop_column('shelving_job_containers', 'moved_by_user_id')
    op.drop_column('shelving_job_containers', 'source_tray_barcode')
    op.drop_column('shelving_job_containers', 'source_tray_id')
    op.drop_column('shelving_job_containers', 'original_location')
    op.drop_column('shelving_job_containers', 'original_shelf_position_id')
