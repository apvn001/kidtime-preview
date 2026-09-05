"""v1.5: 管理端视觉主题三样式（默认 / 护眼 / 护眼 2）

变更内容：
  重建 `user_preferences`：把 `theme_valid` CHECK 从 3 个值扩展为 6 个值，
  新增 'default' / 'eyecare' / 'eyecare2'，同时保留旧值 'light' / 'dark' /
  'system'（向后兼容，已有数据行存的就是旧值，不能让老数据变非法）。

⚠️ 关于 CHECK 约束的 SQLite 陷阱（同 0004 / 0005）：SQLite 的 `ALTER TABLE` 无法
删除 / 修改 CHECK 约束；必须用 `copy_from` 提供完整新表定义 + `recreate='always'`
强制整表重建，并在 upgrade 末尾用 `sqlite_master` 断言新枚举值确实写入了 CHECK，
防止重建后 CHECK 静默丢失。

⚠️ 列默认值说明：`UserPreference.theme` 在 ORM 层是 Python 端默认值
（`mapped_column(..., default="default")`），**不是** `server_default`；当前库里的
真实 DDL 也没有 DEFAULT 子句。故本迁移刻意不引入 `server_default`，以保持
models/user.py 与库表 DDL 严格一致（否则 autogenerate 会出现虚假 diff）。
主题的默认值由 `preference_service.get_or_create()` 写入 'default'。

⚠️ 约束命名：本文件的 `sa.MetaData` 必须带上 `NAMING_CONVENTION`，否则重建后
约束名会从 `ck_user_preferences_theme_valid` 退化成 `theme_valid`，与模型漂移。

⚠️ 外键开关由 `alembic/env.py` 在事务外处理，本文件内不得再写 `PRAGMA foreign_keys`。

Revision ID: 0006_theme_styles
Revises: 0005_v1_4_2_lock_style
Create Date: 2026-08-20 10:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.base  # noqa: F401  提供 UtcDateTime 自定义类型
from app.db.base import NAMING_CONVENTION


revision: str = "0006_theme_styles"
down_revision: Union[str, None] = "0005_v1_4_2_lock_style"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 本次需要整表重建的表（仅 user_preferences，沿用 0004 / 0005 模式）
_REBUILT_TABLES: tuple[str, ...] = ("user_preferences",)

# 扩展后的 6 个合法主题值：前 3 个为历史兼容值，后 3 个为本次新增
_THEME_NEW: str = "theme IN ('light','dark','system','default','eyecare','eyecare2')"

# 收窄回历史的 3 个值（供 downgrade 使用）
_THEME_OLD: str = "theme IN ('light','dark','system')"

# downgrade 时需要回写成 'light' 的新枚举值
_NEW_ONLY_THEMES: tuple[str, ...] = ("default", "eyecare", "eyecare2")


def _assert_not_referenced(bind, tables: Sequence[str]) -> None:
    """确认这些表没有被其他表通过外键引用（复用 0004 / 0005 守卫）。

    经静态核查 user_preferences 为叶子表（只出边指向 users，无入边），
    此处再做一次运行时断言兜底，防止后续新增表引用它后整表重建改写外键指向。
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


def _user_preferences(theme_check: str) -> sa.Table:
    """返回 `user_preferences` 的完整表定义，供 batch 重建 copy_from。

    列定义与 `app/models/user.py::UserPreference` 严格一致：
    4 列 + 主键 + theme CHECK + 指向 users 的级联外键，无额外索引。

    Args:
        theme_check: `theme_valid` CHECK 的 SQL 表达式（升级用 6 值 / 降级用 3 值）。
    """
    return sa.Table(
        "user_preferences",
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column("user_id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("dashboard_layout_json", sa.String(length=512), nullable=False),
        sa.Column("theme", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(theme_check, name="theme_valid"),
    )


def _assert_theme_check(bind, *, must_contain: Sequence[str], must_absent: Sequence[str]) -> None:
    """断言重建后的 user_preferences DDL 中 CHECK 枚举值符合预期。"""
    sql_text = "\n".join(
        row[0]
        for row in bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='user_preferences'"
        ).fetchall()
        if row[0]
    )
    if "theme_valid" not in sql_text:
        raise RuntimeError(
            "迁移校验失败：user_preferences 缺少 theme_valid 约束，CHECK 可能已丢失。"
        )
    for value in must_contain:
        if f"'{value}'" not in sql_text:
            raise RuntimeError(
                f"迁移校验失败：user_preferences.theme_valid 未包含 '{value}'，"
                "CHECK 可能已丢失或写错。"
            )
    for value in must_absent:
        if f"'{value}'" in sql_text:
            raise RuntimeError(
                f"迁移校验失败：user_preferences.theme_valid 仍包含 '{value}'，"
                "CHECK 未按预期收窄。"
            )


def upgrade() -> None:
    """扩展 theme CHECK 至 6 个值（新增 default / eyecare / eyecare2）。"""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _assert_not_referenced(bind, _REBUILT_TABLES)

    # 整表重建：copy_from 提供完整新定义 + recreate='always'，避免 CHECK 静默丢失。
    # 新 CHECK 是旧 CHECK 的超集，存量行（light/dark/system）不会与之冲突。
    with op.batch_alter_table(
        "user_preferences",
        schema=None,
        recreate="always",
        copy_from=_user_preferences(_THEME_NEW),
    ) as _batch_op:
        pass

    # 断言：6 个值确实都在 CHECK 里（防重建后 CHECK 再丢失）
    if bind.dialect.name == "sqlite":
        _assert_theme_check(
            bind,
            must_contain=("light", "dark", "system", "default", "eyecare", "eyecare2"),
            must_absent=(),
        )


def downgrade() -> None:
    """收窄 theme CHECK 回 3 个值。

    ⚠️ 收窄 CHECK 前必须先把新枚举值回写成 'light'，否则整表重建时
    存量的 default / eyecare / eyecare2 行会与 3 值 CHECK 冲突而失败。
    这里选择降级为 'light'（旧版本的默认主题），属于有损降级但不阻塞。
    """
    bind = op.get_bind()

    # 先把新枚举值回写为 'light'，避免收窄 CHECK 时冲突
    placeholders = ",".join(f"'{value}'" for value in _NEW_ONLY_THEMES)
    op.execute(
        f"UPDATE user_preferences SET theme = 'light' WHERE theme IN ({placeholders})"
    )

    with op.batch_alter_table(
        "user_preferences",
        schema=None,
        recreate="always",
        copy_from=_user_preferences(_THEME_OLD),
    ) as _batch_op:
        pass

    # 断言：新枚举值已从 CHECK 中移除，旧 3 值仍在
    if bind.dialect.name == "sqlite":
        _assert_theme_check(
            bind,
            must_contain=("light", "dark", "system"),
            must_absent=_NEW_ONLY_THEMES,
        )
