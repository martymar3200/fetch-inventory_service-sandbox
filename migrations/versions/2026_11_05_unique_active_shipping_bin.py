"""unique_active_shipping_bin

Revision ID: 2026_11_05_unique_active_bin
Revises: 2026_11_05_shipping_bin_contents
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2026_11_05_unique_active_bin'
down_revision: Union[str, None] = '2026_11_05_shipping_bin_contents'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A bin barcode can be active (uncleared) in only one bin at a time. The bin
    # scan already checks this, but two scans at the same moment could both pass.
    duplicates = op.get_bind().execute(sa.text(
        "SELECT barcode, string_agg(shipping_job_id::text, ', ' ORDER BY shipping_job_id) "
        "FROM shipping_bins WHERE cleared_dt IS NULL GROUP BY barcode HAVING count(*) > 1"
    )).all()
    if duplicates:
        listed = "; ".join(f"{barcode} (jobs {jobs})" for barcode, jobs in duplicates)
        # Stop rather than pick which bin to clear: that needs someone who knows the bins
        raise RuntimeError(
            f"These bin barcodes are active in more than one bin: {listed}. "
            "Clear the extra bins (Clear Bin on the Shipping page), then run the migration again."
        )

    op.create_index(
        'uq_shipping_bins_active_barcode', 'shipping_bins', ['barcode'],
        unique=True, postgresql_where=sa.text('cleared_dt IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_shipping_bins_active_barcode', table_name='shipping_bins')
