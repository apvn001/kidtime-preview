"""用户相关模型：`users` / `refresh_tokens` / `user_preferences`（§3.1–§3.3）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import DEFAULT_DASHBOARD_LAYOUT_JSON
from app.db.base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    pass


class User(Base):
    """Web 账号（D35/D36）。"""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','parent')", name="role_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="parent")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    preference: Mapped["UserPreference | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )


class RefreshToken(Base):
    """刷新令牌轮换链（D37）。"""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class UserPreference(Base):
    """管理端个人偏好（仪表盘布局 + 主题）。"""

    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "theme IN ('light','dark','system','default','eyecare','eyecare2')",
            name="theme_valid",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    dashboard_layout_json: Mapped[str] = mapped_column(
        String(512), nullable=False, default=DEFAULT_DASHBOARD_LAYOUT_JSON
    )
    theme: Mapped[str] = mapped_column(String(16), nullable=False, default="default")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    user: Mapped["User"] = relationship(back_populates="preference")

    @property
    def dashboard_layout(self) -> list[str]:
        """反序列化仪表盘布局。"""
        try:
            value = json.loads(self.dashboard_layout_json)
        except (TypeError, ValueError):
            return list(json.loads(DEFAULT_DASHBOARD_LAYOUT_JSON))
        return [str(item) for item in value] if isinstance(value, list) else []

    @dashboard_layout.setter
    def dashboard_layout(self, value: list[str]) -> None:
        """序列化仪表盘布局。"""
        self.dashboard_layout_json = json.dumps(list(value), ensure_ascii=False)
