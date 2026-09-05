"""延时申请模型：`extension_requests`（§3.9）。主键即客户端生成的 UUID。"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.device import Device


class ExtensionRequest(Base):
    """孩子提交的延时申请（D27–D31）。"""

    __tablename__ = "extension_requests"
    __table_args__ = (
        CheckConstraint("requested_minutes BETWEEN 1 AND 240", name="requested_minutes_range"),
        CheckConstraint(
            "approved_minutes IS NULL OR approved_minutes BETWEEN 0 AND 240",
            name="approved_minutes_range",
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired','credited')",
            name="status_valid",
        ),
        Index("ix_extension_requests_device_status", "device_id", "status"),
        Index("ix_extension_requests_target_date", "target_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    requested_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    approved_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # V5：删账号后审批记录必须留存（PRD D5）→ SET NULL + 姓名快照
    decided_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="审批人姓名快照（账号被删后仍可溯源）"
    )
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    credited_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    client_created_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    device: Mapped["Device"] = relationship(back_populates="extension_requests")
