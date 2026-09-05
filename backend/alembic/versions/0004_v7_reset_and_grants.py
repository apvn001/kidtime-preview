"""v7: 重置当日用量 + 下发时长（§2.2 / §3.3）

变更内容：
  1. 新建 `time_grants` 表（管理员下发当日额外额度，复用 credits 下行链路）。
  2. 重建 `remote_commands`：在 `type_valid` CHECK 中追加 'RESET_USAGE'
     （admin-only 指令）。

⚠️ 关于 CHECK 约束的 SQLite 陷阱（§4.2）：
  SQLite 的 `ALTER TABLE` **无法**删除 / 修改 CHECK 约束；若只做
  `batch_alter_table` 的 `drop_constraint` + `create_check_constraint` 而不显式
  重建整表，新 CHECK 会被**静默丢弃**（batch 模式用临时表搬运数据时只搬运列与
  索引，旧 CHECK 随临时表定义消失，重建回正式表时又按元数据里的旧 CHECK 生成）。
  因此本迁移用 `copy_from` 提供**完整的新表定义**（含 RESET_USAGE 的 type_valid），
  并 `recreate='always'` 强制整表重建；upgrade 末尾再用 `sqlite_master` 正则断言
  type_valid 确实包含 RESET_USAGE，防止重建后 CHECK 再次丢失。

⚠️ 外键开关由 `alembic/env.py` 在事务外处理（PRAGMA 在事务内是 no-op），
  本文件内不得再写 `PRAGMA foreign_keys`。

Revision ID: 0004_v7_reset_and_grants
Revises: 0003_v5_delete_semantics
Create Date: 2026-08-15 09:00:00.000000

"""

from __future__ import annotations

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.base  # noqa: F401  提供 UtcDateTime 自定义类型


revision: str = "0004_v7_reset_and_grants"
down_revision: Union[str, None] = "0003_v5_delete_semantics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 操作人姓名快照列统一类型（与 users.username 的 String(64) 对齐）
_SNAP = sa.String(length=64)


# 本次需要整表重建的表（仅 remote_commands，time_grants 为新建）
_REBUILT_TABLES: tuple[str, ...] = ("remote_commands",)


def _assert_not_referenced(bind, tables: Sequence[str]) -> None:
    """确认这些表没有被其他表通过外键引用（复用 0003 守卫）。

    详见 0003_v5_delete_semantics._assert_not_referenced：SQLite ≥ 3.25 在
    RENAME 时会自动改写其他表的外键子句，可能把引用改指到临时表。经静态核查
    remote_commands 为叶子表，此处再做一次运行时断言兜底。
    """
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


def _remote_commands_v7() -> sa.Table:
    """返回「含 RESET_USAGE 的 type_valid」完整新表定义，供 batch 重建 copy_from。"""
    return sa.Table(
        "remote_commands",
        sa.MetaData(),
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
        sa.Column("created_by_name", _SNAP, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("acked_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "type IN ('UNLOCK_TEMP','PAUSE_ENFORCEMENT','RESUME_ENFORCEMENT','SYNC_NOW','RESET_USAGE')",
            name="type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','delivered','acked','expired')", name="status_valid"
        ),
        sa.Index("ix_remote_commands_device_status", "device_id", "status"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _assert_not_referenced(bind, _REBUILT_TABLES)

    # ── 1) 新建 time_grants ───────────────────────────────────────────────
    op.create_table(
        "time_grants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(length=36),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(length=16),
            sa.CheckConstraint(
                "status IN ('granted','credited','revoked','expired')", name="status_valid"
            ),
            nullable=False,
            server_default="granted",
        ),
        sa.Column(
            "granted_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("granted_by_name", _SNAP, nullable=True),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.Column("credited_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("minutes BETWEEN 1 AND 240", name="minutes_range"),
        sa.Index("ix_time_grants_device_date", "device_id", "target_date"),
        sa.Index("ix_time_grants_device_status", "device_id", "status"),
    )

    # ── 2) 重建 remote_commands：type_valid 追加 RESET_USAGE ──────────────
    #     copy_from 提供完整新定义 + recreate='always' 强制整表重建，
    #     避免 CHECK 静默丢失（见模块 docstring）。
    with op.batch_alter_table(
        "remote_commands", schema=None, recreate="always", copy_from=_remote_commands_v7()
    ) as _batch_op:
        pass

    # ── 3) 断言：type_valid 确实包含 RESET_USAGE（防重建后 CHECK 再丢失）───
    if bind.dialect.name == "sqlite":
        sql_text = "\n".join(
            row[0]
            for row in bind.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='remote_commands'"
            ).fetchall()
            if row[0]
        )
        if not re.search(r"type_valid[^,]*RESET_USAGE", sql_text) and "RESET_USAGE" not in sql_text:
            raise RuntimeError(
                "迁移校验失败：remote_commands.type_valid 未包含 RESET_USAGE，CHECK 可能已丢失。"
            )


def downgrade() -> None:
    """降级。

    ⚠️ 若存在 RESET_USAGE 指令记录（已下发的重置指令），重建会丢失该枚举值，
    导致这些行与外键语义冲突；故先断言无 RESET_USAGE 行，否则中止。

    Raises:
        RuntimeError: 已存在 RESET_USAGE 指令，物理上不可无损降级。
    """
    bind = op.get_bind()
    existing = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM remote_commands WHERE type = 'RESET_USAGE'"
    ).scalar()
    if existing:
        raise RuntimeError(
            f"无法降级：remote_commands 存在 {existing} 条 RESET_USAGE 指令记录，"
            "降级会丢失该枚举值。请改用备份文件整体还原。"
        )

    # 删除 v7 新增的 time_grants 表
    op.drop_table("time_grants")

    # 重建 remote_commands 回到 v6 的 4 值 type_valid（不含 RESET_USAGE）
    old_def = sa.Table(
        "remote_commands",
        sa.MetaData(),
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
        sa.Column("created_by_name", _SNAP, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("acked_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "type IN ('UNLOCK_TEMP','PAUSE_ENFORCEMENT','RESUME_ENFORCEMENT','SYNC_NOW')",
            name="type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','delivered','acked','expired')", name="status_valid"
        ),
        sa.Index("ix_remote_commands_device_status", "device_id", "status"),
    )
    with op.batch_alter_table(
        "remote_commands", schema=None, recreate="always", copy_from=old_def
    ) as _batch_op:
        pass
