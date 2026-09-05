"""置顶消息框工具（1.4.3 修复 · 关机确认框被锁屏盖住）。

背景
----
锁屏窗口（``GateWindow`` / ``OverlayWindow``）的标志是
``FramelessWindowHint | WindowStaysOnTopHint | Tool | BypassWindowManagerHint``
——永远置顶且绕过窗口管理器。而 ``QMessageBox.question()`` 这类**静态方法**
创建的对话框没有任何置顶标志，Z 序低于锁屏：窗口其实弹出来了，但被锁屏
盖在下面，用户看不见也点不到（1.4.3 实测 bug：点击锁屏「关机」按钮后无法
确认）。

历史上锁屏之上的弹窗对话框被锁屏盖住，解决方式是 ``setWindowFlag(
Qt.WindowStaysOnTopHint, True)``（已真机验收）。本模块把它固化成通用工具：
**凡是在锁屏之上必须可见的 QMessageBox，一律从这里创建**——除了加
``WindowStaysOnTopHint``，还要 ``raise_()`` + ``activateWindow()`` 抢焦点，
确保与锁屏（同为 Topmost）争抢 Z 序时后弹出的对话框胜出。

1.4.4 增量：居中显示。``QMessageBox`` 静态方法默认位置由 Qt 决定，往往不
在屏幕中央；显式把它移到主屏**可用区**正中央，与 ``ReminderPopup`` /
``ShutdownWarningWindow`` 三处一致。调用方可用 ``center=False`` 关掉
（少数嵌进某个父窗口的子框场景保留旧行为）。

🔴 用法红线：只有「锁屏之上必须可见」的确认/告警才用本模块（当前唯一场景是
   ``app._on_shutdown_requested`` 的关机确认与失败告警）。普通桌面对话框继续
   用 QMessageBox 静态方法，不要为了统一而全局替换——多余置顶会干扰正常
   窗口层级。
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from kidtime_client.ui.window_policy import center_within_available_geometry

logger = logging.getLogger(__name__)

#: 图标枚举透传（调用方少 import 一个名字）。
IconQuestion: Final = QMessageBox.Icon.Question
IconWarning: Final = QMessageBox.Icon.Warning


def build_always_on_top_box(
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton,
    default_button: QMessageBox.StandardButton,
    parent: QWidget | None = None,
    *,
    center: bool = True,
) -> QMessageBox:
    """构造一个**置顶**的 ``QMessageBox``（不 exec，供测试与复用）。

    Args:
        icon: 图标（``IconQuestion`` / ``IconWarning`` 等）。
        title: 标题。
        text: 正文。
        buttons: 按钮组合。
        default_button: 默认按钮（按回车即触发，危险操作给 No）。
        parent: 可选的父窗口；锁屏场景传 ``None``。
        center: 是否把对话框移到主屏可用区正中央。默认 ``True``。

    Returns:
        已设置 ``WindowStaysOnTopHint`` 且 ``raise_``/``activateWindow`` 的
        消息框实例；调用方负责 ``exec()``。
    """
    box = QMessageBox(parent)
    # 🔴 关键修复：置顶标志——否则被 BypassWindowManagerHint 的锁屏盖住。
    box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    box.setDefaultButton(default_button)
    # 1.4.4：居中。和锁屏同为 Topmost 时居中对齐看起来更协调，避免 Qt
    # 默认位置飘到屏幕角落；只要有主屏就优先居中。
    if center:
        box.adjustSize()
        center_within_available_geometry(box)
    # 与锁屏同为 Topmost 时，后激活者胜出：显式抢焦点保证对话框可见可点。
    box.raise_()
    box.activateWindow()
    return box


def confirm_on_top(
    title: str,
    text: str,
    parent: QWidget | None = None,
    default_no: bool = True,
) -> bool:
    """在锁屏之上弹「是/否」确认框（默认答案是「否」）。

    Args:
        title: 标题。
        text: 正文。
        parent: 可选父窗口（锁屏场景传 ``None``）。
        default_no: 默认按钮是否为「否」（危险操作建议 ``True``）。

    Returns:
        ``True`` 当用户点了「是」。
    """
    default = QMessageBox.No if default_no else QMessageBox.Yes
    box = build_always_on_top_box(
        IconQuestion,
        title,
        text,
        QMessageBox.Yes | QMessageBox.No,
        default,
        parent,
    )
    return box.exec() == QMessageBox.Yes


def warn_on_top(title: str, text: str, parent: QWidget | None = None) -> None:
    """在锁屏之上弹「警告」框（单「确定」按钮）。

    Args:
        title: 标题。
        text: 正文。
        parent: 可选父窗口（锁屏场景传 ``None``）。
    """
    box = build_always_on_top_box(
        IconWarning,
        title,
        text,
        QMessageBox.Ok,
        QMessageBox.Ok,
        parent,
    )
    box.exec()


__all__ = [
    "IconQuestion",
    "IconWarning",
    "build_always_on_top_box",
    "confirm_on_top",
    "warn_on_top",
]
