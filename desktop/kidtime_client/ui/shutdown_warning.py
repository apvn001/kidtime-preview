"""关机预告窗口（1.4.3 增量 · 需求 2b）。

锁屏（启动门禁 / 日常锁屏）**单次连续**停留满 15 分钟后，进入 30 秒关机预告。
本窗口负责把预告呈现给孩子：置顶抢焦点的倒计时 + 「取消关机」按钮。

职责边界
--------
窗口只做**展示**，不做任何判断：

* 倒计时的推进由
  :class:`~kidtime_client.core.lock_auto_shutdown.AutoShutdownController` 的
  心跳驱动——控制器每秒调用一次 :meth:`update_remaining`，窗口纯被动刷新；
* 「取消关机」按钮只发 :attr:`cancelRequested` 信号，由 ``app.py`` 转发给
  控制器（``cancel_grace()``，内部会重新累计并触发 ``on_grace_cancelled``
  回调把本窗口收回去）；
* 预告归零后的关机动作同样由控制器回调驱动（``on_shutdown``），窗口不直接
  调用系统电源。

这样整条「累计 → 预告 → 取消/关机」链路都在控制器里，可被单元测试零真实
等待地演练，窗口只是它的显示器。
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kidtime_client.ui.theme import (
    APP_STYLESHEET,
    COLOR_BORDER,
    COLOR_DANGER,
    COLOR_PRIMARY,
    COLOR_TEXT,
    COLOR_TEXT_WEAK,
    FONT_FAMILY,
)
from kidtime_client.ui.window_policy import center_within_available_geometry

logger = logging.getLogger(__name__)

#: 预告窗口固定宽度（逻辑像素）。
WARNING_WIDTH: Final[int] = 480


_WARNING_QSS: Final[str] = f"""
QFrame#shutdownWarningCard {{
    background: #FFFFFF;
    border: 1px solid {COLOR_BORDER};
    border-radius: 14px;
}}
QLabel#shutdownWarningTitle {{
    font-family: {FONT_FAMILY};
    font-size: 20px;
    font-weight: 700;
    color: {COLOR_TEXT};
    background: transparent;
}}
QLabel#shutdownWarningBody {{
    font-family: {FONT_FAMILY};
    font-size: 14px;
    color: {COLOR_TEXT_WEAK};
    background: transparent;
}}
QLabel#shutdownWarningCountdown {{
    font-family: {FONT_FAMILY};
    font-size: 34px;
    font-weight: 300;
    color: {COLOR_DANGER};
    background: transparent;
}}
QLabel#shutdownWarningHint {{
    font-family: {FONT_FAMILY};
    font-size: 12px;
    color: {COLOR_TEXT_WEAK};
    background: transparent;
}}
QPushButton#shutdownWarningCancel {{
    background: transparent;
    color: {COLOR_PRIMARY};
    border: 1px solid {COLOR_PRIMARY};
    border-radius: 8px;
    padding: 8px 28px;
    font-family: {FONT_FAMILY};
    font-size: 14px;
    font-weight: 600;
}}
QPushButton#shutdownWarningCancel:hover {{
    background: #EAF1FF;
}}
"""


class ShutdownWarningWindow(QWidget):
    """30 秒关机预告的置顶展示窗口（单实例复用）。

    Attributes:
        cancelRequested: 「取消关机」被点击（由 app 转发给控制器）。
    """

    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setStyleSheet(APP_STYLESHEET + _WARNING_QSS)
        self.setFixedWidth(WARNING_WIDTH)

        self._card = QFrame(self)
        self._card.setObjectName("shutdownWarningCard")

        self._title = QLabel("即将自动关机", self._card)
        self._title.setObjectName("shutdownWarningTitle")
        self._title.setAlignment(Qt.AlignCenter)

        self._body = QLabel("", self._card)
        self._body.setObjectName("shutdownWarningBody")
        self._body.setAlignment(Qt.AlignCenter)
        self._body.setWordWrap(True)

        self._countdown = QLabel("", self._card)
        self._countdown.setObjectName("shutdownWarningCountdown")
        self._countdown.setAlignment(Qt.AlignCenter)

        self._hint = QLabel("如仍在使用这台电脑，请点击「取消关机」", self._card)
        self._hint.setObjectName("shutdownWarningHint")
        self._hint.setAlignment(Qt.AlignCenter)

        self._cancel_button = QPushButton("取消关机", self._card)
        self._cancel_button.setObjectName("shutdownWarningCancel")
        self._cancel_button.setCursor(Qt.PointingHandCursor)
        self._cancel_button.clicked.connect(self.cancelRequested.emit)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(10)
        card_layout.addWidget(self._title)
        card_layout.addSpacing(6)
        card_layout.addWidget(self._body)
        card_layout.addSpacing(8)
        card_layout.addWidget(self._countdown)
        card_layout.addSpacing(2)
        card_layout.addWidget(self._hint)
        card_layout.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._cancel_button)
        row.addStretch(1)
        card_layout.addLayout(row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card)

    # ------------------------------------------------------------------
    # 对外 API（由 app 层经控制器回调驱动）
    # ------------------------------------------------------------------
    def show_warning(self, total_seconds: int, body: str = "") -> None:
        """展示预告窗口并初始化倒计时。

        Args:
            total_seconds: 预告总时长（秒），如 30。
            body: 正文；为空时用默认文案。
        """
        self._body.setText(
            body
            or "锁屏已停留一段时间，电脑将在倒计时结束后自动关闭，请提前保存。"
        )
        self.update_remaining(total_seconds)
        self._center_on_primary()
        self.show()
        self.raise_()
        self.activateWindow()
        logger.info("关机预告窗口已显示（%d 秒）", total_seconds)

    def update_remaining(self, remaining_seconds: int) -> None:
        """刷新倒计时数字。

        Args:
            remaining_seconds: 剩余秒数（≥0）。
        """
        seconds = max(0, int(remaining_seconds))
        self._countdown.setText(f"{seconds} 秒")
        if seconds == 0:
            # 归零由控制器触发关机，这里只是把数字停住。
            self.hide()

    def dismiss(self) -> None:
        """收起预告窗口（取消/锁屏消失时由 app 调用，幂等）。"""
        if self.isVisible():
            self.hide()

    @property
    def remaining_seconds(self) -> int:
        """当前展示的剩余秒数（测试断言用）。"""
        try:
            text = self._countdown.text().replace("秒", "").strip()
            return int(text)
        except (ValueError, AttributeError):  # pragma: no cover - 防御
            return 0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _center_on_primary(self) -> None:
        """放到当前屏幕**可用区**正中央（含 30 秒大数字，矩形较大，居中最稳）。

        弹窗为无边框窗口（``FramelessWindowHint``），``geometry`` 即可见区，
        直接复用通用 ``center_within_available_geometry`` 即可（D6），与
        :class:`~kidtime_client.ui.reminder_popup.ReminderPopup` 的居中策略
        收敛到同一实现。
        """
        self.adjustSize()
        center_within_available_geometry(self)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 命名
        """Esc 等价于点「取消关机」（家长可能正在赶过来）。"""
        if event.key() == Qt.Key_Escape:
            self.cancelRequested.emit()
            return
        super().keyPressEvent(event)


__all__ = ["WARNING_WIDTH", "ShutdownWarningWindow"]
