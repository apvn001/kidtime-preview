"""便携体验版（preview）规则编辑服务层。

封装后端规则相关 REST 调用，供家长面板「规则管控」对话框使用。
设备身份来自首启引导保存的连接文件（``data/client/portable_setup_done.json``）。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from .admin_client import AdminApiClient
from .paths import client_dir

logger = logging.getLogger(__name__)

_CONNECTION_FILE = "portable_setup_done.json"


def _load_connection() -> dict:
    path = client_dir() / _CONNECTION_FILE
    if not path.exists():
        raise FileNotFoundError(f"preview 连接信息缺失：{path}（请先完成首启）")
    return json.loads(path.read_text(encoding="utf-8"))


class RulesEditorService:
    """规则全量读取 / 回写 + 模板只读套用。"""

    def __init__(self) -> None:
        conn = _load_connection()
        self.base_url: str = conn["base_url"]
        self.device_id: str | None = conn.get("device_id")
        # 管理端口令来自运行期密钥文件（S2 整改：不再使用任何固定口令）。
        self._client = AdminApiClient(self.base_url)
        self._client.login()

    # ------------------------------------------------------------------
    def get_rules(self) -> dict:
        """读取本机设备规则全量。"""
        if not self.device_id:
            raise RuntimeError("尚未确定本机设备，无法读取规则")
        return self._client.get_rules(self.device_id)

    def save_rules(self, payload: dict) -> dict:
        """全量回写规则（不可只 PUT 改动字段，与网页一致）。"""
        key = uuid.uuid4().hex
        return self._client.put_rules(self.device_id, payload, key)

    def list_templates(self) -> list[dict]:
        """只读列出内置规则模板。"""
        return self._client.list_templates()

    def apply_template(self, template_id: str) -> dict:
        """套用模板到本机（立即生效，与网页 apply 一致）。"""
        key = uuid.uuid4().hex
        return self._client.apply_template(template_id, self.device_id, key)

    def close(self) -> None:
        self._client.close()
