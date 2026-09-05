"""远程指令模型：`remote_commands`（§3.10）。逐设备独立 command_id（D48）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.device import Device


class RemoteCommand(Base):
    """家长下发的远程指令（D18/D19/D48/D49）。"""

    __tablename__ = "remote_commands"
    __table_args__ = (
        CheckConstraint(
            "type IN ('UNLOCK_TEMP','PAUSE_ENFORCEMENT','RESUME_ENFORCEMENT','SYNC_NOW','RESET_USAGE','SET_LOCK_STYLE','DEDUCT_USAGE')",
            name="type_valid",
        ),
        CheckConstraint(
            "status IN ('pending','delivered','acked','expired')", name="status_valid"
        ),
        Index("ix_remote_commands_device_status", "device_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # V5：删账号后指令历史必须留存（PRD D5），故改为可空 + SET NULL；
    # 操作人姓名在删账号事务内先快照进 created_by_name，再执行 DELETE users。
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="操作人姓名快照（账号被删后用于展示「已删除账号（原 X）」）"
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    device: Mapped["Device"] = relationship(back_populates="commands")

    @property
    def payload(self) -> dict[str, Any]:
        """反序列化指令参数。"""
        try:
            value = json.loads(self.payload_json)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @payload.setter
    def payload(self, value: dict[str, Any]) -> None:
        """序列化指令参数。"""
        self.payload_json = json.dumps(value or {}, ensure_ascii=False)

    @property
    def result(self) -> dict[str, Any] | None:
        """反序列化执行结果。"""
        if not self.result_json:
            return None
        try:
            value = json.loads(self.result_json)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @result.setter
    def result(self, value: dict[str, Any] | None) -> None:
        """序列化执行结果。"""
        self.result_json = None if value is None else json.dumps(value, ensure_ascii=False)
