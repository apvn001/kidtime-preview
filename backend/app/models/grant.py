"""下发时长模型：`time_grants`（§3.3.2）。

管理员向某设备「当日额外配额」下发记录。每条 grant 由服务端生成 UUID，
经既有 credits 下行链路（``/client/sync`` 的 ``credits`` 数组）推送给客户端，
客户端凭既有 ``credited_ledger`` 精确一次去重入账；``/client/confirm-credit``
路由到本表与 ``ExtensionRequest`` 双表（credit_service.confirm）。

状态机：``granted`` → ``credited``（客户端确认入账）/ ``revoked``（管理员撤回）/
``expired``（目标日已过期，惰性置位，不再下发）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import GrantStatus
from app.db.base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.device import Device


class TimeGrant(Base):
    """管理员下发的一次性当日额外时长（V7 §2.3 / §3.3）。"""

    __tablename__ = "time_grants"
    __table_args__ = (
        CheckConstraint("minutes BETWEEN 1 AND 240", name="minutes_range"),
        CheckConstraint(
            "status IN ('granted','credited','revoked','expired')", name="status_valid"
        ),
        Index("ix_time_grants_device_date", "device_id", "target_date"),
        Index("ix_time_grants_device_status", "device_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=GrantStatus.GRANTED.value, index=True
    )
    # V5 风格：删账号后下发记录必须留存 → SET NULL + 姓名快照
    granted_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    granted_by_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="下发人姓名快照（账号被删后仍可溯源）"
    )
    granted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    credited_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    device: Mapped["Device"] = relationship(back_populates="time_grants")
