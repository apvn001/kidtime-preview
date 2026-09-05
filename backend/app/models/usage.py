"""用量模型：`daily_usage`（§3.8）。每设备每日一行，UNIQUE 约束防重复。"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.device import Device


class DailyUsage(Base):
    """设备每日累计用量（D33，服务端写入取 MAX 防回退）。"""

    __tablename__ = "daily_usage"
    __table_args__ = (
        UniqueConstraint("device_id", "usage_date", name="uq_daily_usage_device_date"),
        Index("ix_daily_usage_device_date", "device_id", "usage_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    used_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bonus_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # V7 功能三（扣减用量）：累计被管理端扣减的分钟，纯审计列，不进入 remaining 公式
    # （扣减已通过 used_minutes += applied 生效）。默认值 0 与 used_minutes 等列一致。
    deducted_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    break_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_quota_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    device: Mapped["Device"] = relationship(back_populates="daily_usages")

    @property
    def effective_quota_minutes(self) -> int:
        """有效配额 = 基础配额 + 当日额度（D32）。"""
        return self.base_quota_minutes + self.bonus_minutes

    @property
    def remaining_minutes(self) -> int:
        """剩余分钟 = max(0, 有效配额 - 已用)。"""
        return max(0, self.effective_quota_minutes - self.used_minutes)
