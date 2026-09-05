"""v7 功能三: 扣减用量（§2.2 / §3.5）

变更内容：
  1. `daily_usage` 新增 `deducted_minutes` 审计列（累计被管理端扣减的分钟，
     不进入 remaining 公式——扣减通过 `used_minutes += applied` 生效）。
  2. 重建 `remote_commands`：在 `type_valid` CHECK 中追加 'DEDUCT_USAGE'
     （admin-only 指令，镜像 RESET_USAGE / SET_LOCK_STYLE）。

⚠️ 关于 CHECK 约束的 SQLite 陷阱（同 0004/0005）：SQLite 的 `ALTER TABLE` 无法
删除 / 修改 CHECK 约束；必须用 `copy_from` 提供完整新表定义 + `recreate='always'`
强制整表重建，并在 upgrade 末尾用 `sqlite_master` 正则断言 type_valid 确实包含
DEDUCT_USAGE，防止重建后 CHECK 再次丢失。

⚠️ 外键开关由 `alembic/env.py` 在事务外处理，本文件内不得再写 `PRAGMA foreign_keys`。

Revision ID: 0008_deduct_usage
Revises: 0007_client_versions
Create Date: 2026-08-20 00:20:00.000000

"""

from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.base  # noqa: F401  提供 UtcDateTime 自定义类型
from app.db.base import NAMING_CONVENTION


revision: str = "0008_deduct_usage"
down_revision: Union[str, None] = "0007_client_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 本次需要整表重建的表（仅 remote_commands；daily_usage 走 ALTER 加列）
_REBUILT_TABLES: tuple[str, ...] = ("remote_commands",)


def _assert_not_referenced(bind, tables: Sequence[str]) -> None:
    """确认这些表没有被其他表通过外键引用（复用 0004/0005 守卫）。"""
    names = [
        row[0]
        for row in bind.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    targets = set(tables)
    for table_name in names:
        if table_name in targets:
            continue
        rows = bind.exec_driver_sql(
            f'PRAGMA foreign_key_list("{table_name}")'
        ).fetchall()
        for fk in rows:
            referred = fk[2]
            if referred in targets:
                raise RuntimeError(
                    f"迁移中止：表 {table_name} 通过外键引用了待重建表 {referred}，"
                    "整表重建会连带改写其外键指向，需改用显式重建流程。"
                )


def _remote_commands_v7deduct() -> sa.Table:
    """返回「含 DEDUCT_USAGE 的 type_valid」完整新表定义，供 batch 重建 copy_from。

    ⚠️ MetaData 必须带 `NAMING_CONVENTION`，索引必须声明齐全
    （对齐 0005 的纠偏注释：0004 曾导致约束名退化与丢索引，0005 起回归 0001 命名）。
    """
    return sa.Table(
        "remote_commands",
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(length=36),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by_name", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("acked_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "type IN ('UNLOCK_TEMP','PAUSE_ENFORCEMENT','RESUME_ENFORCEMENT','SYNC_NOW','RESET_USAGE','SET_LOCK_STYLE','DEDUCT_USAGE')",
            name="type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','delivered','acked','expired')", name="status_valid"
        ),
        sa.Index("ix_remote_commands_batch_id", "batch_id"),
        sa.Index("ix_remote_commands_device_id", "device_id"),
        sa.Index("ix_remote_commands_status", "status"),
        sa.Index("ix_remote_commands_device_status", "device_id", "status"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _assert_not_referenced(bind, _REBUILT_TABLES)

    # ── 1) daily_usage 追加 deducted_minutes 审计列 ────────────────────────
    op.add_column(
        "daily_usage",
        sa.Column(
            "deducted_minutes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # ── 2) 重建 remote_commands：type_valid 追加 DEDUCT_USAGE ───────────────
    with op.batch_alter_table(
        "remote_commands", schema=None, recreate="always", copy_from=_remote_commands_v7deduct()
    ) as _batch_op:
        pass

    # ── 3) 断言：type_valid 确实包含 DEDUCT_USAGE（防重建后 CHECK 再丢失）──
    if bind.dialect.name == "sqlite":
        sql_text = "\n".join(
            row[0]
            for row in bind.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='remote_commands'"
            ).fetchall()
            if row[0]
        )
        if (
            not re.search(r"type_valid[^,]*DEDUCT_USAGE", sql_text)
            and "DEDUCT_USAGE" not in sql_text
        ):
            raise RuntimeError(
                "迁移校验失败：remote_commands.type_valid 未包含 DEDUCT_USAGE，CHECK 可能已丢失。"
            )

        # 断言：约束名符合 NAMING_CONVENTION（防再次退化成裸名）
        for expected in (
            "pk_remote_commands",
            "ck_remote_commands_type_valid",
            "ck_remote_commands_status_valid",
            "fk_remote_commands_device_id_devices",
            "fk_remote_commands_created_by_users",
        ):
            if expected not in sql_text:
                raise RuntimeError(
                    f"迁移校验失败：remote_commands 缺少符合命名约定的约束 {expected}，"
                    "约束名可能又退化成了裸名。"
                )

        # 断言：4 个索引齐全（防重建再次静默丢索引）
        index_names = {
            row[0]
            for row in bind.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='remote_commands' AND name IS NOT NULL"
            ).fetchall()
        }
        for expected_ix in (
            "ix_remote_commands_batch_id",
            "ix_remote_commands_device_id",
            "ix_remote_commands_status",
            "ix_remote_commands_device_status",
        ):
            if expected_ix not in index_names:
                raise RuntimeError(
                    f"迁移校验失败：remote_commands 缺少索引 {expected_ix}，"
                    f"整表重建可能又把它丢了。当前索引：{sorted(index_names)}"
                )


def downgrade() -> None:
    """降级。

    ⚠️ 若存在 DEDUCT_USAGE 指令记录，重建会丢失该枚举值，导致这些行与 CHECK 冲突；
    故先断言无 DEDUCT_USAGE 行，否则中止。

    ⚠️ 降级只回退「type_valid 枚举」与 deducted_minutes 列这两项 schema 增量，
    **不**回退约束命名与索引补齐（对齐 0005 注释：那是对 0004 引入缺陷的纠偏）。
    """
    bind = op.get_bind()
    existing = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM remote_commands WHERE type = 'DEDUCT_USAGE'"
    ).scalar()
    if existing:
        raise RuntimeError(
            f"无法降级：remote_commands 存在 {existing} 条 DEDUCT_USAGE 指令记录，"
            "降级会丢失该枚举值。请改用备份文件整体还原。"
        )

    # 删除 deducted_minutes 审计列
    op.drop_column("daily_usage", "deducted_minutes")

    # 重建回不含 DEDUCT_USAGE 的 6 值 type_valid（保留 RESET_USAGE / SET_LOCK_STYLE）
    old_def = sa.Table(
        "remote_commands",
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(length=36),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by_name", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("acked_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "type IN ('UNLOCK_TEMP','PAUSE_ENFORCEMENT','RESUME_ENFORCEMENT','SYNC_NOW','RESET_USAGE','SET_LOCK_STYLE')",
            name="type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','delivered','acked','expired')", name="status_valid"
        ),
        sa.Index("ix_remote_commands_batch_id", "batch_id"),
        sa.Index("ix_remote_commands_device_id", "device_id"),
        sa.Index("ix_remote_commands_status", "status"),
        sa.Index("ix_remote_commands_device_status", "device_id", "status"),
    )
    with op.batch_alter_table(
        "remote_commands", schema=None, recreate="always", copy_from=old_def
    ) as _batch_op:
        pass
