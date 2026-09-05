"""PC 客户端版本更新（MVP 方案 A）：新增 `client_versions` 表

变更内容：
  仅**新增一张表** `client_versions`，不触碰任何既有表，因此无需 `batch_alter_table`
  整表重建（对比 0004 / 0005 / 0006 的 CHECK 改造）。

⚠️ 约束命名必须与 ORM 侧 `Base.metadata` 的 NAMING_CONVENTION 解析结果**逐字一致**，
否则 `alembic upgrade head` 建出的库与 `Base.metadata.create_all()`（测试用）建出的库
DDL 漂移，autogenerate 会持续报虚假 diff。本文件显式写死解析后的最终名：

  ============================================  =========================================
  ORM 侧写法                                    本文件必须写的最终名
  ============================================  =========================================
  ``primary_key=True``                          ``pk_client_versions``
  ``UniqueConstraint(name="uq_..._version")``   原样保留（uq 模板不含 constraint_name）
  ``CheckConstraint(name="channel_valid")``     ``ck_client_versions_channel_valid``
  ``ForeignKey("users.id")``                    ``fk_client_versions_published_by_users``
  ``Index("ix_client_versions_...")``           原样保留（ix 模板不含 constraint_name）
  ============================================  =========================================

⚠️ 列默认值说明：`build_number` / `is_latest` / `published_at` 等在 ORM 层都是
**Python 端默认值**（`mapped_column(..., default=...)`），不是 `server_default`。
故本迁移刻意不写 `server_default`，与 models/client_version.py 严格对齐。

⚠️ 外键开关由 `alembic/env.py` 在事务外处理，本文件内不得再写 `PRAGMA foreign_keys`。

Revision ID: 0007_client_versions
Revises: 0006_theme_styles
Create Date: 2026-08-27 10:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.base  # noqa: F401  提供 UtcDateTime 自定义类型


revision: str = "0007_client_versions"
down_revision: Union[str, None] = "0006_theme_styles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE: str = "client_versions"

# MVP 只有 stable 一个通道（已批准决策）。未来加 beta 需整表重建改 CHECK。
_CHANNEL_CHECK: str = "channel IN ('stable')"


def upgrade() -> None:
    """新建 `client_versions` 表 + 两个查询索引。"""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("build_number", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("min_version", sa.String(length=32), nullable=False),
        sa.Column("download_url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.Column("is_latest", sa.Boolean(), nullable=False),
        sa.Column("published_by", sa.Integer(), nullable=True),
        sa.Column("published_by_name", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_client_versions"),
        sa.UniqueConstraint(
            "channel", "version", name="uq_client_versions_channel_version"
        ),
        sa.CheckConstraint(_CHANNEL_CHECK, name="ck_client_versions_channel_valid"),
        sa.ForeignKeyConstraint(
            ["published_by"],
            ["users.id"],
            name="fk_client_versions_published_by_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_client_versions_channel_latest", _TABLE, ["channel", "is_latest"]
    )
    op.create_index(
        "ix_client_versions_channel_min", _TABLE, ["channel", "min_version"]
    )


def downgrade() -> None:
    """删除 `client_versions` 表（含其索引）。

    该表是叶子表（只出边指向 users，无入边），可直接整表删除。
    """
    op.drop_index("ix_client_versions_channel_min", table_name=_TABLE)
    op.drop_index("ix_client_versions_channel_latest", table_name=_TABLE)
    op.drop_table(_TABLE)
