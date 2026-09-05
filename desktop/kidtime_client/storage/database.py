"""本地 SQLite 连接管理、PRAGMA、路径自检与事务（ARCHITECTURE.md §5.11 / §9.4）。

🔴 三条硬约束在本文件落地：

* **S1 路径自检**：:meth:`LocalDatabase.assert_db_path_safe` 命中同步盘或 UNC 直接拒绝启动。
* **Single-Writer**：连接对象放在 ``threading.local()``，写事务只应由 GUI 主线程发起；
  非主线程调用 :meth:`LocalDatabase.transaction` 会记录 ERROR 级告警（R1/R2）。
* **BEGIN IMMEDIATE**：所有写事务显式取写锁，遇 SQLITE_BUSY 按 20/50/100ms 退避重试。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Iterator

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
"""命中即拒绝启动的云同步目录标记（S1）。"""

BUSY_BACKOFF_MS: Final[tuple[int, ...]] = (20, 50, 100)
"""SQLITE_BUSY 退避序列，最多重试 3 次。"""

PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

SCHEMA_FILE: Final[Path] = Path(__file__).with_name("schema.sql")


class UnsafeDatabasePathError(RuntimeError):
    """数据库路径落在同步盘 / 网络路径上。"""


class DatabaseBusyError(RuntimeError):
    """重试耗尽后仍无法取得写锁。"""


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """判断异常是否为 SQLite 忙/锁。"""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class LocalDatabase:
    """客户端本地库句柄。

    Attributes:
        path: 数据库文件绝对路径。
        allow_unsafe: 是否放行不安全路径（仅测试）。
    """

    def __init__(self, path: Path, allow_unsafe: bool = False) -> None:
        """初始化（不立即建立连接）。

        Args:
            path: SQLite 文件路径。
            allow_unsafe: ``True`` 时跳过同步盘拒绝（对应 ``ALLOW_UNSAFE_DB=1``）。
        """
        self.path: Path = Path(path)
        self.allow_unsafe: bool = allow_unsafe
        self._local: threading.local = threading.local()
        self._main_thread_id: int = threading.get_ident()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------ 路径自检
    @staticmethod
    def assert_db_path_safe(path: Path, allow_unsafe: bool = False) -> None:
        """🔴 校验数据库路径不在云同步目录 / UNC 上（S1）。

        Args:
            path: 目标数据库路径。
            allow_unsafe: 显式放行开关。

        Raises:
            UnsafeDatabasePathError: 命中同步盘标记或 UNC 且未放行。
        """
        raw = str(path)
        normalized = raw.replace("\\", "/").lower()
        hit = next((marker for marker in UNSAFE_PATH_MARKERS if marker in normalized), None)
        is_unc = raw.startswith("\\\\") or raw.startswith("//")
        if hit is None and not is_unc:
            return

        reason = f"命中云同步目录标记 “{hit}”" if hit else "是 UNC 网络路径"
        hint = (
            "\n"
            "=========================== 启动被拒绝 ===========================\n"
            f"🔴 客户端数据库路径不安全：{raw}\n"
            f"   原因：该路径{reason}。\n"
            "   云同步客户端会并发读写 .db / .db-wal / .db-shm，与 SQLite 文件锁\n"
            "   语义冲突，典型后果是 database disk image is malformed（不可逆损坏，\n"
            "   孩子的全部计时数据会丢失）。\n"
            "\n"
            "   修复方式（任选其一）：\n"
            "   1) 删除 KIDTIME_CLIENT_DATA_DIR 环境变量，改用默认路径\n"
            "      %LOCALAPPDATA%\\KidTime\\client\\kidtime_client.db\n"
            "   2) 指向本机非同步目录，例如\n"
            "      set KIDTIME_CLIENT_DATA_DIR=D:\\kidtime-data\n"
            "   3) 仅在测试环境下设置 ALLOW_UNSAFE_DB=1 跳过本检查\n"
            "==================================================================\n"
        )
        if allow_unsafe:
            logger.warning("已通过 ALLOW_UNSAFE_DB=1 放行不安全的数据库路径：%s", raw)
            return
        logger.critical(hint)
        raise UnsafeDatabasePathError(hint)

    # ------------------------------------------------------------ 连接
    def connect(self) -> sqlite3.Connection:
        """返回当前线程的连接（不存在则创建并设置 PRAGMA）。

        Returns:
            线程私有的 `sqlite3.Connection`，``row_factory`` 为 `sqlite3.Row`。
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            for pragma in PRAGMAS:
                cursor.execute(pragma)
        finally:
            cursor.close()
        self._local.conn = conn
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    def initialize(self) -> None:
        """路径自检 → 建目录 → 建表 → 迁移到最新 schema_version。

        Raises:
            UnsafeDatabasePathError: 路径不安全。
        """
        self.assert_db_path_safe(self.path, self.allow_unsafe)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        from kidtime_client.storage.migrations import migrate  # 局部导入避免循环

        migrate(conn)
        logger.info("客户端本地库已就绪：%s", self.path)

    def close(self) -> None:
        """关闭当前线程连接。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需处理
            logger.debug("关闭 SQLite 连接时出现异常", exc_info=True)
        with self._connections_lock:
            if conn in self._connections:
                self._connections.remove(conn)
        self._local.conn = None

    def close_all(self) -> None:
        """关闭进程内已建立的全部连接（退出与测试清理用）。"""
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                logger.debug("关闭 SQLite 连接时出现异常", exc_info=True)
        self._local.conn = None

    # ------------------------------------------------------------ 查询辅助
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        """执行一条 SQL 并返回游标（自动提交模式）。

        Args:
            sql: SQL 语句。
            params: 绑定参数。

        Returns:
            游标对象。
        """
        return self.connect().execute(sql, params)

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        """查询单行。"""
        cursor = self.execute(sql, params)
        try:
            return cursor.fetchone()
        finally:
            cursor.close()

    def query_all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        """查询多行。"""
        cursor = self.execute(sql, params)
        try:
            return list(cursor.fetchall())
        finally:
            cursor.close()

    # ------------------------------------------------------------ 事务
    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """写事务上下文：``BEGIN IMMEDIATE`` + 忙重试 + 提交/回滚。

        Args:
            immediate: ``True`` 用 ``BEGIN IMMEDIATE`` 立刻取写锁（默认，写路径必须）；
                ``False`` 用 ``BEGIN``（只读一致性快照）。

        Yields:
            事务内的连接。

        Raises:
            DatabaseBusyError: 退避重试耗尽仍拿不到写锁。
        """
        if threading.get_ident() != self._main_thread_id:
            logger.error(
                "🔴 违反 Single-Writer 约束：线程 %s 尝试开启数据库写事务，"
                "所有写操作必须回到 GUI 主线程（ARCHITECTURE.md §9.4 R1）",
                threading.current_thread().name,
            )

        conn = self.connect()
        statement = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        attempts = len(BUSY_BACKOFF_MS)
        for attempt in range(attempts + 1):
            try:
                conn.execute(statement)
                break
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt >= attempts:
                    if _is_busy(exc):
                        logger.error("SQLite 持续繁忙，重试 %d 次后放弃：%s", attempts, exc)
                        raise DatabaseBusyError(str(exc)) from exc
                    raise
                time.sleep(BUSY_BACKOFF_MS[attempt] / 1000.0)

        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚失败无可挽回
                logger.exception("事务回滚失败")
            raise
        else:
            conn.execute("COMMIT")

    def checkpoint(self) -> None:
        """执行 WAL checkpoint（退出前收敛 -wal 文件）。"""
        try:
            self.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:  # pragma: no cover - 非关键路径
            logger.debug("WAL checkpoint 失败", exc_info=True)
