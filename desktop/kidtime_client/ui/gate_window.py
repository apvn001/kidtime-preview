"""启动门禁的双入口锁屏窗口（1.2 增量 · 决策 2 / 决策 5）。

每次启动都会弹这块锁屏——手动启动、开机自启、崩溃重启，三条路径一视同仁
（决策 2）。视觉上复用与倒计时锁屏完全相同的毛玻璃基线 + 居中卡片（决策 5
方案 A），孩子不会觉得这是两个不同的软件。

窗口本身只管**画**和**发信号**，一个判断都不做：
* 显示什么由 :class:`~kidtime_client.core.startup_gate.GateContext` 决定；
* 点击后干什么由 ``app.py`` 的槽函数决定。

🔴 毛玻璃只模糊自绘的品牌渐变，绝不截取桌面（见
:mod:`kidtime_client.ui.blur_backdrop` 的隐私红线说明）。

窗口标志 / 居中卡片 / 毛玻璃绘制 / 关机倒计时 / Esc 吞键等公共部分已上提至
:class:`~kidtime_client.ui.lock_screen_base.LockScreenWidgetBase`（D4）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from kidtime_client.constants import LOCK_STYLE_DEFAULT
from kidtime_client.core.startup_gate import GateContext, GatePhase
from kidtime_client.ui.blur_backdrop import BrandBlurBackdrop
from kidtime_client.ui.lock_screen_base import LockScreenWidgetBase

logger = logging.getLogger(__name__)

#: 双入口首屏的文案。
_PENDING_TITLE = "现在由谁使用这台电脑？"
_PENDING_DETAIL = "选择「孩子使用」后才开始计算今天的上机时间。"

#: 家长待机态的文案（决策 3：已进家长模式，但面板要家长自己点）。
_PARENT_TITLE = "已进入家长模式"
_PARENT_DETAIL = "这段时间不计入孩子的上机时长。可以打开家长面板调整规则。"


class GateWindow(LockScreenWidgetBase):
    """一块屏幕上的门禁锁屏。

    Args:
        screen: 本窗口覆盖的屏幕。
        backdrop: 共享毛玻璃底座（与倒计时锁屏是同一个实例，同分辨率的多屏
            只渲染一次）。
        parent: 可选的 Qt 父对象。
        style: 锁屏外观标识（``LOCK_STYLE_ORDER`` 之一）。

    Attributes:
        childChosen: 孩子入口被点击。
        parentChosen: 家长入口被点击（**尚未验证**，验证由 app 层发起）。
        parentPanelRequested: 家长待机态下请求打开家长面板。
        shutdownRequested: 关机按钮被点击（继承自基类）。
    """

    childChosen = Signal()
    parentChosen = Signal()
    parentPanelRequested = Signal()

    def __init__(
        self,
        screen: QScreen,
        backdrop: BrandBlurBackdrop,
        parent: QWidget | None = None,
        style: str = LOCK_STYLE_DEFAULT,
    ) -> None:
        super().__init__(screen, parent, backdrop, style)
        # 初始渲染成首屏形态，避免 show() 之前有一瞬间的空卡片。
        self.apply_context(GateContext(phase=GatePhase.PENDING))

    # ------------------------------------------------------------------
    # 子类钩子：填充卡片内容
    # ------------------------------------------------------------------
    def _build_card_layout(self) -> None:
        self._primary_button = QPushButton("", self._card)
        self._primary_button.setObjectName("lockPrimary")
        self._primary_button.setCursor(Qt.PointingHandCursor)
        self._primary_button.clicked.connect(self._on_primary_clicked)

        self._secondary_button = QPushButton("", self._card)
        self._secondary_button.setObjectName("lockGhost")
        self._secondary_button.setCursor(Qt.PointingHandCursor)
        self._secondary_button.clicked.connect(self._on_secondary_clicked)

        buttons = QHBoxLayout()
        buttons.setSpacing(16)
        buttons.addStretch(1)
        buttons.addWidget(self._primary_button)
        buttons.addWidget(self._secondary_button)
        buttons.addStretch(1)

        shutdown_row = QHBoxLayout()
        shutdown_row.addStretch(1)
        shutdown_row.addWidget(self._shutdown_button)
        shutdown_row.addStretch(1)

        self._card_layout.addWidget(self._title)
        self._card_layout.addSpacing(14)
        self._card_layout.addWidget(self._detail)
        self._card_layout.addSpacing(30)
        self._card_layout.addLayout(buttons)
        self._card_layout.addSpacing(22)
        self._card_layout.addLayout(shutdown_row)
        self._card_layout.addStretch(1)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def shutdown_visible(self) -> bool:
        """关机按钮当前是否可见（测试断言用）。"""
        return self._shutdown_button.isVisible()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def apply_context(self, ctx: GateContext) -> None:
        """按 ``ctx`` 刷新卡片内容。

        Args:
            ctx: 门禁渲染上下文。
        """
        if ctx.phase is GatePhase.PARENT_STANDBY:
            self._title.setText(_PARENT_TITLE)
            self._detail.setText(_PARENT_DETAIL)
            self._primary_button.setText("打开家长面板")
            self._secondary_button.setText("让孩子开始使用")
        else:
            self._title.setText(_PENDING_TITLE)
            self._detail.setText(_PENDING_DETAIL)
            self._primary_button.setText("孩子使用")
            self._secondary_button.setText("家长设置")

        # 记住当前阶段，供两个按钮的槽函数分发。
        self._phase = ctx.phase
        # 1.4.3：关机按钮固定显示，不随上下文变化（显隐由构造决定，默认可见）。

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_primary_clicked(self) -> None:
        """主按钮：首屏 = 孩子使用；家长待机 = 打开家长面板。"""
        if getattr(self, "_phase", GatePhase.PENDING) is GatePhase.PARENT_STANDBY:
            self.parentPanelRequested.emit()
        else:
            self.childChosen.emit()

    def _on_secondary_clicked(self) -> None:
        """次按钮：首屏 = 家长设置；家长待机 = 让孩子开始使用。"""
        if getattr(self, "_phase", GatePhase.PENDING) is GatePhase.PARENT_STANDBY:
            self.childChosen.emit()
        else:
            self.parentChosen.emit()


__all__ = ["GateWindow"]
