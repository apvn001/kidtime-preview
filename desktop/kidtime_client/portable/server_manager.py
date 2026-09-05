"""便携单机版内嵌后端服务管理器。

在普通守护线程内运行 uvicorn，监听 127.0.0.1；import ``backend.app.main``
之前注入 DATABASE_URL / JWT_SECRET / LOG_DIR / ALLOW_UNSAFE_DB，把数据导向
本机 ``data/`` 目录。退出时置 ``should_exit=True`` 干净收尾，无孤儿进程。

🔴 S1/S2 安全整改：JWT 密钥与内置管理员口令不再写死在源码里，而在 ``start()``
里由 ``portable/runtime_secrets`` **运行期生成并落盘复用**，再经环境变量
注入后端（``JWT_SECRET`` 供签名、``KIDTIME_SEED_ADMIN_PASSWORD`` 供后端把
admin 口令同步为运行期口令，见 ``backend/app/db/init_db.sync_admin_from_env``）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .constants import PREVIEW_SERVER_HOST, preview_server_port
from .runtime_secrets import apply_to_environment, load_or_create_secrets

logger = logging.getLogger(__name__)

# 🔴 内嵌服务恒为 127.0.0.1：全局禁用代理，避免本机请求被 HTTP(S)_PROXY 拦截
# （实测构建机设了代理，localhost 走代理会返回 502 / 连接失败，自配对随之崩溃）。
import urllib.request as _urllib_request

_urllib_request.install_opener(
    _urllib_request.build_opener(_urllib_request.ProxyHandler({}))
)

_HEALTH_URL = "http://{host}:{port}/health"


class EmbeddedServerManager:
    """在后台线程内托管内嵌 FastAPI 服务。"""

    def __init__(self) -> None:
        self.host = PREVIEW_SERVER_HOST
        self.port = preview_server_port()
        self._server: object | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        # 🔴 真机第十一轮复查：stop()→_checkpoint() 读 self._db_path 做 WAL 检查点，
        # 但 start() 此前从未给它赋值 → 每次 preview 退出都 AttributeError、检查点
        # 从不执行（-wal/-shm 残留）。这里默认 None，start() 时落库路径。
        self._db_path: Path | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, db_path, log_dir) -> None:
        """注入环境并启动服务，阻塞到 /health 返回 200 或超时。"""
        self._db_path = Path(db_path)  # 供 stop() 的 WAL 检查点使用
        # 🔴 S1/S2：运行期生成并落盘 JWT 密钥与 admin 口令（重启复用、旧库自动收敛），
        # 必须在 import 后端之前注入环境变量。
        secrets_ = load_or_create_secrets()
        apply_to_environment(secrets_)
        os.environ["DATABASE_URL"] = f"sqlite:///{Path(db_path).as_posix()}"
        os.environ["LOG_DIR"] = str(log_dir)
        # 🔴 Q2 强放：单机版常解压在同步盘，必须显式放行路径自检。
        os.environ["ALLOW_UNSAFE_DB"] = "1"
        os.environ["EXPOSE_API_DOCS"] = "0"

        try:
            from app.main import create_app
            import uvicorn

            app = create_app()
            config = uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_config=None,
                access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._thread = threading.Thread(target=self._run_server, daemon=True)
            self._thread.start()
            self._wait_ready(timeout=10)
            logger.info("内嵌后端已启动：%s:%s", self.host, self.port)
        except Exception:
            logger.exception("内嵌后端启动失败（preview 自包含诊断）")
            raise

    def _run_server(self) -> None:
        """在守护线程内运行 uvicorn；异常落盘，避免被窗口程序吞掉。"""
        try:
            assert self._server is not None
            self._server.run()
        except Exception:
            logger.exception("内嵌后端运行异常（uvicorn 线程）")

    def _wait_ready(self, timeout: float = 10) -> bool:
        url = _HEALTH_URL.format(host=self.host, port=self.port)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        self._ready.set()
                        return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        logger.warning("内嵌后端健康检查超时（%ss）", timeout)
        return False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        """置 should_exit 让 uvicorn 退出，join 守护线程并做 WAL 检查点。"""
        if self._server is None:
            return
        self._server.should_exit = True  # type: ignore[attr-defined]
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._checkpoint()
        logger.info("内嵌后端已停止")

    def _checkpoint(self) -> None:
        """退出前对 WAL 做 TRUNCATE 检查点，避免残留 -wal/-shm。"""
        if not self._db_path or not os.path.exists(self._db_path):
            return
        try:
            conn = sqlite3.connect(self._db_path, timeout=3)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except Exception:  # pragma: no cover - 防御
            logger.debug("WAL 检查点失败", exc_info=True)
