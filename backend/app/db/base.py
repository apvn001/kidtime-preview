"""SQLAlchemy 声明基类、命名约定与通用类型（ARCHITECTURE.md §3）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """返回当前 UTC 时间（带时区）。所有 `*_at` 字段统一用它（D10）。"""
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator[datetime]):
    """始终以 UTC 存取的 `DateTime`。

    SQLite 的 DATETIME 不保存时区信息，直接使用 `DateTime(timezone=True)` 会导致
    写入时丢失 tzinfo、读出时得到 naive datetime，与 `datetime.now(timezone.utc)`
    比较会抛 `TypeError`。本类型在绑定时统一转换成 UTC，在取回时补回 UTC tzinfo。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        """写库前统一转为带 UTC 时区的 datetime。"""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        """读库后补回 UTC 时区。"""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """提供 `created_at` / `updated_at` 两列的混入。"""

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
