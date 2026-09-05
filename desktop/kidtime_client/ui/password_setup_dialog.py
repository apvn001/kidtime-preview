"""KidTime 单机版首启最小向导：设置家长密码。

全新本地库下若没有家长密码，家长模式验证恒失败（死锁）、托盘退出会跳过
验证（安全空洞），因此启动门禁显示之前**强制**先设置家长密码。

本对话框是独立单机形态保留的最小首启向导：仅设置家长密码（复用
``validate_parent_password`` 校验规则），在门禁弹出前无条件执行。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from kidtime_client.security.passwords import validate_parent_password
from kidtime_client.ui.theme import APP_STYLESHEET

logger = logging.getLogger(__name__)


class PasswordSetupDialog(QDialog):
    """preview 首启：设置家长密码（两次输入一致 + 强度校验后写入本地库）。"""

    def __init__(self, parent=None) -> None:  # noqa: ANN001 - Qt 惯例
        super().__init__(parent)
        self.setWindowTitle("首次设置 · 家长密码")
        self.setMinimumWidth(380)
        self.setStyleSheet(APP_STYLESHEET)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._password: str = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(10)

        hint = QLabel(
            "欢迎使用便携体验版。请先设置家长密码：\n"
            "进入家长面板与退出程序都需要它。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        self._edit_first = QLineEdit()
        self._edit_first.setEchoMode(QLineEdit.Password)
        self._edit_first.setPlaceholderText("至少 6 位，不能为纯数字")
        self._edit_confirm = QLineEdit()
        self._edit_confirm.setEchoMode(QLineEdit.Password)
        self._edit_confirm.setPlaceholderText("再输入一次")
        form.addRow("家长密码", self._edit_first)
        form.addRow("确认密码", self._edit_confirm)
        root.addLayout(form)

        self._lbl_error = QLabel("")
        self._lbl_error.setStyleSheet("color: #c62828; font-size: 12px;")
        self._lbl_error.setWordWrap(True)
        self._lbl_error.hide()
        root.addWidget(self._lbl_error)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok_button = QPushButton("保存并继续")
        ok_button.setObjectName("primary")
        ok_button.clicked.connect(self._on_accept)
        buttons.addWidget(ok_button)
        root.addLayout(buttons)

    @property
    def password(self) -> str:
        """校验通过后的明文密码（仅在对话框被 accept 后有效）。"""
        return self._password

    def _on_accept(self) -> None:
        first = self._edit_first.text()
        confirm = self._edit_confirm.text()
        try:
            validate_parent_password(first)
        except Exception as exc:
            self._show_error(f"密码不合要求：{exc}")
            return
        if first != confirm:
            self._show_error("两次输入的密码不一致。")
            return
        self._password = first
        self.accept()

    def _show_error(self, message: str) -> None:
        self._lbl_error.setText(message)
        self._lbl_error.show()
        logger.debug("首启密码设置校验失败：%s", message)


def run_password_setup(parent=None) -> str | None:
    """弹出设置对话框；返回设置的明文密码，用户取消返回 ``None``。"""
    dlg = PasswordSetupDialog(parent)
    if dlg.exec() != QDialog.Accepted or not dlg.password:
        return None
    return dlg.password


__all__ = ["PasswordSetupDialog", "run_password_setup"]
