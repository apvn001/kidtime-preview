"""数据库初始化辅助（供脚本与测试使用）。

🔴 S2（开源整改）：额外提供 ``sync_admin_from_env`` —— 内嵌单机形态在启动时
经环境变量注入内置管理员口令，本函数把库里 admin **同步为运行期口令**
（存在即更新、缺失即创建）。固定口令绝不出现在源码 / 打包产物中。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core.config import get_settings
from app.core.constants import UserRole
from app.core.security import hash_password
from app.db.base import Base, utcnow
from app.db.session import (
    SessionLocal,
    assert_db_path_safe,
    get_engine,
    retry_on_busy,
    sqlite_url_to_path,
)
from app.models.user import User

# 触发全部模型注册到 Base.metadata
import app.models  # noqa: F401  pylint: disable=unused-import

logger = logging.getLogger(__name__)

#: 环境变量名（与 desktop ``portable/runtime_secrets.py`` 保持一致）。
ENV_SEED_ADMIN_USERNAME = "KIDTIME_SEED_ADMIN_USERNAME"
ENV_SEED_ADMIN_PASSWORD = "KIDTIME_SEED_ADMIN_PASSWORD"


def ensure_data_dir() -> Path | None:
    """确保数据库所在目录存在。

    Returns:
        数据库文件路径；非文件型数据库返回 ``None``。
    """
    path = sqlite_url_to_path(get_settings().DATABASE_URL)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def init_database(create_all: bool = True) -> None:
    """执行启动前的数据库准备：路径自检 + 建目录 + 建表。

    Args:
        create_all: 为 ``True`` 时直接用 `Base.metadata.create_all` 建表
            （开发脚本与测试用；生产走 ``alembic upgrade head``）。
    """
    settings = get_settings()
    assert_db_path_safe(settings.DATABASE_URL)
    ensure_data_dir()
    if create_all:
        Base.metadata.create_all(get_engine())
        logger.info("数据库表结构已就绪：%s", settings.DATABASE_URL)


def drop_all() -> None:
    """删除全部表（仅供开发种子脚本 `--reset` 使用）。"""
    Base.metadata.drop_all(get_engine())


@retry_on_busy
def _upsert_admin(db, username: str, password: str) -> bool:
    """把内置 admin 的口令同步为运行期口令。

    Args:
        db: 数据库会话。
        username: 管理员用户名。
        password: 运行期口令明文。

    Returns:
        是否发生了创建 / 更新。

    Raises:
        OSError / sqlalchemy 异常: 读写失败时向上抛出（启动失败即中止，
            避免带着「错误口令的 admin」继续服务）。
    """
    moment = utcnow()
    user = db.query(User).filter(User.role == UserRole.ADMIN.value).first()
    if user is None:
        db.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=UserRole.ADMIN.value,
                is_active=True,
                created_at=moment,
                updated_at=moment,
            )
        )
        db.commit()
        logger.info("内置 admin 不存在，已创建（口令为运行期随机值）")
        return True
    user.password_hash = hash_password(password)
    user.updated_at = moment
    db.commit()
    logger.info("内置 admin 口令已同步为运行期随机值")
    return True


def sync_admin_from_env() -> bool:
    """按环境变量把内置 admin 口令同步为运行期口令（幂等，可重复调用）。

    无环境变量（普通部署 / 1.4.6 既有后端）时**什么都不做**，行为零变化；
    仅内嵌单机形态（便携启动器）会设置这两个变量。语义为 **update-if-exists /
    create-if-missing**：旧库升级（库里 admin 还是旧模板种下的旧口令哈希）也会
    自动收敛到运行期口令，登录不会失败。

    Returns:
        是否执行了同步。
    """
    username = os.environ.get(ENV_SEED_ADMIN_USERNAME, "").strip()
    password = os.environ.get(ENV_SEED_ADMIN_PASSWORD, "").strip()
    if not username or not password:
        return False
    logger.info("检测到内置 admin 种子环境变量，同步运行期口令")
    with SessionLocal() as db:
        return _upsert_admin(db, username, password)
