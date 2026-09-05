"""应用配置（ARCHITECTURE.md §11 环境变量清单）。

全部配置项一律来自环境变量或 `.env` 文件（D44：禁止硬编码密钥）。
`JWT_SECRET` 没有默认值，缺失时启动直接失败并给出中文提示。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir(sub: str) -> Path:
    """返回 `%LOCALAPPDATA%\\KidTime\\<sub>` 并确保目录存在。

    🔴 数据库绝不能落在同步盘（ARCHITECTURE.md §0.2）。本函数是后端与桌面端
    共用的路径策略：一律落在本机 LOCALAPPDATA 下。

    Args:
        sub: 子目录名，后端为 ``server``，客户端为 ``client``。

    Returns:
        已创建好的绝对路径。
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / "KidTime" / sub
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings(BaseSettings):
    """后端全部可配置项。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- 数据库 ---
    DATABASE_URL: str = ""

    # --- 安全 ---
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    # D3.1 廉价对冲：默认由 14 天下调至 7 天。纯配置项，不涉及逻辑改动。
    REFRESH_TOKEN_TTL_DAYS: int = 7

    # --- 跨域 ---
    # D1.3 / G16：MVP 下前端与后端同机同源（Caddy 静态托管 frontend/dist），
    # 默认收敛为**空集合** —— 为空时 `create_app()` 直接不注册 CORSMiddleware，
    # 连带消除 `allow_credentials=True` 的风险面。
    # 本地开发（vite dev server 5173 端口）请在 backend/.env 中显式放开。
    CORS_ORIGINS: str = ""

    # --- 公网暴露面收口（P0-6） ---
    # 置 1 才暴露 /docs、/redoc、/openapi.json。生产必须保持 0。
    EXPOSE_API_DOCS: bool = False

    # --- 进程内轻量限流（P1-1 降级版 / D1.2） ---
    # 单 worker 前提下内存计数器天然一致，无需 Redis。格式："次数/窗口秒数"。
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN: str = "10/60"
    RATE_LIMIT_PAIR: str = "10/60"
    RATE_LIMIT_SYNC: str = "60/60"
    RATE_LIMIT_GRANT: str = "10/60"
    # PC 客户端版本更新（MVP 方案 A）：三个客户端端点是**匿名**可达的（不带设备凭证），
    # 因此必须限流。默认 8 小时轮询一次 + 手动「检查更新」，30 次/60 秒余量充足。
    RATE_LIMIT_VERSION_LATEST: str = "30/60"
    RATE_LIMIT_VERSION_CHECK: str = "30/60"
    RATE_LIMIT_UPDATE_ASSET: str = "30/60"
    # 管理端发布走 require_admin，限流仅作兜底。
    RATE_LIMIT_ADMIN_VERSION: str = "10/60"

    # --- 业务参数 ---
    IDEMPOTENCY_TTL_HOURS: int = 24
    IDEMPOTENCY_IN_PROGRESS_TIMEOUT_MINUTES: int = 5
    PAIRING_CODE_TTL_MINUTES: int = 15
    PAIRING_CODE_MAX_ACTIVE: int = 5
    COMMAND_EXPIRE_MINUTES: int = 30

    # --- 日志 ---
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = ""

    # --- 安全阀 ---
    ALLOW_UNSAFE_DB: bool = False

    # --- 服务监听 ---
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    @field_validator("ALLOW_UNSAFE_DB", "EXPOSE_API_DOCS", "RATE_LIMIT_ENABLED", mode="before")
    @classmethod
    def _parse_bool(cls, value: object) -> object:
        """允许 ``1`` / ``true`` / ``yes`` / ``on`` 等写法。"""
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return value

    @model_validator(mode="after")
    def _fill_defaults(self) -> "Settings":
        """为 `DATABASE_URL` / `LOG_DIR` 填充基于 LOCALAPPDATA 的安全默认值。"""
        if not self.DATABASE_URL:
            db_path = default_data_dir("server") / "kidtime.db"
            object.__setattr__(self, "DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
        if not self.LOG_DIR:
            object.__setattr__(self, "LOG_DIR", str(default_data_dir("server") / "logs"))
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 `CORS_ORIGINS` 拆成列表。为空表示不启用 CORS。"""
        return [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]

    @property
    def rate_limit_rules(self) -> dict[str, str]:
        """返回「路径 → 限流表达式」映射。

        ⚠️ `RateLimitMiddleware` 按**路径精确匹配**（已 rstrip 末尾 `/`），
        因此这里的 key 必须与 router 实际注册的完整路径逐字一致。
        """
        return {
            "/api/v1/auth/login": self.RATE_LIMIT_LOGIN,
            "/api/v1/client/pair": self.RATE_LIMIT_PAIR,
            "/api/v1/client/sync": self.RATE_LIMIT_SYNC,
            "/api/v1/grants": self.RATE_LIMIT_GRANT,
            # PC 客户端版本更新（MVP 方案 A）：匿名可达路径，必须限流。
            "/api/v1/client/version/latest": self.RATE_LIMIT_VERSION_LATEST,
            "/api/v1/client/version/check": self.RATE_LIMIT_VERSION_CHECK,
            "/api/v1/client/update/asset": self.RATE_LIMIT_UPDATE_ASSET,
            "/api/v1/admin/client/version": self.RATE_LIMIT_ADMIN_VERSION,
        }


class MissingConfigError(RuntimeError):
    """必填配置缺失。"""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取（并缓存）配置。

    Raises:
        MissingConfigError: 缺少 `JWT_SECRET` 等必填项时抛出，附中文修复提示。
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = [
            ".".join(str(part) for part in err.get("loc", ()))
            for err in exc.errors()
            if err.get("type") == "missing"
        ]
        if missing:
            raise MissingConfigError(
                "启动失败：缺少必填环境变量 "
                + "、".join(missing)
                + "。请在 backend/.env 中设置（可参考 backend/.env.example），"
                "或通过系统环境变量提供。禁止在代码中硬编码密钥（D44）。"
            ) from exc
        raise
