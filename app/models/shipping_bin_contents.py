
from typing import Optional, TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.shipping_bins import ShippingBin


class ShippingBinContent(Base):
    """
    What a shipping bin held when its job was completed: one row per item or
    non-tray item. Written once by /complete and never changed, so a completed
    job's manifest stays the same after its bin is cleared, the item is
    refiled, or the item is shipped again (all of which move or clear
    items.shipping_bin_id).
    """
    __tablename__ = "shipping_bin_contents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    shipping_bin_id: Mapped[int] = mapped_column(
        ForeignKey("shipping_bins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("items.id"), nullable=True, index=True)
    non_tray_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("non_tray_items.id"), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "(item_id IS NULL) <> (non_tray_item_id IS NULL)",
            name="ck_shipping_bin_contents_one_item",
        ),
        UniqueConstraint("shipping_bin_id", "item_id", name="uq_shipping_bin_contents_item"),
        UniqueConstraint("shipping_bin_id", "non_tray_item_id", name="uq_shipping_bin_contents_non_tray_item"),
    )
