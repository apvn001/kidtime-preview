"""客户端路径与运行时配置（单机便携形态）。

🔴 数据目录策略：固定取便携解压目录下 ``data/client``，可用环境变量
``KIDTIME_CLIENT_DATA_DIR`` 覆盖（测试用）。**绝不读写**系统上其它安装的
``%LOCALAPPDATA%\\KidTime\\client``（避免记录/设置/家长密码串库），也**不允许**
落在百度网盘 / OneDrive / Dropbox / 坚果云 / Google Drive 等同步目录或 UNC
路径（由上层 ``LocalDatabase`` 的 unsafe 判定兜底）。

单机形态：内嵌 FastAPI 服务固定监听 ``http://127.0.0.1`` 明文，无 HTTPS
证书 / CA bundle / TLS 校验概念，相关配置链已整体移除。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from kidtime_client.constants import CLIENT_VERSION, DEFAULT_TIMEZONE

ENV_DATA_DIR: Final[str] = "KIDTIME_CLIENT_DATA_DIR"
ENV_BASE_URL: Final[str] = "KIDTIME_BASE_URL"
ENV_DEVICE_ID: Final[str] = "KIDTIME_DEVICE_ID"
ENV_DEVICE_SECRET: Final[str] = "KIDTIME_DEVICE_SECRET"
ENV_ALLOW_UNSAFE_DB: Final[str] = "ALLOW_UNSAFE_DB"
ENV_LOG_LEVEL: Final[str] = "KIDTIME_LOG_LEVEL"

DB_FILENAME: Final[str] = "kidtime_client.db"
CREDENTIALS_FILENAME: Final[str] = "credentials.bin"
LOG_FILENAME: Final[str] = "client.log"

DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8000"


def _env_flag(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量，接受 ``1/true/yes/on``。

    Args:
        name: 环境变量名。
        default: 变量不存在时的返回值。

    Returns:
        解析后的布尔值。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def default_data_dir() -> Path:
    """返回客户端数据目录并确保存在。

    Returns:
        ``KIDTIME_CLIENT_DATA_DIR`` 指定目录，否则取解压目录 ``data/client``
        （便携单机形态固定本地数据目录，🔴 不会去读系统上其它安装的
        ``%LOCALAPPDATA%\\KidTime\\client``，避免记录/设置/家长密码串库）。
    """
    override = os.environ.get(ENV_DATA_DIR, "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        from kidtime_client.portable.paths import data_dir as portable_data_dir

        path = portable_data_dir() / "client"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class ClientConfig:
    """客户端运行时配置快照。

    Attributes:
        data_dir: 数据根目录。
        db_path: 本地 SQLite 文件路径。
        credentials_path: DPAPI 加密的设备凭据文件路径。
        log_dir: 日志目录。
        base_url: 后端基址（单机形态恒为内嵌服务的 ``http://127.0.0.1``）。
        client_version: 客户端版本号。
        timezone: 本机 IANA 时区名。
        allow_unsafe_db: 是否放行同步盘路径（仅测试）。
        log_level: 日志级别。
    """

    data_dir: Path = field(default_factory=default_data_dir)
    db_path: Path = field(default_factory=lambda: default_data_dir() / DB_FILENAME)
    credentials_path: Path = field(
        default_factory=lambda: default_data_dir() / CREDENTIALS_FILENAME
    )
    log_dir: Path = field(default_factory=lambda: default_data_dir() / "logs")
    base_url: str = DEFAULT_BASE_URL
    client_version: str = CLIENT_VERSION
    timezone: str = DEFAULT_TIMEZONE
    allow_unsafe_db: bool = False
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "ClientConfig":
        """从环境变量构造配置。

        Returns:
            冻结的配置对象。
        """
        data_dir = default_data_dir()
        return cls(
            data_dir=data_dir,
            db_path=data_dir / DB_FILENAME,
            credentials_path=data_dir / CREDENTIALS_FILENAME,
            log_dir=data_dir / "logs",
            base_url=os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL,
            client_version=CLIENT_VERSION,
            timezone=local_timezone_name(),
            allow_unsafe_db=_env_flag(ENV_ALLOW_UNSAFE_DB, False),
            log_level=os.environ.get(ENV_LOG_LEVEL, "INFO").strip().upper() or "INFO",
        )


def local_timezone_name() -> str:
    """尽力探测本机 IANA 时区名。

    Windows 上 ``tzlocal`` 不一定可用，退化为默认的 ``Asia/Shanghai``。

    Returns:
        IANA 时区字符串。
    """
    env_tz = os.environ.get("TZ", "").strip()
    if env_tz:
        return env_tz
    try:  # pragma: no cover - 依赖运行环境
        import tzlocal  # type: ignore[import-not-found]

        name = str(tzlocal.get_localzone_name() or "").strip()
        if name:
            return name
    except Exception:  # noqa: BLE001 - 探测失败一律退化
        pass
    return DEFAULT_TIMEZONE


def env_bootstrap_credentials() -> tuple[str, str, str] | None:
    """读取环境变量里的免配对凭据（便携引导在启动前注入）。

    Returns:
        ``(base_url, device_id, device_secret)``；任一缺失返回 ``None``。
    """
    base_url = os.environ.get(ENV_BASE_URL, "").strip()
    device_id = os.environ.get(ENV_DEVICE_ID, "").strip()
    secret = os.environ.get(ENV_DEVICE_SECRET, "").strip()
    if base_url and device_id and secret:
        return base_url, device_id, secret
    return None
