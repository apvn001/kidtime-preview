"""Engine / Session 工厂、SQLite PRAGMA、同步盘路径自检与忙重试（D51 / S1）。

Engine 采用**惰性创建 + URL 变化自动重建**的方式，使测试可以通过设置
``DATABASE_URL`` 环境变量并调用 :func:`reset_engine` 切换到临时文件数据库，
而中间件里直接使用的 ``SessionLocal()`` 也能跟着切换（幂等中间件跨 Session
可见性是必须被真实测到的，见 ARCHITECTURE.md §9.6）。
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Final, Iterator, TypeVar
from urllib.parse import unquote, urlparse

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError, UnsafeDatabasePathError

logger = logging.getLogger(__name__)

UNSAFE_PATH_MARKERS: Final[tuple[str, ...]] = (
    "baidusyncdisk",
    "onedrive",
    "dropbox",
    "nutstore",
    "坚果云",
    "googledrive",
    "google drive",
)

BUSY_BACKOFF_MS: Final[tuple[int, ...]] = (20, 50, 100)
"""D51：最多重试 3 次，退避 20/50/100 毫秒。"""

_engine: Engine | None = None
_engine_url: str | None = None
_session_factory: sessionmaker[Session] | None = None


def sqlite_url_to_path(url: str) -> Path | None:
    """把 ``sqlite:///...`` URL 解析成本地绝对路径。

    Args:
        url: SQLAlchemy 数据库 URL。

    Returns:
        本地文件路径；非 sqlite 文件库（如 ``:memory:``）返回 ``None``。
    """
    if not url.startswith("sqlite"):
        return None
    parsed = urlparse(url)
    raw = unquote(parsed.path or "")
    if parsed.netloc:
        # sqlite://///server/share/x.db 这类 UNC 形式
        raw = f"//{parsed.netloc}{raw}"
    raw = raw.lstrip("/") if len(raw) > 2 and raw[2] == ":" else raw
    if not raw or raw.endswith(":memory:") or raw == "":
        return None
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, ValueError):  # pragma: no cover - 极端非法路径
        return Path(raw)


def assert_db_path_safe(url: str, allow_unsafe: bool | None = None) -> None:
    """🔴 校验数据库路径未落在云同步目录 / 网络路径上（S1）。

    Args:
        url: 数据库 URL。
        allow_unsafe: 显式放行开关；``None`` 时读取 `Settings.ALLOW_UNSAFE_DB`。

    Raises:
        UnsafeDatabasePathError: 路径命中同步盘或 UNC 且未显式放行。
    """
    if allow_unsafe is None:
        allow_unsafe = get_settings().ALLOW_UNSAFE_DB

    path = sqlite_url_to_path(url)
    if path is None:
        return

    text = str(path).replace("\\", "/").lower()
    hit = next((marker for marker in UNSAFE_PATH_MARKERS if marker in text), None)
    is_unc = str(path).startswith("\\\\") or str(path).startswith("//")
    if hit is None and not is_unc:
        return

    reason = f"命中云同步目录标记 “{hit}”" if hit else "是 UNC 网络路径"
    hint = (
        "\n"
        "=========================== 启动被拒绝 ===========================\n"
        f"🔴 数据库路径不安全：{path}\n"
        f"   原因：该路径{reason}。\n"
        "   云同步客户端会并发读写 .db / .db-wal / .db-shm，与 SQLite 文件锁\n"
        "   语义冲突，典型后果是 database disk image is malformed（不可逆损坏）。\n"
        "\n"
        "   修复方式（任选其一）：\n"
        "   1) 删除 DATABASE_URL 配置，改用默认路径 %LOCALAPPDATA%\\KidTime\\server\\kidtime.db\n"
        "   2) 指向本机非同步目录，例如 DATABASE_URL=sqlite:///D:/kidtime-data/kidtime.db\n"
        "   3) 仅在测试环境下设置 ALLOW_UNSAFE_DB=1 跳过本检查\n"
        "==================================================================\n"
    )
    if allow_unsafe:
        logger.warning("已通过 ALLOW_UNSAFE_DB=1 放行不安全的数据库路径：%s", path)
        return
    logger.critical(hint)
    raise UnsafeDatabasePathError(hint)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_conn: Any, _rec: Any) -> None:
    """每次建立 SQLite 连接时设置四条 PRAGMA（D51）。"""
    if not isinstance(dbapi_conn, sqlite3.Connection):
        return
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _build_engine(url: str) -> Engine:
    """按 URL 创建 Engine（SQLite 专用连接参数）。"""
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 5.0}
        path = sqlite_url_to_path(url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, future=True)


def get_engine() -> Engine:
    """返回全局 Engine；`DATABASE_URL` 变化时自动重建。"""
    global _engine, _engine_url, _session_factory
    url = get_settings().DATABASE_URL
    if _engine is None or _engine_url != url:
        if _engine is not None:
            _engine.dispose()
        _engine = _build_engine(url)
        _engine_url = url
        _session_factory = sessionmaker(
            bind=_engine, autocommit=False, autoflush=False, expire_on_commit=False
        )
    return _engine


def reset_engine() -> None:
    """释放并清空全局 Engine（测试隔离用）。"""
    global _engine, _engine_url, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None
    _session_factory = None


def get_session_factory() -> sessionmaker[Session]:
    """返回与当前 Engine 绑定的 Session 工厂。"""
    get_engine()
    assert _session_factory is not None  # get_engine 保证已创建
    return _session_factory


def SessionLocal() -> Session:  # noqa: N802 - 保持与文档一致的命名
    """创建一个新的数据库 Session。"""
    return get_session_factory()()


def session_scope() -> Iterator[Session]:
    """`yield` 一个 Session，正常结束提交、异常回滚（供脚本使用）。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


F = TypeVar("F", bound=Callable[..., Any])


def _is_busy_error(exc: OperationalError) -> bool:
    """判断是否为 SQLite 忙/锁错误。"""
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text or "busy" in text


def retry_on_busy(fn: F) -> F:
    """装饰写 service：遇到 `SQLITE_BUSY` 按 20/50/100ms 退避重试 3 次（D51）。

    重试前会对参数中的 `Session` 执行 `rollback()`，保证事务状态干净。
    重试耗尽抛出 503 `DB_BUSY`。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        attempts = len(BUSY_BACKOFF_MS)
        for attempt in range(attempts + 1):
            try:
                return fn(*args, **kwargs)
            except OperationalError as exc:
                if not _is_busy_error(exc):
                    raise
                session = next((a for a in args if isinstance(a, Session)), None)
                if session is None:
                    candidate = kwargs.get("db")
                    session = candidate if isinstance(candidate, Session) else None
                if session is not None:
                    session.rollback()
                if attempt >= attempts:
                    logger.error("SQLite 持续繁忙，重试 %d 次后仍失败：%s", attempts, exc)
                    raise ServiceUnavailableError() from exc
                time.sleep(BUSY_BACKOFF_MS[attempt] / 1000.0)
        raise ServiceUnavailableError()  # pragma: no cover - 逻辑上不可达

    return wrapper  # type: ignore[return-value]
