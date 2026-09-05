"""客户端日志初始化：轮转文件 + 控制台 + 敏感信息脱敏。

🔴 明文 `device_secret` / 家长密码 / 恢复码**绝不允许**进入日志（D44）。
:class:`SecretRedactingFilter` 会在写出前把已注册的敏感串替换成 ``***``。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Final

LOG_FORMAT: Final[str] = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
MAX_BYTES: Final[int] = 2 * 1024 * 1024
BACKUP_COUNT: Final[int] = 5
REDACTED: Final[str] = "***"

_SECRETS: set[str] = set()
_CONFIGURED: bool = False


def register_secret(value: str | None) -> None:
    """登记一个需要在日志中脱敏的字符串。

    Args:
        value: 敏感明文；``None``、空串或过短（<8）时忽略。
    """
    if not value or len(value) < 8:
        return
    _SECRETS.add(value)


def clear_secrets() -> None:
    """清空已登记的敏感串（测试用）。"""
    _SECRETS.clear()


class SecretRedactingFilter(logging.Filter):
    """在日志记录写出前替换敏感明文。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """执行脱敏。

        Args:
            record: 日志记录。

        Returns:
            恒为 ``True``（不丢弃任何记录，只改写内容）。
        """
        if not _SECRETS:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败不应影响日志链路
            return True
        redacted = message
        for secret in _SECRETS:
            if secret in redacted:
                redacted = redacted.replace(secret, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> Path:
    """配置根 logger：轮转文件 + 可选控制台。

    重复调用是幂等的（只配置一次）。

    Args:
        log_dir: 日志目录，不存在会自动创建。
        level: 日志级别名。
        console: 是否附加 stderr 处理器（打包 noconsole 时无副作用）。

    Returns:
        日志文件路径。
    """
    global _CONFIGURED
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "client.log"
    if _CONFIGURED:
        return log_path

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    redactor = SecretRedactingFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(redactor)
        root.addHandler(stream_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True
    return log_path


def reset_logging() -> None:
    """移除全部处理器并复位配置标记（测试用）。"""
    global _CONFIGURED
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _CONFIGURED = False
