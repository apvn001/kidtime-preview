"""便携体验版（preview）目录布局。

所有数据落在「解压目录/data」下，零安装零配置。打包后模板库存于
``_internal/portable/``，运行时复制到 ``data/server/kidtime.db``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def portable_root() -> Path:
    """预览版根目录：``KIDTIME_PORTABLE_ROOT`` 优先，否则取 exe 所在目录。"""
    root = os.environ.get("KIDTIME_PORTABLE_ROOT")
    if root:
        return Path(root).resolve()
    return Path(sys.executable).resolve().parent


def data_dir() -> Path:
    return portable_root() / "data"


def server_dir() -> Path:
    return data_dir() / "server"


def server_db_path() -> Path:
    return server_dir() / "kidtime.db"


def server_logs_dir() -> Path:
    return server_dir() / "logs"


def client_dir() -> Path:
    return data_dir() / "client"


def template_db_path() -> Path:
    """打包内嵌的预建模板库（已含 16 张表 + 内置规则模板 + alembic_version）。"""
    return portable_root() / "_internal" / "portable" / "kidtime_template.db"
