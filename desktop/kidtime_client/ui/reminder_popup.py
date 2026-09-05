"""倒计时置顶提醒弹窗（1.4.3 增量 · 需求 1）。

背景
----
1.4.2 的 15/5/1 分钟提醒只走托盘气泡（``tray.notify``）——孩子在全屏游戏或
没有盯着托盘时很容易错过。1.4.3 的需求 1 把它升级成**置顶弹窗**：

* 弹窗显示在最上层（``WindowStaysOnTopHint``）并**抢焦点**（产品确认：
  提醒的目的就是让正在使用的孩子必然看到，5 秒自动关闭的打断是可接受的）；
* 用户可以点「知道了」立即关闭，或等弹窗上的倒计时（默认 5 秒）归零自动关闭；
* 与原托盘气泡**双通道**并存（产品确认），提示音照旧。

窗口本身只负责**画**和**发信号**，不感知引擎/锁屏——调用方（``app._on_reminder``）
把 ``(title, body)`` 传进来即可。每次调用都会重置倒计时并重新抢焦点，因此
15/5/1 三个节点复用同一个实例也不会叠加出多个弹窗。

🔴 与锁屏（毛玻璃卡片）不同，这里是**普通系统风格**弹窗：锁屏是强制状态，
提醒是轻量提示，用 ``APP_STYLESHEET`` 的浅色系统观感，不抢锁屏的视觉语言。
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kidtime_client.constants import REMINDER_POPUP_AUTO_CLOSE_SECONDS
from kidtime_client.ui.theme import (
    APP_STYLESHEET,
    COLOR_BORDER,
    COLOR_PRIMARY,
    COLOR_TEXT_WEAK,
    FONT_FAMILY,
)
from kidtime_client.ui.window_policy import center_within_available_geometry

logger = logging.getLogger(__name__)

#: 弹窗固定宽度（逻辑像素）。
POPUP_WIDTH: Final[int] = 420

#: 弹窗自身的补充样式（全局 APP_STYLESHEET 之上叠加）。
_POPUP_QSS: Final[str] = f"""
QFrame#reminderCard {{
    background: #FFFFFF;
    border: 1px solid {COLOR_BORDER};
    border-radius: 14px;
}}
QLabel#reminderTitle {{
    font-family: {FONT_FAMILY};
    font-size: 18px;
    font-weight: 700;
    color: #1F2329;
    background: transparent;
}}
QLabel#reminderBody {{
    font-family: {FONT_FAMILY};
    font-size: 14px;
    color: {COLOR_TEXT_WEAK};
    background: transparent;
}}
QLabel#reminderCountdown {{
    font-family: {FONT_FAMILY};
    font-size: 12px;
    color: {COLOR_TEXT_WEAK};
    background: transparent;
}}
QPushButton#reminderOk {{
    background: {COLOR_PRIMARY};
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 8px 28px;
    font-family: {FONT_FAMILY};
    font-size: 14px;
    font-weight: 600;
}}
QPushButton#reminderOk:hover {{
    background: #245BDB;
}}
"""


class ReminderPopup(QWidget):
    """置顶、抢焦点的倒计时提醒弹窗（单实例复用）。

    Args:
        parent: 可选的 Qt 父对象（生产环境由 app 持有，无需父对象）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setStyleSheet(APP_STYLESHEET + _POPUP_QSS)
        self.setFixedWidth(POPUP_WIDTH)
        #: 距顶 5 秒自动关闭的倒计时剩余秒数。
        self._remaining_seconds: int = 0
        self._dismissed = False

        self._card = QFrame(self)
        self._card.setObjectName("reminderCard")

        self._title = QLabel("", self._card)
        self._title.setObjectName("reminderTitle")
        self._title.setWordWrap(True)

        self._body = QLabel("", self._card)
        self._body.setObjectName("reminderBody")
        self._body.setWordWrap(True)

        self._countdown = QLabel("", self._card)
        self._countdown.setObjectName("reminderCountdown")

        self._ok_button = QPushButton("知道了", self._card)
        self._ok_button.setObjectName("reminderOk")
        self._ok_button.setCursor(Qt.PointingHandCursor)
        self._ok_button.clicked.connect(self.dismiss)

        header = QHBoxLayout()
        header.addWidget(self._title, 1)
        header.addWidget(self._countdown, 0, Qt.AlignTop)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(20, 16, 20, 16)
        card_layout.setSpacing(10)
        card_layout.addLayout(header)
        card_layout.addWidget(self._body)
        card_layout.addSpacing(4)
        card_layout.addWidget(self._ok_button, 0, Qt.AlignRight)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card)

        #: 自动关闭定时器（1 秒一格；显示时启动，关闭/隐藏时停止）。
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------
    def show_reminder(self, title: str, body: str) -> None:
        """展示（或刷新）一次提醒，并重置 5 秒自动关闭倒计时。

        Args:
            title: 标题，如「还有 15 分钟」。
            body: 正文，如「今天剩余 15 分钟，安排好节奏吧。」
        """
        self._title.setText(title)
        self._body.setText(body)
        self._remaining_seconds = max(1, REMINDER_POPUP_AUTO_CLOSE_SECONDS)
        self._dismissed = False
        self._countdown.setText(f"{self._remaining_seconds} 秒后自动关闭")
        self._timer.start()
        self._center_on_primary()
        # 置顶 + 抢焦点：show 之后 raise/activate，确保盖住全屏内容。
        self.show()
        self.raise_()
        self.activateWindow()
        logger.info("倒计时提醒弹窗已显示：%s", title)

    def dismiss(self) -> None:
        """点「知道了」或倒计时归零：立即收起弹窗（幂等）。"""
        if self._dismissed:
            return
        self._dismissed = True
        self._timer.stop()
        self.hide()
        logger.debug("倒计时提醒弹窗已关闭")

    @property
    def remaining_seconds(self) -> int:
        """当前自动关闭倒计时剩余秒数（测试断言用）。"""
        return self._remaining_seconds

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _on_tick(self) -> None:
        """每秒推进自动关闭倒计时。"""
        self._remaining_seconds -= 1
        if self._remaining_seconds <= 0:
            self.dismiss()
            return
        self._countdown.setText(f"{self._remaining_seconds} 秒后自动关闭")

    def _center_on_primary(self) -> None:
        """把弹窗放到当前屏幕**可用区**正中央（不含任务栏）。

        弹窗为无边框窗口（``FramelessWindowHint``），``geometry`` 即可见区，
        直接复用通用 ``center_within_available_geometry`` 即可，无需各写一遍
        居中数学（D6）。多屏时按弹窗所在屏居中，单屏等价于原主屏居中。
        """
        self.adjustSize()
        center_within_available_geometry(self)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 命名
        """Esc 等价于点「知道了」（轻量提示允许键盘关闭）。"""
        if event.key() == Qt.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)


__all__ = ["POPUP_WIDTH", "ReminderPopup"]
