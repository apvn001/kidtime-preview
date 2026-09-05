"""便携体验版（preview）首启免责提示。

首次启动时展示**免责条款**，仅展示一次（靠 ack 标记文件去重）。
文案与交付包内「使用说明.txt」的「免责条款」一节保持一致，改动请同步。

注：早期版本此处提示过「勿放同步盘」，按用户要求已移除；该风险现只在
「使用说明.txt」中说明。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from .paths import client_dir

logger = logging.getLogger(__name__)

_ACK_FILE = "preview_risk_ack.json"


def maybe_show_risk_notice() -> None:
    """首次启动时展示免责条款，已确认过则跳过。"""
    ack_path = client_dir() / _ACK_FILE
    if ack_path.exists():
        return
    try:
        client_dir().mkdir(parents=True, exist_ok=True)
        QMessageBox.warning(
            None,
            "KidTime 便携版 · 免责条款",
            "本软件仅在本机本地运行，不收集、不上传任何个人或设备使用数据。\n\n"
            "本软件仅供家庭内部的自我管理 / 辅助引导之用，不构成任何专业育儿建议。\n\n"
            "软件按「现状」提供，使用者自担使用风险；因使用或无法使用本软件所产生的"
            "任何后果，作者及贡献者不承担责任。\n\n"
            "请家长与孩子共同约定使用规则，软件只是辅助工具，亲子沟通才是根本。",
        )
        ack_path.write_text(
            json.dumps({"acked": True}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # pragma: no cover - 防御：提示失败不阻断启动
        logger.debug("展示风险提示失败", exc_info=True)
