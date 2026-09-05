"""客户端版本发布模型：`client_versions`（PC 客户端更新 MVP 方案 A）。

一行记录 = 一次「某通道 + 某版本」的发布。设计要点：

* ``UNIQUE(channel, version)`` 保证同通道同版本只有一行，使「重复发布」天然成为
  幂等 upsert（``client_version_service.publish``）而不是产生脏重复行。
* 同通道内**至多一行** ``is_latest=True``，由 ``publish()`` 在同一事务内先清位
  再置位来保证；查询侧因此可以用 ``ix_client_versions_channel_latest`` 一次命中。
* ``min_version`` 是**强制更新下限**：客户端 ``current_version < min_version``
  即必须更新，不给「稍后」。它随每次发布一起下发，便于事后收紧。
* ``sha256`` 由发布方（CI / 发布脚本）计算并传入，后端**不回源校验**
  （已批准决策：download_url 用长期公开直链，后端不下载文件）。
* ``published_by_name`` 是发布人姓名快照：账号被删后（FK ``SET NULL``）
  历史发布记录依然可追溯（与 V5 §2.2 的姓名快照约定一致）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import CHANNEL_STABLE
from app.db.base import Base, UtcDateTime, utcnow


class ClientVersion(Base):
    """PC 客户端发布版本（含强制更新下限与下载物元信息）。"""

    __tablename__ = "client_versions"
    __table_args__ = (
        # 幂等 upsert 的依据：同通道同版本唯一。
        UniqueConstraint(
            "channel", "version", name="uq_client_versions_channel_version"
        ),
        # GET /client/version/latest 的主查询路径。
        Index("ix_client_versions_channel_latest", "channel", "is_latest"),
        # POST /client/version/check 读取强制下限时的辅助路径。
        Index("ix_client_versions_channel_min", "channel", "min_version"),
        # MVP 只有 stable 一个通道；未来加 beta 需走整表重建迁移（SQLite 无法改 CHECK）。
        CheckConstraint("channel IN ('stable')", name="channel_valid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    build_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CHANNEL_STABLE
    )
    min_version: Mapped[str] = mapped_column(String(32), nullable=False)
    download_url: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    published_by_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    published_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"ClientVersion(channel={self.channel!r}, version={self.version!r}, "
            f"is_latest={self.is_latest!r})"
        )
