"""Full-screen lock overlay shown for CURFEW / QUOTA / BREAK.

Tone matters: the overlay explains why the screen is locked and when the child
can come back. It never scolds.

1.2 增量（决策 5 · 方案 A）
--------------------------
锁屏从「一整块纯色底 + 居中堆叠文字」改成与启动门禁完全一致的
**毛玻璃品牌渐变 + 居中卡片**。孩子在开机时看到的门禁、和用完额度时看到的
锁屏，现在是同一种视觉语言，不会觉得是两个软件。

* 底层：:func:`~kidtime_client.ui.blur_backdrop.paint_backdrop`（与门禁共用
  同一个 :class:`BrandBlurBackdrop` 实例，缓存也共用）。
* 上层：``objectName="lockCard"`` 的居中卡片，样式来自
  :func:`~kidtime_client.ui.theme.lock_surface_stylesheet`。

窗口标志 / 居中卡片 / 毛玻璃绘制 / 关机倒计时 / Esc 吞键等公共部分已上提至
:class:`~kidtime_client.ui.lock_screen_base.LockScreenWidgetBase`（D4）。

🔴 :class:`OverlayContext` 的 6 个字段是**冻结契约**，1.2 一个都没动，
   :meth:`OverlayWindow.apply_context` 的函数体也**一行未改**——重构只发生在
   控件的父子关系和绘制上，渲染逻辑保持原样。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QLabel, QHBoxLayout, QPushButton, QWidget

from kidtime_client.ui.blur_backdrop import BrandBlurBackdrop
from kidtime_client.ui.lock_screen_base import LockScreenWidgetBase
from kidtime_client.ui.theme import format_countdown
from kidtime_client.constants import LOCK_STYLE_DEFAULT


@dataclass(frozen=True, slots=True)
class OverlayContext:
    """Everything the overlay needs to render one lock reason.

    Attributes:
        reason: ``CURFEW`` | ``QUOTA`` | ``BREAK``.
        title: Large headline.
        detail: Supporting sentence explaining the rule.
        countdown_seconds: Remaining break seconds, or ``None``.
        next_available_text: When the child may use the computer again.
        allow_shutdown: ``False`` during a forced break（1.4.3 需求 2a：
            BREAK 不显示关机按钮、不参与 15 分钟自动关机）。
    """

    reason: str
    title: str
    detail: str
    countdown_seconds: int | None = None
    next_available_text: str | None = None
    allow_shutdown: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "OverlayContext":
        """Build a context from the dict emitted by ``RuntimeEngine``.

        Args:
            data: Mapping produced by ``RuntimeEngine._overlay_context``.

        Returns:
            The parsed :class:`OverlayContext`.
        """
        return cls(
            reason=str(data.get("reason") or "QUOTA"),
            title=str(data.get("title") or "已锁定"),
            detail=str(data.get("detail") or ""),
            countdown_seconds=data.get("countdown_seconds"),
            next_available_text=data.get("next_available_text"),
            allow_shutdown=bool(data.get("allow_shutdown", True)),
        )


class OverlayWindow(LockScreenWidgetBase):
    """One full-screen overlay bound to a single :class:`QScreen`.

    Args:
        screen: The screen this window covers.
        parent: Optional Qt parent.
        backdrop: 共享毛玻璃底座。传 ``None`` 时自建一个，但生产环境应当由
            :class:`~kidtime_client.ui.overlay_manager.OverlayManager` 注入，
            以便与启动门禁共用同一份缓存。
        style: 锁屏外观标识（``LOCK_STYLE_ORDER`` 之一）。
    """

    parentUnlockRequested = Signal()

    def __init__(
        self,
        screen: QScreen,
        parent: QWidget | None = None,
        backdrop: BrandBlurBackdrop | None = None,
        style: str = LOCK_STYLE_DEFAULT,
    ) -> None:
        super().__init__(screen, parent, backdrop, style)

    # ------------------------------------------------------------------
    # 子类钩子：填充卡片内容
    # ------------------------------------------------------------------
    def _build_card_layout(self) -> None:
        self._countdown = QLabel("", self._card)
        self._countdown.setObjectName("lockCountdown")
        self._countdown.setAlignment(Qt.AlignCenter)

        self._next = QLabel("", self._card)
        self._next.setObjectName("lockCaption")
        self._next.setAlignment(Qt.AlignCenter)
        self._next.setWordWrap(True)

        self._parent_button = QPushButton("家长解锁", self._card)
        self._parent_button.setObjectName("lockGhost")
        self._parent_button.setCursor(Qt.PointingHandCursor)
        self._parent_button.clicked.connect(self.parentUnlockRequested.emit)

        # 1.4.3（需求 2a）：日常锁屏的关机按钮，与启动门禁同款 lockDanger 样式。
        # 默认隐藏；``apply_context`` 按 ``allow_shutdown`` 显示（BREAK 除外）。
        self._shutdown_button.setVisible(False)

        buttons = QHBoxLayout()
        buttons.setSpacing(16)
        buttons.addStretch(1)
        buttons.addWidget(self._parent_button)
        buttons.addStretch(1)

        shutdown_row = QHBoxLayout()
        shutdown_row.addStretch(1)
        shutdown_row.addWidget(self._shutdown_button)
        shutdown_row.addStretch(1)

        self._card_layout.addWidget(self._title)
        self._card_layout.addSpacing(12)
        self._card_layout.addWidget(self._detail)
        self._card_layout.addSpacing(18)
        self._card_layout.addWidget(self._countdown)
        self._card_layout.addSpacing(10)
        self._card_layout.addWidget(self._next)
        self._card_layout.addSpacing(28)
        self._card_layout.addLayout(buttons)
        self._card_layout.addSpacing(16)
        self._card_layout.addLayout(shutdown_row)
        self._card_layout.addStretch(1)

    def apply_context(self, ctx: OverlayContext) -> None:
        """Render ``ctx``.

        Args:
            ctx: The lock reason to display.
        """
        self._title.setText(ctx.title)
        self._detail.setText(ctx.detail)

        if ctx.countdown_seconds is not None:
            self._countdown.setText(format_countdown(ctx.countdown_seconds))
            self._countdown.setVisible(True)
        else:
            self._countdown.setVisible(False)

        if ctx.next_available_text:
            self._next.setText(f"下次可以使用的时间：{ctx.next_available_text}")
            self._next.setVisible(True)
        else:
            self._next.setVisible(False)

        # 1.4.3（需求 2a）：BREAK 不显示关机按钮（也不参与自动关机）。
        self._shutdown_button.setVisible(bool(ctx.allow_shutdown))


__all__ = ["OverlayContext", "OverlayWindow"]
