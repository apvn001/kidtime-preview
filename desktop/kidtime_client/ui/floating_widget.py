"""Always-on-top floating widget showing the remaining time.

单机便携形态：倒计时小窗口**固定显示在屏幕右下角**（不可拖拽、不持久化
位置），默认以虚化（半透明）状态呈现，鼠标悬停切换为清晰显示原样。颜色
跟随 :func:`kidtime_client.ui.theme.remaining_color`：green -> orange -> red
随额度耗尽变化。

交互（单机语义）：**双击**打开家长面板；单击不做任何动作。锁屏外观统一为
米黄淡绿护眼样式（由引擎推送 ``LOCK_STYLE_EYECARE``），已无「单点循环轮换
样式」的概念。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from kidtime_client.constants import (
    LOCK_STYLE_DEFAULT,
    SETTING_FLOATING_VISIBLE,
    SETTING_LOCK_STYLE,
    normalize_lock_style,
)
from kidtime_client.core.state_machine import State
from kidtime_client.storage.repositories import SettingsRepository
from kidtime_client.ui.theme import (
    COLOR_OVERLAY_WEAK,
    LOCK_STYLE_FLOATING_BG,
    LOCK_STYLE_WEAK,
    format_minutes,
    remaining_color,
)

logger = logging.getLogger(__name__)

#: 虚化状态窗口透明度。
_DIM_ALPHA: Final[float] = 0.35
#: 悬停清晰状态透明度（原样显示）。
_CLEAR_ALPHA: Final[float] = 1.0
#: 鼠标离开后回到虚化的防抖时间（毫秒），避免在边缘移动时闪烁。
_DIM_DEBOUNCE_MS: Final[int] = 300
#: 右下角贴边距（像素）。
_CORNER_MARGIN: Final[int] = 24
#: 浮窗「舞台」尺寸（含悬停余量，避免在部分 DPI 下被裁切）。
_STAGE_W: Final[int] = 196
_STAGE_H: Final[int] = 100

#: Human-readable label for each state, shown under the remaining time.
_STATE_LABEL: dict[State, str] = {
    State.ACTIVE: "使用中",
    State.GRACE: "临时放行",
    State.PARENT: "家长模式",
    State.BREAK: "休息中",
    State.IDLE_PAUSED: "已暂停计时",
    State.LOCKED_CURFEW: "非使用时段",
    State.LOCKED_QUOTA: "今日已用完",
    State.DISABLED: "管控已关闭",
}


class FloatingWidget(QWidget):
    """Frameless, always-on-top remaining-time badge (fixed bottom-right).

    单机语义：默认虚化（``_DIM_ALPHA``）、悬停清晰；**双击**打开家长面板
    （``doubleClicked``）；单击无动作。可见性偏好持久化到
    ``SETTING_FLOATING_VISIBLE``，位置固定不持久化。样式由引擎推送
    （单机恒定 ``LOCK_STYLE_EYECARE``，经 ``set_style`` 重绘）。

    Args:
        settings: Settings repository used to persist visibility.
        parent: Optional Qt parent.
    """

    doubleClicked = Signal()

    def __init__(self, settings: SettingsRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(_STAGE_W, _STAGE_H)

        container = QWidget(self)
        container.setObjectName("FloatingContainer")
        # 徽标贴住舞台右下角（stage 略大于可见徽标，为字体渲染留足空间）。
        container.setGeometry(_STAGE_W - 148, _STAGE_H - 66, 148, 66)

        self._time_label = QLabel("--", container)
        self._time_label.setAlignment(Qt.AlignCenter)
        self._state_label = QLabel("启动中", container)
        self._state_label.setAlignment(Qt.AlignCenter)

        inner = QVBoxLayout(container)
        inner.setContentsMargins(10, 8, 10, 8)
        inner.setSpacing(2)
        inner.addWidget(self._time_label)

        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.addWidget(self._state_label)
        inner.addLayout(state_row)

        # ⚠️ 关键：舞台（self）**不能**用布局管理 container（否则会把徽标拉伸
        # 填满整个舞台，并在样式表变更等事件中反复覆盖下方几何设定）。浮窗的
        # 位置/几何完全由本类显式 setGeometry / move 控制，container 仅作为
        # 自绘标签的载体。
        self._base_w = 148
        self._base_h = 66
        self._base_rect = self.geometry()
        # 从本地设置恢复初始样式（单机由引擎推送恒定 eyecare，此处仅兜底）。
        self._style = normalize_lock_style(self._settings.get(SETTING_LOCK_STYLE))
        self._last_accent = "#34C724"

        self._container = container
        self._apply_style(self._last_accent, self._style)

        # 悬停防抖定时器：leaveEvent 后延迟回虚化，避免边缘移动闪烁。
        self._dim_timer = QTimer(self)
        self._dim_timer.setSingleShot(True)
        self._dim_timer.setInterval(_DIM_DEBOUNCE_MS)
        self._dim_timer.timeout.connect(self._apply_dim)
        self.setWindowOpacity(_DIM_ALPHA)
        self._place_bottom_right()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def update_from_tick(self, output: Any) -> None:
        """Refresh the badge from a :class:`TickOutput`.

        Args:
            output: The tick result emitted by ``RuntimeEngine.tickCompleted``.
        """
        try:
            remaining = float(output.remaining_minutes)
            state = output.state
        except AttributeError:  # pragma: no cover - defensive
            return

        if state in (State.LOCKED_CURFEW, State.LOCKED_QUOTA):
            self._time_label.setText("0 分钟")
        elif state == State.BREAK and output.overlay_countdown_seconds is not None:
            seconds = max(0, int(output.overlay_countdown_seconds))
            self._time_label.setText(f"休息 {seconds // 60:02d}:{seconds % 60:02d}")
        else:
            self._time_label.setText(format_minutes(remaining))

        self._state_label.setText(_STATE_LABEL.get(state, state.value))
        self._apply_style(remaining_color(remaining), self._style)

    def set_visible_persisted(self, visible: bool) -> None:
        """Show/hide the widget and remember the choice.

        Args:
            visible: Desired visibility.
        """
        self._settings.set_bool(SETTING_FLOATING_VISIBLE, visible)
        self.setVisible(visible)

    def restore_visibility(self) -> None:
        """Apply the persisted visibility preference (visible by default)."""
        self.setVisible(self._settings.get_bool(SETTING_FLOATING_VISIBLE, True))

    def set_style(self, style: str) -> None:
        """外部（引擎）写入当前样式并即时重绘（单机恒定 eyecare，双保险）。

        Args:
            style: 锁屏外观标识（``LOCK_STYLE_ORDER`` 之一）。
        """
        normalized = normalize_lock_style(style)
        if normalized == self._style:
            return
        self._apply_style(self._last_accent, normalized)

    # ------------------------------------------------------------------
    # 虚化 ↔ 清晰状态机
    # ------------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """鼠标悬停：立即切换为清晰显示（原样）。"""
        self._set_clear()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """鼠标离开：防抖后回到虚化状态，避免边缘移动时闪烁。"""
        self._schedule_dim()
        super().leaveEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """首次显示时确保贴住右下角（屏幕可能在构造后才就绪）。"""
        self._place_bottom_right()
        super().showEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """双击打开家长面板。"""
        self.doubleClicked.emit()
        event.accept()

    # ------------------------------------------------------------------
    # 虚化内部
    # ------------------------------------------------------------------
    def _set_clear(self) -> None:
        """悬停清晰：取消防抖定时器并恢复不透明。"""
        self._dim_timer.stop()
        self.setWindowOpacity(_CLEAR_ALPHA)

    def _schedule_dim(self) -> None:
        """离开后启动防抖定时器（超时回到虚化）。"""
        self._dim_timer.start()

    def _apply_dim(self) -> None:
        """应用虚化透明度。"""
        self.setWindowOpacity(_DIM_ALPHA)

    # ------------------------------------------------------------------
    # 样式渲染
    # ------------------------------------------------------------------
    def _apply_style(self, accent: str, style: str = LOCK_STYLE_DEFAULT) -> None:
        """用 ``accent`` 作主色、按 ``style`` 重绘浮窗。

        Args:
            accent: 时间数字的主色（跟随剩余额度绿/橙/红）。
            style: 锁屏外观标识。
        """
        self._last_accent = accent
        self._style = normalize_lock_style(style)

        bg = LOCK_STYLE_FLOATING_BG.get(self._style, LOCK_STYLE_FLOATING_BG[LOCK_STYLE_DEFAULT])
        self._container.setStyleSheet(
            f"""
            QWidget#FloatingContainer {{
                background: {bg};
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 28);
            }}
            """
        )
        self._time_label.setStyleSheet(
            f"color: {accent}; font-size: 19px; font-weight: 700; background: transparent;"
        )
        # 浅底（护眼/护眼2）上必须换成深墨弱色，否则对比度跌破 AA。
        weak = LOCK_STYLE_WEAK.get(self._style, COLOR_OVERLAY_WEAK)
        self._state_label.setStyleSheet(
            f"color: {weak}; font-size: 11px; background: transparent;"
        )

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _place_bottom_right(self) -> None:
        """固定显示在屏幕右下角。

        v1.4 起不再允许拖拽/持久化位置：孩子无法把它挪走，也避免旧坐标在
        分辨率变化后落到屏幕外。边距 ``_CORNER_MARGIN`` 与旧默认保持一致。
        末尾记录基准矩形，供后续布局参照。
        """
        screen = self.screen()
        if screen is None:  # pragma: no cover - offscreen/无屏环境
            return
        geometry = screen.availableGeometry()
        self.move(
            geometry.right() - self.width() - _CORNER_MARGIN,
            geometry.bottom() - self.height() - _CORNER_MARGIN,
        )
        self._base_rect = self.geometry()


__all__ = ["FloatingWidget"]
