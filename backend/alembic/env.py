"""Alembic 运行环境。

URL 从 `Settings` 读取（不写死在 alembic.ini），并强制 `render_as_batch=True`
—— SQLite 的 ALTER 能力有限，批处理模式是必需的。

V5 增量（增量架构设计 §3.2 决策 B）：
    SQLite 的 ``PRAGMA foreign_keys`` **在事务内是 no-op**，写在迁移脚本的
    ``upgrade()`` 里等于没写。`batch_alter_table` 的「12 步重建法」需要在外键
    关闭状态下复制数据，因此开关必须提到 ``context.begin_transaction()``
    **之前**（pysqlite 不会为 PRAGMA 语句隐式开启事务，此处才真正生效）。

    同时在迁移前后各跑一次 ``PRAGMA foreign_key_check`` 作为门禁：
      - 前置：原库若已有孤儿行，FK OFF 期间会被静默复制进新表；
      - 后置：重建后的表若外键指向错误（RENAME 连带改写陷阱）会在此暴露。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  确保全部模型被注册

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 门禁输出里最多展示多少条问题行，避免刷屏
_MAX_REPORTED_ROWS = 10


def _resolve_url() -> str:
    """解析数据库 URL：优先 `DATABASE_URL` 环境变量，其次 Settings 默认值。"""
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    try:
        from app.core.config import get_settings

        return get_settings().DATABASE_URL
    except Exception:  # pragma: no cover - JWT_SECRET 缺失等情况的兜底
        from app.core.config import default_data_dir

        return f"sqlite:///{(default_data_dir('server') / 'kidtime.db').as_posix()}"


DATABASE_URL = _resolve_url()
config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata


def _foreign_key_check(connection) -> list[tuple]:
    """执行 `PRAGMA foreign_key_check`，返回违规行列表（空列表表示通过）。

    Args:
        connection: 已建立的 SQLAlchemy `Connection`。

    Returns:
        违规行元组列表，格式为 `(表名, rowid, 被引用表, 外键序号)`。
    """
    return list(connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall())


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL。"""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。

    SQLite 场景下附加外键开关与前/后置 `foreign_key_check` 门禁。
    """
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = DATABASE_URL
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"

        if is_sqlite:
            # ⚠️ 必须在 begin_transaction() 之前执行：PRAGMA foreign_keys 在事务内是 no-op。
            #    pysqlite 不会为 PRAGMA/SELECT 语句向 SQLite 发出 BEGIN，故此处真正生效。
            orphans = _foreign_key_check(connection)
            if orphans:
                raise RuntimeError(
                    "迁移中止：升级前数据库已存在孤儿外键行，"
                    f"共 {len(orphans)} 条，前 {_MAX_REPORTED_ROWS} 条为 "
                    f"{orphans[:_MAX_REPORTED_ROWS]}。请先修数据再升级。"
                )
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            # 🔴 关键：SQLAlchemy 2.x 的 Connection 会在首次 execute 时「自动开启」
            #    逻辑事务。若不显式结束，Alembic 会认为事务由外部托管而**不再提交**，
            #    导致迁移全部静默回滚（现象：alembic_version 为空、DDL 未落盘）。
            #    PRAGMA foreign_keys 是连接级设置，rollback 不会将其复位。
            connection.rollback()

        migration_failed = False
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        except Exception:
            migration_failed = True
            raise
        finally:
            # 无论迁移成败都要把外键开关恢复，避免连接被复用时处于 OFF 态
            if is_sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                if migration_failed:
                    connection.rollback()

        if is_sqlite:
            bad = _foreign_key_check(connection)
            connection.rollback()
            if bad:
                raise RuntimeError(
                    "迁移后外键校验失败（请立即用备份回滚）："
                    f"共 {len(bad)} 条，前 {_MAX_REPORTED_ROWS} 条为 "
                    f"{bad[:_MAX_REPORTED_ROWS]}"
                )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
