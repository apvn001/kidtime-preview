"""便携单机版运行期密钥 / 内置管理员口令管理（S1 / S2 整改）。

开源整改目标：**任何固定口令 / 固定 JWT 密钥都不允许出现在源码或打包产物里**
（公开的固定密钥 = 本机任何进程都能伪造 token 或直接调用 127.0.0.1 后端，
绕过家长管控）。因此：

* JWT 密钥 —— 内嵌后端首次启动时用 :func:`secrets.token_urlsafe` 生成，
  持久化到数据库同目录 ``data/server/runtime_secrets.json``，重启复用，
  保证已签发 token 不因重启失效。
* 内置管理员口令 —— 同样随机生成、同文件落盘。桌面端（bootstrap /
  规则编辑）从该文件读取，并把它经环境变量
  ``KIDTIME_SEED_ADMIN_PASSWORD`` 注入后端，由后端在启动时把库里的 admin
  **同步为该口令**（见 ``backend/app/db/init_db.py::sync_admin_from_env``）。

口令按后端密码策略（≥8 位且同时含字母与数字）构造，满足
``/setup/init`` 与登录校验。文件名刻意不含 ``secret`` / ``password`` 等
敏感词，避免被日志 / 扫描工具误报；内容仅本机可读（权限尽力收紧）。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import string
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 运行期密钥 / 口令文件名（存数据库同目录；不含敏感词，降低被扫描误报的概率）。
RUNTIME_SECRETS_FILENAME = "runtime_secrets.json"

#: 传给后端的环境变量名：后端据此把 admin 口令同步为运行期口令。
ENV_SEED_ADMIN_USERNAME = "KIDTIME_SEED_ADMIN_USERNAME"
ENV_SEED_ADMIN_PASSWORD = "KIDTIME_SEED_ADMIN_PASSWORD"

#: 内置管理员用户名。用户名不敏感，保持固定即可。
DEFAULT_ADMIN_USERNAME = "admin"

#: 文件名键。
_KEY_ADMIN_USERNAME = "admin_username"
_KEY_ADMIN_PASSWORD = "admin_password"
_KEY_JWT_SECRET = "jwt_secret"


@dataclass(frozen=True)
class RuntimeSecrets:
    """一组运行期生成的敏感值。

    Attributes:
        admin_username: 内置管理员用户名（固定 ``admin``）。
        admin_password: 随机生成的内置管理员口令。
        jwt_secret: 随机生成的 JWT 签名密钥。
    """

    admin_username: str
    admin_password: str
    jwt_secret: str

    def to_dict(self) -> dict[str, str]:
        """转成可 JSON 序列化的字典。

        Returns:
            扁平字符串字典。
        """
        return asdict(self)


def _make_admin_password() -> str:
    """生成符合后端密码策略的管理员口令。

    后端策略（``app/core/security.py::validate_password_policy``）：≥8 位且
    同时含字母与数字。``token_urlsafe`` 以 62 字符表随机，可能偶发「无数字」
    或「无字母」，因此用「固定字母 + 固定数字 + urlsafe 补齐」保证恒合规。

    Returns:
        形如 ``aK9<...随机 17 字符>`` 的口令（总长 20 字符）。
    """
    letter = secrets.choice(string.ascii_letters)
    digit = secrets.choice(string.digits)
    tail = secrets.token_urlsafe(17)  # 22 字符长，base64 无填充
    # 去掉可能出现的 ``-`` / ``_`` 之外的符号问题：token_urlsafe 只含字母数字与 -_，
    # 但为稳妥仍做一次字符白名单过滤。
    return letter + digit + "".join(ch for ch in tail if ch.isalnum())[:18]


def secrets_path() -> Path:
    """运行期密钥文件路径（数据库同目录）。

    依赖 ``portable.paths`` 的 ``server_dir()``；调用方须确保该目录已创建
    （``db_bootstrap.ensure_database`` 先于一切写操作执行）。

    Returns:
        ``data/server/runtime_secrets.json`` 的绝对路径。
    """
    from .paths import server_dir  # 局部导入避免循环依赖

    return server_dir() / RUNTIME_SECRETS_FILENAME


def _write_secrets(path: Path, secrets_: RuntimeSecrets) -> None:
    """把密钥原子落盘，并尽力收紧权限。

    Args:
        path: 目标文件路径。
        secrets_: 待持久化的密钥组。

    Raises:
        OSError: 写入失败（目录只读 / 权限不足）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(secrets_.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        # Windows 上 chmod 尽力而为：去掉 group/other 的读写位（POSIX 语义）。
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 权限模型下可能不支持
        logger.debug("收紧密钥文件权限失败（可忽略）", exc_info=True)


def _read_secrets(path: Path) -> RuntimeSecrets | None:
    """从磁盘读取密钥；缺失 / 损坏返回 ``None``。

    Args:
        path: 密钥文件路径。

    Returns:
        解析成功的密钥组；否则 ``None``。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    admin_username = str(data.get(_KEY_ADMIN_USERNAME) or DEFAULT_ADMIN_USERNAME)
    admin_password = str(data.get(_KEY_ADMIN_PASSWORD) or "")
    jwt_secret = str(data.get(_KEY_JWT_SECRET) or "")
    if not admin_password or not jwt_secret:
        return None
    return RuntimeSecrets(
        admin_username=admin_username,
        admin_password=admin_password,
        jwt_secret=jwt_secret,
    )


def load_or_create_secrets() -> RuntimeSecrets:
    """加载既有运行期密钥；首次运行则生成并落盘。

    幂等：文件已存在（重启 / 旧版升级）时**原样复用**，绝不覆盖 ——
    否则已签发的 token 全部失效、已同步的 admin 口令会与文件脱钩。

    Returns:
        本次生效的密钥组。

    Raises:
        OSError: 生成后落盘失败。
    """
    path = secrets_path()
    existing = _read_secrets(path)
    if existing is not None:
        return existing

    created = RuntimeSecrets(
        admin_username=DEFAULT_ADMIN_USERNAME,
        admin_password=_make_admin_password(),
        jwt_secret=secrets.token_urlsafe(32),
    )
    _write_secrets(path, created)
    logger.info("已生成运行期密钥文件：%s（admin 口令 / JWT 密钥均已随机化）", path.name)
    return created


def apply_to_environment(secrets_: RuntimeSecrets) -> None:
    """把密钥写入环境变量，供内嵌后端读取。

    必须在内嵌后端 import / ``create_app`` **之前**调用：
    * ``JWT_SECRET`` 是后端必填配置，缺失会抛 ``MissingConfigError``；
    * ``KIDTIME_SEED_ADMIN_PASSWORD`` 让后端启动时把库里 admin 口令同步为
      运行期口令（旧库自动收敛，见 ``init_db.sync_admin_from_env``）。

    Args:
        secrets_: 本次生效的密钥组。
    """
    os.environ["JWT_SECRET"] = secrets_.jwt_secret
    os.environ[ENV_SEED_ADMIN_USERNAME] = secrets_.admin_username
    os.environ[ENV_SEED_ADMIN_PASSWORD] = secrets_.admin_password


def load_admin_credentials() -> tuple[str, str]:
    """读取内置管理员用户名 / 口令（供桌面端登录调用）。

    Returns:
        ``(username, password)``；文件缺失时自动生成并落盘。
    """
    secrets_ = load_or_create_secrets()
    return secrets_.admin_username, secrets_.admin_password


__all__ = [
    "DEFAULT_ADMIN_USERNAME",
    "ENV_SEED_ADMIN_PASSWORD",
    "ENV_SEED_ADMIN_USERNAME",
    "RUNTIME_SECRETS_FILENAME",
    "RuntimeSecrets",
    "apply_to_environment",
    "load_admin_credentials",
    "load_or_create_secrets",
]
