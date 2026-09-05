"""便携单机版首启引导编排。

流程：确保数据库 → 启动内嵌服务（运行期密钥在此生成/复用，admin 口令由后端
启动时同步）→ 登录内置 admin → 未完成初始化则 init（兜底）→ **自动完成本机
设备配对**（生成配对码 + 兑换，零输入）→ 把 device 凭据经由环境变量 + 连接
文件交给客户端（客户端据此跳过配对向导）。全程无需用户操作。

设备凭据持久化在 ``data/client/portable_setup_done.json``（仅本机、明文，单机
场景可接受）；重启时复用既有凭据，不再重复配对。

🔴 S2 整改：admin 口令来自 ``portable/runtime_secrets``（运行期生成 + 落盘
复用），不再引用任何固定口令常量。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import traceback
import uuid
from pathlib import Path

from .admin_client import AdminApiClient
from .db_bootstrap import ensure_database
from .paths import client_dir, server_db_path, server_logs_dir
from .server_manager import EmbeddedServerManager
from kidtime_client.constants import CLIENT_VERSION

logger = logging.getLogger(__name__)


def _install_excepthook() -> None:
    """未捕获异常落盘到 data/server/logs/kidtime-preview-trace.log，
    便于窗口程序吞掉 stderr 时仍能诊断。"""
    def _hook(etype, value, tb):
        try:
            d = server_logs_dir()
            d.mkdir(parents=True, exist_ok=True)
            p = d / "kidtime-preview-trace.log"
            with p.open("a", encoding="utf-8") as f:
                f.write("\n=== uncaught exception (preview) ===\n")
                f.write("".join(traceback.format_exception(etype, value, tb)))
        except Exception:
            pass
        sys.__excepthook__(etype, value, tb)

    sys.excepthook = _hook

_SETUP_DONE_FILE = "portable_setup_done.json"
_PREVIEW_DEVICE_NAME = "本机预览设备"
_PREVIEW_TIMEZONE = "Asia/Shanghai"
_PREVIEW_OS_INFO = "Windows-preview"


class PortableBootstrapper:
    """首启自动化。"""

    def __init__(self) -> None:
        self.manager = EmbeddedServerManager()
        self.client: AdminApiClient | None = None

    def run(self) -> str:
        """执行引导，返回内嵌服务 base_url。"""
        _install_excepthook()
        try:
            ensure_database()
            self.manager.start(server_db_path(), server_logs_dir())
            atexit.register(self.stop)
            self.client = AdminApiClient(self.manager.base_url)
            try:
                # 🔴 顺序关键：先确保 admin 已初始化（模板不再种 admin，S2 整改），
                # 再登录。后端启动时已把 admin 口令同步为运行期口令（见
                # ``init_db.sync_admin_from_env``），因此登录一定成功；
                # init() 仅在极端兜底（admin 表为空）时被调用一次。
                self._ensure_initialized()
                self.client.login()
                creds = self._load_or_pair()
                self._export_credentials(creds)
            finally:
                self.client.close()
            return self.manager.base_url
        except Exception:
            logger.exception("preview 首启引导失败")
            raise

    # ------------------------------------------------------------------
    def _setup_done_path(self) -> Path:
        return client_dir() / _SETUP_DONE_FILE

    def _load_or_pair(self) -> dict:
        """复用既有凭据；缺失则自配对并持久化。"""
        path = self._setup_done_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("device_id") and data.get("device_secret"):
                    logger.info("复用既有 preview 设备：%s", data["device_id"])
                    return data
            except Exception as exc:  # pragma: no cover - 损坏则重新配对
                logger.warning("连接文件损坏，重新配对：%s", exc)
        return self._self_pair()

    def _self_pair(self) -> dict:
        """生成配对码 → 兑换设备凭据。"""
        assert self.client is not None
        self._ensure_initialized()
        code = self.client.create_pairing_code(note="preview-auto")
        logger.info("preview 自配对，配对码已生成")
        result = self.client.pair_device(
            code=code,
            device_name=_PREVIEW_DEVICE_NAME,
            timezone=_PREVIEW_TIMEZONE,
            client_version=CLIENT_VERSION,
            os_info=_PREVIEW_OS_INFO,
        )
        creds = {
            "base_url": self.manager.base_url,
            "device_id": result["device_id"],
            "device_secret": result["device_secret"],
            "setup_done": True,
        }
        self._save_connection(creds)
        logger.info("preview 自配对完成：%s", creds["device_id"])
        return creds

    def _ensure_initialized(self) -> None:
        """确保后端已完成初始化（存在 admin）；未初始化则调用 init 兜底。"""
        assert self.client is not None
        status = self.client.setup_status()
        if not status.get("initialized"):
            logger.info("首次初始化 preview 服务…")
            result = self.client.init()
            # init 成功即返回 TokenPair；直接采纳，避免无谓再登录一次。
            token = (result or {}).get("access_token") if isinstance(result, dict) else None
            if token:
                self.client.adopt_token(str(token))
        else:
            logger.info("服务已初始化，跳过 init")

    def _save_connection(self, creds: dict) -> None:
        """把连接信息（含 device_secret）写入客户端可读的连接文件。"""
        client_dir().mkdir(parents=True, exist_ok=True)
        path = self._setup_done_path()
        path.write_text(
            json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _export_credentials(self, creds: dict) -> None:
        """把凭据注入环境变量，供 KidTimeApp 经 env_bootstrap_credentials 读取。"""
        os.environ["KIDTIME_BASE_URL"] = creds["base_url"]
        os.environ["KIDTIME_DEVICE_ID"] = creds["device_id"]
        os.environ["KIDTIME_DEVICE_SECRET"] = creds["device_secret"]

    def stop(self) -> None:
        self.manager.stop()
