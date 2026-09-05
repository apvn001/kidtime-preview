"""KidTime 便携单机版内嵌后端与单机闭环的运行时包。

本包承载内嵌 FastAPI 后端、单机自动配对与本地数据目录，是独立单机
形态（不再外联任何服务器）的核心运行时。
"""
from __future__ import annotations

from .constants import (
    PREVIEW_ADMIN_USERNAME,
    PREVIEW_DB_REVISION_FILE,
    PREVIEW_SERVER_HOST,
    preview_server_port,
)

__all__ = [
    "PREVIEW_ADMIN_USERNAME",
    "PREVIEW_DB_REVISION_FILE",
    "PREVIEW_SERVER_HOST",
    "preview_server_port",
]
