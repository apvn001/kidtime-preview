"""设备相关模型：`devices` / `device_credentials` / `pairing_codes`（§3.4–§3.6）。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import DEFAULT_DEVICE_NAME, DEFAULT_TIMEZONE
from app.db.base import Base, UtcDateTime, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.command import RemoteCommand
    from app.models.event import EventLog
    from app.models.extension import ExtensionRequest
    from app.models.grant import TimeGrant
    from app.models.rule import RuleProfile
    from app.models.usage import DailyUsage


class Device(Base):
    """受管电脑（`device_id` 全局不可变，D06）。"""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("status IN ('active','retired')", name="status_valid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default=DEFAULT_DEVICE_NAME)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default=DEFAULT_TIMEZONE)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    client_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_info: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    last_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_state_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    paired_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    credentials: Mapped[list["DeviceCredential"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )
    rule_profile: Mapped["RuleProfile | None"] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )
    daily_usages: Mapped[list["DailyUsage"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )
    extension_requests: Mapped[list["ExtensionRequest"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )
    commands: Mapped[list["RemoteCommand"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list["EventLog"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )
    # V7 §3.3.2：下发时长记录。与 time_grants.device 互为 back_populates，
    # 缺失该端会导致 TimeGrant mapper 初始化失败并连带拖垮全部 mapper。
    time_grants: Mapped[list["TimeGrant"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", passive_deletes=True
    )


class DeviceCredential(Base):
    """设备同步凭证（D41/D42，S5：sha256 索引 + bcrypt cost 10 校验）。"""

    __tablename__ = "device_credentials"
    __table_args__ = (
        Index(
            "uq_device_credentials_active",
            "device_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_lookup: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    device: Mapped["Device"] = relationship(back_populates="credentials")


class PairingCode(Base):
    """一次性配对码（D01/D02/D03/D05）。"""

    __tablename__ = "pairing_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    code_prefix: Mapped[str] = mapped_column(String(4), nullable=False)
    # V5：配对码是分钟级 TTL 的瞬时活凭据，无审计价值。创建人被删 / 设备被删时
    # 一并物理清除（增量架构设计 §2.1 第 1、2 项，§10-Q1）。故保持 NOT NULL + CASCADE，
    # 不设姓名快照列。
    created_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    used_by_device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
