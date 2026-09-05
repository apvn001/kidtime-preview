"""日志配置与脱敏（D45）。

同时提供 `request_id` 的 contextvar，供中间件、错误处理器与日志格式化共用。
放在本模块可避免 `errors` 与 `middleware` 之间产生循环依赖。
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
"""当前请求的追踪 id。中间件在请求进入时设置，异常处理器读取。"""

_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(x-device-secret\s*[:=]\s*)[^\s,;'\"]+"),
    re.compile(r"(?i)(\"?(?:access_token|refresh_token|device_secret|password|"
               r"new_password|old_password|recovery_code|token|secret)\"?\s*[:=]\s*\"?)"
               r"[^\s,;'\"}\]]+"),
)

_LOG_FORMAT: Final[str] = (
    "%(asctime)s | %(levelname)-8s | %(name)s | rid=%(request_id)s | %(message)s"
)


class RequestIdFilter(logging.Filter):
    """给每条日志补上 `request_id` 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class SecretMaskingFilter(logging.Filter):
    """把日志中的 token / secret / 密码等敏感值打码为 `***`（D45）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 日志本身不应因格式化失败而中断
            return True
        masked = message
        for pattern in _SECRET_PATTERNS:
            masked = pattern.sub(r"\1***", masked)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


_CONFIGURED = False


def setup_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    """配置根日志：控制台 + 滚动文件，并挂载脱敏与 request_id 过滤器。

    Args:
        level: 日志级别名称，如 ``INFO``。
        log_dir: 日志目录；``None`` 或空串时只输出到控制台。
    """
    global _CONFIGURED
    root = logging.getLogger()
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric_level)

    if _CONFIGURED:
        return

    formatter = logging.Formatter(_LOG_FORMAT)
    request_filter = RequestIdFilter()
    secret_filter = SecretMaskingFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(request_filter)
    console.addFilter(secret_filter)
    root.addHandler(console)

    if log_dir:
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                directory / "kidtime-server.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(request_filter)
            file_handler.addFilter(secret_filter)
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - 目录不可写时降级为仅控制台
            root.warning("日志目录不可用，已降级为仅控制台输出：%s", exc)

    _CONFIGURED = True
