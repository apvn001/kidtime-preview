"""幂等键模型：`idempotency_keys`（§3.12 / D47）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UtcDateTime, utcnow


class IdempotencyKey(Base):
    """写请求幂等记录，TTL 24 小时。"""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint("state IN ('in_progress','completed')", name="state_valid"),
    )

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
