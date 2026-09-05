"""规则模板模型：`rule_templates`（落地架构设计 §2.1）。

模板 = RuleProfile 的 11 个参数 + name + is_builtin + 版本/审计。
CheckConstraint 与 `rule_profiles` 完全同构（前缀换 `rule_templates`）。
应用模板 = 复制参数到设备（方案 A），`rule_profiles.template_id` 仅作溯源展示。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import (
    DEFAULT_ALLOWED_END,
    DEFAULT_ALLOWED_START,
    DEFAULT_BREAK_MINUTES,
    DEFAULT_CONTINUOUS_LIMIT_MINUTES,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_PARENT_MODE_TIMEOUT_MINUTES,
    DEFAULT_REMINDER_POINTS,
    DEFAULT_REMINDER_POINTS_JSON,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    DEFAULT_WEEKDAY_QUOTA_MINUTES,
    DEFAULT_WEEKEND_QUOTA_MINUTES,
)
from app.db.base import Base, UtcDateTime, utcnow


class RuleTemplate(Base):
    """规则模板（业务对象，id 自增；内置模板不可删除/改名）。"""

    __tablename__ = "rule_templates"
    __table_args__ = (
        CheckConstraint(
            "weekday_quota_minutes BETWEEN 0 AND 1440", name="weekday_quota_range"
        ),
        CheckConstraint(
            "weekend_quota_minutes BETWEEN 0 AND 1440", name="weekend_quota_range"
        ),
        CheckConstraint(
            "continuous_limit_minutes BETWEEN 10 AND 240", name="continuous_limit_range"
        ),
        CheckConstraint("break_minutes BETWEEN 1 AND 60", name="break_minutes_range"),
        CheckConstraint("idle_minutes BETWEEN 1 AND 60", name="idle_minutes_range"),
        CheckConstraint(
            "parent_mode_timeout_minutes BETWEEN 5 AND 120", name="parent_timeout_range"
        ),
        CheckConstraint(
            "sync_interval_seconds BETWEEN 15 AND 300", name="sync_interval_range"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weekday_quota_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_WEEKDAY_QUOTA_MINUTES
    )
    weekend_quota_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_WEEKEND_QUOTA_MINUTES
    )
    allowed_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default=DEFAULT_ALLOWED_START
    )
    allowed_end: Mapped[str] = mapped_column(
        String(5), nullable=False, default=DEFAULT_ALLOWED_END
    )
    continuous_limit_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_CONTINUOUS_LIMIT_MINUTES
    )
    break_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_BREAK_MINUTES
    )
    idle_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_IDLE_MINUTES
    )
    reminder_points_json: Mapped[str] = mapped_column(
        Text, nullable=False, default=DEFAULT_REMINDER_POINTS_JSON
    )
    parent_mode_timeout_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PARENT_MODE_TIMEOUT_MINUTES
    )
    sync_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SYNC_INTERVAL_SECONDS
    )
    enforcement_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # V5：删账号后模板创建人 / 修改人必须可溯源（PRD D5）→ SET NULL + 姓名快照
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="创建人姓名快照（账号被删后仍可溯源）"
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="最后修改人姓名快照（账号被删后仍可溯源）"
    )

    @property
    def reminder_points(self) -> list[int]:
        """反序列化提醒节点（去重降序）。"""
        try:
            value = json.loads(self.reminder_points_json)
        except (TypeError, ValueError):
            return list(DEFAULT_REMINDER_POINTS)
        if not isinstance(value, list):
            return list(DEFAULT_REMINDER_POINTS)
        return [int(item) for item in value]

    @reminder_points.setter
    def reminder_points(self, value: list[int]) -> None:
        """序列化提醒节点，写入前去重并降序。"""
        normalized = sorted({int(item) for item in value}, reverse=True)
        self.reminder_points_json = json.dumps(normalized)
