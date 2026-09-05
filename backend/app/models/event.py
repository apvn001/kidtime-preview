"""事件日志模型：`event_logs`（§3.11）。`client_event_id` UNIQUE 用于离线去重（S10）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.device import Device


class EventLog(Base):
    """设备与服务端事件流水。"""

    __tablename__ = "event_logs"
    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','error')", name="severity_valid"),
        Index("ix_event_logs_device_occurred", "device_id", "occurred_at"),
        Index("ix_event_logs_type_occurred", "event_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_event_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    device_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # V5：删账号后事件「操作人」仍需可读（PRD D5）。升级前的存量行为 NULL，
    # 迁移不做回填（§10-Q4），此时前端退化展示为「已删除账号」。
    actor_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="操作人姓名快照（账号被删后仍可溯源）"
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    device: Mapped["Device | None"] = relationship(back_populates="events")

    @property
    def payload(self) -> dict[str, Any]:
        """反序列化事件负载。"""
        try:
            value = json.loads(self.payload_json)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {"value": value}

    @payload.setter
    def payload(self, value: dict[str, Any]) -> None:
        """序列化事件负载。"""
        self.payload_json = json.dumps(value or {}, ensure_ascii=False)
