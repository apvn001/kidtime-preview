"""KidTime 便携单机版常量。

本包是独立单机形态（不联网、不外联任何服务器），下述常量为该形态专属。
"""
from __future__ import annotations

import os

# 内置管理员用户名（单机自包含，不暴露登录入口）。
# 🔴 S2 整改：用户名非敏感故保持固定；**口令与 JWT 密钥一律不写入源码**，
# 由 ``portable/runtime_secrets.py`` 在运行期随机生成并落盘
# ``data/server/runtime_secrets.json``，重启复用（见 OPEN_SOURCE 阶段1）。
PREVIEW_ADMIN_USERNAME = "admin"

# 预建模板库（剔除 alembic 后直接起后端用）。
PREVIEW_DB_REVISION_FILE = "db_revision.txt"

# 同源部署：内嵌服务监听 127.0.0.1，端口从环境变量取，缺省 8765。
PREVIEW_SERVER_HOST = "127.0.0.1"


def preview_server_port() -> int:
    """内嵌后端监听端口。"""
    try:
        return int(os.environ.get("KIDTIME_PREVIEW_PORT", "8765"))
    except ValueError:
        return 8765
