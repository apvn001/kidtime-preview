"""本地库版本迁移（基于 ``PRAGMA user_version``）。

版本 1 = ARCHITECTURE.md §4 的 6 张表初始结构。后续新增字段时在
:data:`MIGRATIONS` 追加 ``(版本号, SQL 列表)``，禁止修改已发布的历史条目。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

SCHEMA_FILE: Final[Path] = Path(__file__).with_name("schema.sql")
CURRENT_VERSION: Final[int] = 1

MIGRATIONS: Final[tuple[tuple[int, tuple[str, ...]], ...]] = ()
"""历史增量迁移（版本 1 由 schema.sql 直接建表，故当前为空）。"""


def read_schema_sql() -> str:
    """读取初始建表脚本。

    Returns:
        `schema.sql` 全文。

    Raises:
        FileNotFoundError: 打包时漏带 `schema.sql`。
    """
    if not SCHEMA_FILE.exists():  # pragma: no cover - 打包配置错误才会触发
        raise FileNotFoundError(
            f"缺少建表脚本 {SCHEMA_FILE}。若为 PyInstaller 打包产物，"
            "请确认 spec 的 datas 已包含 kidtime_client/storage/schema.sql"
        )
    return SCHEMA_FILE.read_text(encoding="utf-8")


def get_user_version(conn: sqlite3.Connection) -> int:
    """读取当前 `user_version`。"""
    cursor = conn.execute("PRAGMA user_version")
    try:
        row = cursor.fetchone()
    finally:
        cursor.close()
    return int(row[0]) if row is not None else 0


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """写入 `user_version`（PRAGMA 不支持参数绑定，故用字面量拼接整数）。"""
    conn.execute(f"PRAGMA user_version = {int(version)}")


def migrate(conn: sqlite3.Connection) -> int:
    """把数据库升级到 :data:`CURRENT_VERSION`。

    Args:
        conn: 已设置好 PRAGMA 的连接。

    Returns:
        迁移后的版本号。
    """
    version = get_user_version(conn)
    if version == 0:
        conn.executescript(read_schema_sql())
        _ensure_runtime_row(conn)
        set_user_version(conn, 1)
        version = 1
        logger.info("本地库初始化完成，schema_version=1")

    for target, statements in MIGRATIONS:
        if version >= target:
            continue
        for statement in statements:
            conn.execute(statement)
        set_user_version(conn, target)
        version = target
        logger.info("本地库已迁移至 schema_version=%d", target)

    _ensure_runtime_row(conn)
    return version


def _ensure_runtime_row(conn: sqlite3.Connection) -> None:
    """保证 `runtime_state` 存在唯一的 ``id = 1`` 行。"""
    conn.execute(
        "INSERT OR IGNORE INTO runtime_state (id, last_state, clean_shutdown) "
        "VALUES (1, 'ACTIVE', 1)"
    )
