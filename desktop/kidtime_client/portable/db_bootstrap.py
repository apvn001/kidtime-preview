"""便携体验版（preview）数据库初始化。

首次启动从内嵌预建模板库复制出 ``data/server/kidtime.db``（免去 alembic
迁移，正好支撑瘦身目标）。模板库已含 16 张表 + 内置规则模板 + alembic_version。
"""
from __future__ import annotations

import logging
import shutil

from .paths import server_db_path, server_dir, server_logs_dir, template_db_path

logger = logging.getLogger(__name__)


def ensure_database() -> bool:
    """确保数据库存在。

    Returns:
        首次创建返回 True，已存在返回 False。
    """
    db = server_db_path()
    if db.exists():
        return False

    server_dir().mkdir(parents=True, exist_ok=True)
    server_logs_dir().mkdir(parents=True, exist_ok=True)

    template = template_db_path()
    if not template.exists():
        logger.error("预建模板库缺失：%s", template)
        raise FileNotFoundError(f"preview 模板库未找到：{template}")

    shutil.copy(template, db)
    # 复制 WAL/SHM 伴随文件（若存在）。
    for suffix in (".db-wal", ".db-shm"):
        src = template.with_suffix(suffix) if template.suffix == ".db" else None
        if src and src.exists():
            shutil.copy(src, db.with_suffix(suffix))
    logger.info("已从模板库初始化数据库：%s", db)
    return True
