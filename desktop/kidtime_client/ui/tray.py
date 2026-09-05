"""System tray icon and context menu (ARCHITECTURE.md §5.19).

The tray is the only always-reachable entry point: the parent uses it to open
the panel; the child cannot close the guard from here (quitting is gated behind
parent mode, PRD §4.6). 单机形态的托盘只保留：状态行 / 显示-隐藏悬浮窗 /
家长面板 / 退出——云模式下的延时申请、立即同步、检查更新均不在产品范围内。

The icon is painted in code so the frozen executable needs no external assets.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, QRect, Qt, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from kidtime_client.constants import APP_DISPLAY_NAME
from kidtime_client.core.state_machine import State
from kidtime_client.ui.theme import (
    COLOR_DANGER,
    COLOR_PRIMARY,
    COLOR_TEXT_WEAK,
    COLOR_WARNING,
    format_minutes,
    remaining_color,
)

logger = logging.getLogger(__name__)

_STATE_TEXT: dict[State, str] = {
    State.DISABLED: "管控已暂停",
    State.GRACE: "临时放行中",
    State.PARENT: "家长模式",
    State.LOCKED_CURFEW: "非可用时段",
    State.LOCKED_QUOTA: "今日额度用完",
    State.BREAK: "休息中",
    State.IDLE_PAUSED: "已暂停计时",
    State.ACTIVE: "正在使用",
}


def build_icon(accent: str = COLOR_PRIMARY) -> QIcon:
    """Paint a simple rounded-square "K" icon.

    Args:
        accent: Fill colour in ``#RRGGBB`` form.

    Returns:
        A multi-resolution :class:`QIcon`.
    """
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(accent)))
        radius = max(2, size // 5)
        painter.drawRoundedRect(QRect(0, 0, size, size), radius, radius)
        painter.setPen(QColor("#FFFFFF"))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(max(8, int(size * 0.62)))
        painter.setFont(font)
        painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, "K")
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class TrayIcon(QObject):
    """Owns the ``QSystemTrayIcon`` and translates clicks into signals.

    Args:
        parent_window: Widget used as the menu's logical parent (may be ``None``).
        parent: Optional Qt parent.

    Attributes:
        parentPanelRequested: Someone wants the parent panel (password gated).
        toggleFloatingRequested: Toggle the floating badge.
        quitRequested: Quit the application (password gated by the caller).
    """

    parentPanelRequested = Signal()
    toggleFloatingRequested = Signal()
    quitRequested = Signal()

    def __init__(
        self, parent_window: QWidget | None = None, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(build_icon(), self)
        self._tray.setToolTip(APP_DISPLAY_NAME)

        self._menu = QMenu(parent_window)
        self._status_action = QAction("正在启动…", self._menu)
        self._status_action.setEnabled(False)
        self._menu.addAction(self._status_action)
        self._menu.addSeparator()

        self._floating_action = QAction("显示/隐藏悬浮窗", self._menu)
        self._floating_action.triggered.connect(self.toggleFloatingRequested)
        self._menu.addAction(self._floating_action)

        self._menu.addSeparator()

        self._panel_action = QAction("家长模式…", self._menu)
        self._panel_action.triggered.connect(self.parentPanelRequested)
        self._menu.addAction(self._panel_action)

        self._quit_action = QAction("退出（需要家长密码）", self._menu)
        self._quit_action.triggered.connect(self.quitRequested)
        self._menu.addAction(self._quit_action)

        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def show(self) -> None:
        """Make the tray icon visible."""
        self._tray.show()

    def hide(self) -> None:
        """Hide the tray icon."""
        self._tray.hide()

    @property
    def available(self) -> bool:
        """``True`` when the desktop environment offers a system tray."""
        return QSystemTrayIcon.isSystemTrayAvailable()

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------
    @Slot(object)
    def update_from_tick(self, output: Any) -> None:
        """Refresh tooltip and status line from a tick result.

        Args:
            output: A ``TickOutput`` instance.
        """
        state = getattr(output, "state", State.ACTIVE)
        remaining = float(getattr(output, "remaining_minutes", 0.0) or 0.0)
        label = _STATE_TEXT.get(state, state.value if hasattr(state, "value") else "—")
        text = f"{label} · 剩余 {format_minutes(remaining)}"
        self._status_action.setText(text)
        self._tray.setToolTip(f"{APP_DISPLAY_NAME}\n{text}")
        self._tray.setIcon(build_icon(remaining_color(remaining)))

    @Slot(bool, str)
    def update_sync_status(self, ok: bool, message: str) -> None:
        """同步状态由日志与状态行承载；托盘不再单列同步项（内嵌服务即本机）。

        Args:
            ok: Whether the sync succeeded.
            message: Human-readable detail.
        """
        logger.debug("sync status: ok=%s %s", ok, message)

    def set_parent_mode(self, active: bool) -> None:
        """Adjust wording while parent mode is active.

        Args:
            active: ``True`` when a parent session is open.
        """
        self._panel_action.setText("家长面板…" if active else "家长模式…")
        self._quit_action.setText("退出" if active else "退出（需要家长密码）")

    def notify(self, title: str, message: str, warning: bool = False) -> None:
        """Show a balloon notification.

        Args:
            title: Balloon title.
            message: Balloon body.
            warning: Use the warning icon instead of the information icon.
        """
        icon = QSystemTrayIcon.Warning if warning else QSystemTrayIcon.Information
        try:
            self._tray.showMessage(title, message, icon, 6000)
        except Exception:  # pragma: no cover - platform dependent
            logger.debug("Tray balloon unavailable", exc_info=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Open the parent panel on double click.

        Args:
            reason: Why the tray icon was activated.
        """
        if reason == QSystemTrayIcon.DoubleClick:
            self.parentPanelRequested.emit()


_RESERVED_COLORS = (COLOR_WARNING, COLOR_DANGER, COLOR_TEXT_WEAK)

__all__ = ["TrayIcon", "build_icon"]
