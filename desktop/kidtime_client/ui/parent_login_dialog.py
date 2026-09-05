"""Parent password prompt (PRD §4.5 / ARCHITECTURE.md §5.20).

Verification is delegated entirely to
:class:`~kidtime_client.ui.parent_mode.ParentModeController`, which owns the
PBKDF2 comparison, the failure counter, the 60s cooldown after 5 wrong tries and
the 15-minute emergency grace after 10 (D20/D43). This dialog only renders the
outcome and counts the cooldown down.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from kidtime_client.ui.parent_mode import EnterResult, ParentModeController
from kidtime_client.ui.theme import APP_STYLESHEET, COLOR_DANGER, COLOR_TEXT_WEAK
from kidtime_client.ui.window_policy import apply_always_on_top

logger = logging.getLogger(__name__)


class ParentLoginDialog(QDialog):
    """Ask for the local parent password (or a recovery code).

    Args:
        controller: The parent-mode controller performing verification.
        parent: Optional Qt parent.
        locked: 1.4（PRD P0-1）是否置顶。锁屏可见（门禁/锁屏）时应为
            ``True``，保证弹窗不被全屏锁屏遮挡；家长模式下（非锁屏）传
            ``False``，弹窗为普通层级，便于家长与其他窗口协作。判据由
            app 层 ``_lock_active()`` 决定，本弹窗不自行判断。
        on_failure: 1.2 门禁日志钩子：每次验证失败时回调
            ``on_failure(method, fail_count)``。``method`` 为 ``"password"``
            或 ``"recovery_code"``（**只判类型，不泄露内容**）；``fail_count``
            为控制器里累计的连续失败次数。传 ``None`` 时不回调（行为与 1.1
            完全一致）。
    """

    def __init__(
        self,
        controller: ParentModeController,
        parent: QWidget | None = None,
        locked: bool = False,
        on_failure=None,
    ) -> None:
        super().__init__(parent)
        # v1.4（PRD P0-1）：不再无条件置顶，改为按锁屏状态决定。
        # 锁屏可见时保持置顶（避免被全屏锁屏遮挡）；家长模式下为普通层级。
        apply_always_on_top(self, locked)
        self._controller = controller
        self._outcome: EnterResult | None = None
        self._on_failure = on_failure

        self.setWindowTitle("家长验证")
        self.setModal(True)
        self.setMinimumWidth(340)
        self.setStyleSheet(APP_STYLESHEET)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        headline = QLabel("请输入家长密码")
        headline.setObjectName("headline")
        layout.addWidget(headline)

        self._input = QLineEdit()
        self._input.setEchoMode(QLineEdit.Password)
        self._input.setPlaceholderText("家长密码")
        self._input.returnPressed.connect(self._on_accept)
        layout.addWidget(self._input)

        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setStyleSheet(f"color: {COLOR_DANGER}; font-size: 12px;")
        layout.addWidget(self._message)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, self
        )
        ok_button = self._buttons.button(QDialogButtonBox.Ok)
        ok_button.setText("进入家长模式")
        ok_button.setObjectName("primary")
        self._buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._cooldown_timer = QTimer(self)
        self._cooldown_timer.setInterval(1000)
        self._cooldown_timer.timeout.connect(self._refresh_cooldown)
        self._cooldown_timer.start()
        self._refresh_cooldown()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    @property
    def outcome(self) -> EnterResult | None:
        """The last verification outcome, or ``None`` if never attempted."""
        return self._outcome

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _on_accept(self) -> None:
        """Verify the entered secret and react to the outcome."""
        secret = self._input.text()
        if not secret:
            self._message.setText("请先输入密码。")
            return

        result = self._controller.try_enter(secret)
        self._outcome = result
        self._input.clear()

        if result is not EnterResult.OK and result is not EnterResult.RECOVERY_USED:
            # 🔴 门禁日志钩子：只报「方式 + 累计失败次数」，绝不把秘密内容
            # 传出去。恢复码 20 位，其余按密码处理。
            if self._on_failure is not None:
                method = "recovery_code" if len(secret) == 20 else "password"
                self._on_failure(method, self._controller.failed_attempts)

        if result is EnterResult.OK:
            self.accept()
            return
        if result is EnterResult.RECOVERY_USED:
            self._message.setText("")
            self.accept()
            return
        if result is EnterResult.COOLDOWN:
            self._message.setText("错误次数过多，请稍候再试。")
            self._refresh_cooldown()
            return
        if result is EnterResult.EMERGENCY_GRACE:
            self._message.setText(
                "连续输错 10 次，已临时放行 15 分钟，请稍后联系家长重设密码。"
            )
            self._refresh_cooldown()
            return

        remaining = max(
            0, ParentModeController.COOLDOWN_THRESHOLD - self._controller.failed_attempts
        )
        if remaining:
            self._message.setText(f"密码不正确，再错 {remaining} 次将暂时锁定。")
        else:
            self._message.setText("密码不正确。")

    def _refresh_cooldown(self) -> None:
        """Enable/disable the OK button according to the cooldown deadline."""
        deadline = self._controller.cooldown_until
        ok_button = self._buttons.button(QDialogButtonBox.Ok)
        if deadline is None:
            self._input.setEnabled(True)
            ok_button.setEnabled(True)
            return
        now = datetime.now(timezone.utc)
        seconds = int((deadline - now).total_seconds())
        if seconds <= 0:
            self._input.setEnabled(True)
            ok_button.setEnabled(True)
            return
        self._input.setEnabled(False)
        ok_button.setEnabled(False)
        self._message.setText(f"已暂时锁定，请等待 {seconds} 秒后再试。")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Stop the cooldown timer when the dialog closes.

        Args:
            event: The Qt close event.
        """
        self._cooldown_timer.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    @staticmethod
    def authenticate(
        controller: ParentModeController,
        parent: QWidget | None = None,
        locked: bool = False,
        on_failure=None,
    ) -> bool:
        """Show the dialog and report whether parent mode was entered.

        Args:
            controller: Parent-mode controller.
            parent: Optional Qt parent.
            locked: 1.4 是否置顶（见 :meth:`__init__`）。
            on_failure: 可选失败回调（见 :meth:`__init__`）。

        Returns:
            ``True`` when the password (or recovery code) was accepted.
        """
        dialog = ParentLoginDialog(controller, parent, locked=locked, on_failure=on_failure)
        accepted = dialog.exec() == QDialog.Accepted
        if accepted:
            logger.info("Parent mode entered (%s)", dialog.outcome)
        return accepted


__all__ = ["ParentLoginDialog"]
