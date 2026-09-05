"""锁屏类窗口的共同基类（D4：抽取 GateWindow 与 OverlayWindow 的近逐行重复）。

两类锁屏（启动门禁 / 倒计时锁屏）视觉语言完全一致——同一套无边框置顶窗口标志、
同一张毛玻璃品牌底、同一个 ``lockCard`` 居中卡片、同一组关机按钮与 Esc 吞键逻辑。
本基类把这些「画」与「基础交互」上提，子类只需：

* 在 ``_build_card_layout`` 里往 ``self._card_layout`` 塞自己的控件
  （标题/详情卡片容器已由基类建好）；
* 定义自己的信号与 ``apply_context``（两者渲染语义不同）。

🔴 注意：本基类只重组控件父子关系与公共绘制/交互，不改变任何渲染逻辑或文案。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QPaintEvent, QPainter, QScreen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kidtime_client.constants import LOCK_STYLE_DEFAULT, normalize_lock_style
from kidtime_client.ui.blur_backdrop import BrandBlurBackdrop, paint_backdrop
from kidtime_client.ui.theme import (
    CARD_MIN_HEIGHT,
    CARD_PADDING,
    CARD_WIDTH,
    format_countdown,
    lock_surface_stylesheet,
)


class LockScreenWidgetBase(QWidget):
    """锁屏窗口基类：统一窗口标志、毛玻璃底、居中卡片容器与基础交互。

    Args:
        screen: 本窗口覆盖的屏幕。
        parent: 可选的 Qt 父对象。
        backdrop: 共享毛玻璃底座；传 ``None`` 时自建一个。
        style: 锁屏外观标识（``LOCK_STYLE_ORDER`` 之一）。
    """

    #: 关机按钮被点击（两个锁屏同款，基类统一接线）。
    shutdownRequested = Signal()

    def __init__(
        self,
        screen: QScreen,
        parent: QWidget | None = None,
        backdrop: BrandBlurBackdrop | None = None,
        style: str = LOCK_STYLE_DEFAULT,
    ) -> None:
        super().__init__(parent)
        self._screen = screen
        self._backdrop = backdrop if backdrop is not None else BrandBlurBackdrop()
        self._style = normalize_lock_style(style)

        # 与另一锁屏保持同一套窗口标志：无边框、置顶、绕过窗管，
        # 这样孩子没法用 Alt+Tab 或者拖拽把它挪开。
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.setStyleSheet(lock_surface_stylesheet(self._style))

        # --- 居中卡片容器（子类在 _build_card_layout 内填充内容）--------
        self._card = QFrame(self)
        self._card.setObjectName("lockCard")
        self._card.setFixedWidth(CARD_WIDTH)
        self._card.setMinimumHeight(CARD_MIN_HEIGHT)

        self._title = QLabel("", self._card)
        self._title.setObjectName("lockTitle")
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setWordWrap(True)

        self._detail = QLabel("", self._card)
        self._detail.setObjectName("lockDetail")
        self._detail.setAlignment(Qt.AlignCenter)
        self._detail.setWordWrap(True)

        # 关机按钮（两个锁屏同款 lockDanger；显隐由各自 apply_context 决定）。
        self._shutdown_button = QPushButton("关机", self._card)
        self._shutdown_button.setObjectName("lockDanger")
        self._shutdown_button.setCursor(Qt.PointingHandCursor)
        self._shutdown_button.clicked.connect(self.shutdownRequested.emit)

        self._card_layout = QVBoxLayout(self._card)
        self._card_layout.setContentsMargins(
            CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING
        )
        self._card_layout.setSpacing(0)
        self._card_layout.addStretch(1)
        self._build_card_layout()

        # --- 卡片在屏幕正中 ---------------------------------------------
        centering = QHBoxLayout()
        centering.addStretch(1)
        centering.addWidget(self._card)
        centering.addStretch(1)

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 40, 40, 40)
        root.addStretch(1)
        root.addLayout(centering)
        root.addStretch(1)

        self.apply_geometry(screen)

    # ------------------------------------------------------------------
    # 子类钩子
    # ------------------------------------------------------------------
    def _build_card_layout(self) -> None:
        """子类填充卡片内部布局（标题/详情之上已放好居中拉伸）。

        子类应复用 ``self._title`` / ``self._detail`` / ``self._shutdown_button``，
        并在末尾补一个收尾 ``addStretch(1)``。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 共享属性 / 行为
    # ------------------------------------------------------------------
    @property
    def screen_name(self) -> str:
        """本窗口绑定的 ``QScreen.name()``。"""
        return self._screen.name()

    def apply_style(self, style: str) -> None:
        """切换锁屏外观（FR-1）：重设卡片/文字配色样式表。

        Args:
            style: 锁屏外观标识（``LOCK_STYLE_ORDER`` 之一）。
        """
        self._style = normalize_lock_style(style)
        self.setStyleSheet(lock_surface_stylesheet(self._style))

    def apply_geometry(self, screen: QScreen) -> None:
        """把窗口铺满 ``screen``。

        Args:
            screen: 目标屏幕（热插拔重建后可能换了对象）。
        """
        self._screen = screen
        self.setGeometry(screen.geometry())

    # ------------------------------------------------------------------
    # 1.4.4 增量：关机倒计时（整合进关机按钮文字）
    # ------------------------------------------------------------------
    def show_shutdown_countdown(self, remaining_seconds: int) -> None:
        """把「距自动关机 MM:SS」显示到关机按钮上（由 app 经控制器回调驱动）。

        Args:
            remaining_seconds: 距自动关机剩余秒数（≥0，已含预告时长）。
        """
        self._shutdown_button.setText(f"关机 {format_countdown(int(remaining_seconds))}")

    def clear_shutdown_countdown(self) -> None:
        """把关机按钮文字恢复为「关机」（取消/关机/锁屏消失时调用，幂等）。"""
        self._shutdown_button.setText("关机")

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt 命名
        """画毛玻璃底 + 轻遮罩。

        Args:
            event: Qt 重绘事件。
        """
        painter = QPainter(self)
        try:
            paint_backdrop(
                painter,
                self.size(),
                self.devicePixelRatioF(),
                self._backdrop,
            )
        finally:
            painter.end()
        super().paintEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt 命名
        """吞掉 Esc，锁屏不能用键盘关掉。

        Args:
            event: Qt 按键事件。
        """
        if event.key() == Qt.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)


__all__ = ["LockScreenWidgetBase"]
