"""多屏锁屏管理器的共同基类（D5：抽取 GateManager 与 OverlayManager 的近逐行重复）。

两块锁屏管理器的职责完全对称——每块屏幕一个锁屏窗口、按 ``QScreen.name()`` 索引、
热插拔时防抖重建、绝不留下没被盖住的屏幕（那是孩子绕开门禁的现成缺口）。

本基类把以下公共部分上提，子类只需通过模板方法钩子提供「窗口类型 / 信号接线 /
上下文来源 / 屏幕监听策略」：

* 共享状态：``_app`` / ``_windows`` / ``_visible`` / ``_backdrop`` / ``_auto_shutdown``
  / ``_current_style``；
* 防抖定时器与屏幕热插拔信号接线（``screenAdded`` / ``screenRemoved``）；
* ``visible`` / ``window_count`` / ``current_style`` / ``backdrop`` 属性；
* ``set_style`` / ``set_shutdown_countdown`` / ``clear_shutdown_countdown``；
* ``_schedule_rebuild`` 与 ``_rebuild`` 的对账骨架（丢弃消失屏 + 逐屏创建/刷新）；
* ``shutdown`` 的通用拆解（停定时器 → 断开信号 → 子类收尾 → hide）。

🔴 所有钩子默认实现都保留原管理器的逐行行为；本类只做"把重复代码挪一处"，
   不改任何时序、日志语义或信号接线。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QScreen

from kidtime_client.constants import LOCK_STYLE_DEFAULT, normalize_lock_style
from kidtime_client.ui.blur_backdrop import BrandBlurBackdrop

logger = logging.getLogger(__name__)


class MultiScreenLockManager(QObject):
    """多屏锁屏管理器的共同基类。

    Args:
        app: 正在运行的 :class:`QGuiApplication`。
        parent: 可选的 Qt 父对象。
        backdrop: 共享毛玻璃底座；传 ``None`` 时自建一个。生产环境应当把
            另一块锁屏的 ``backdrop`` 传进来，共用同一份缓存。
        auto_shutdown: 共享的自动关机控制器（两块锁屏时序互斥，共享同一实例）。
        rebuild_debounce_ms: 热插拔重建防抖窗口（毫秒）。
    """

    #: 关机按钮被点击（两个锁屏同款，基类统一接线）。
    shutdownRequested = Signal()

    def __init__(
        self,
        app: QGuiApplication,
        parent: QObject | None = None,
        backdrop: BrandBlurBackdrop | None = None,
        auto_shutdown=None,
        rebuild_debounce_ms: int = 300,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._windows: dict[str, QWidget] = {}  # noqa: F821 - QWidget 仅作注解
        self._visible = False
        self._backdrop = backdrop if backdrop is not None else BrandBlurBackdrop()
        self._auto_shutdown = auto_shutdown
        #: 当前锁屏外观（default/eyecare/eyecare2），由引擎经 app 统一推送。
        self._current_style = LOCK_STYLE_DEFAULT

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(rebuild_debounce_ms)
        self._debounce.timeout.connect(self._rebuild)

        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)

    # ------------------------------------------------------------------
    # 共享属性
    # ------------------------------------------------------------------
    @property
    def visible(self) -> bool:
        """锁屏当前是否显示中。"""
        return self._visible

    @property
    def window_count(self) -> int:
        """存活的锁屏窗口数（每屏一个）。"""
        return len(self._windows)

    @property
    def current_style(self) -> str:
        """当前锁屏外观标识（供 app 初始化推送时读取）。"""
        return self._current_style

    @property
    def backdrop(self) -> BrandBlurBackdrop:
        """共享毛玻璃底座（两块锁屏共用同一份缓存）。"""
        return self._backdrop

    # ------------------------------------------------------------------
    # 外观 / 关机倒计时透传（两个锁屏逐字相同）
    # ------------------------------------------------------------------
    def set_style(self, style: str) -> None:
        """统一切换锁屏外观：透传各窗口 + 同步共享毛玻璃底座（R-5）。

        Args:
            style: 锁屏外观标识（``LOCK_STYLE_ORDER`` 之一）。
        """
        normalized = normalize_lock_style(style)
        if normalized == self._current_style:
            return
        self._current_style = normalized
        self._backdrop.set_style(normalized)
        for window in self._windows.values():
            window.apply_style(normalized)

    def set_shutdown_countdown(self, remaining_seconds: int) -> None:
        """把「距自动关机」倒计时透传给所有锁屏窗口。

        Args:
            remaining_seconds: 距自动关机剩余秒数（≥0，已含预告时长）。
        """
        for window in self._windows.values():
            window.show_shutdown_countdown(int(remaining_seconds))

    def clear_shutdown_countdown(self) -> None:
        """收起所有锁屏窗口上的关机倒计时（取消/关机时由 app 调用）。"""
        for window in self._windows.values():
            window.clear_shutdown_countdown()

    # ------------------------------------------------------------------
    # 多屏热插拔（公共骨架）
    # ------------------------------------------------------------------
    @Slot(QScreen)
    def _on_screen_added(self, screen: QScreen) -> None:
        """新接入一块屏幕。"""
        logger.info("%s: screen added %s", type(self).__name__, screen.name())
        self._backdrop.invalidate()
        self._on_screen_added_extra(screen)
        self._schedule_rebuild()

    @Slot(QScreen)
    def _on_screen_removed(self, screen: QScreen) -> None:
        """拔掉一块屏幕：立即丢弃对应窗口，再防抖对账。"""
        name = screen.name()
        logger.info("%s: screen removed %s", type(self).__name__, name)
        self._on_screen_removed_extra(screen, name)
        window = self._windows.pop(name, None)
        if window is not None:
            window.hide()
            window.deleteLater()
        self._backdrop.invalidate()
        self._schedule_rebuild()

    def _schedule_rebuild(self) -> None:
        """（重新）启动防抖定时器。"""
        self._debounce.start()

    @Slot()
    def _rebuild(self) -> None:
        """把 ``self._windows`` 与当前屏幕列表对账（公共骨架）。"""
        self._debounce.stop()
        if not self._visible or not self._has_context():
            return

        screens = {screen.name(): screen for screen in self._app.screens()}

        # 屏幕没了，窗口也得走。
        for name in list(self._windows):
            if name not in screens:
                window = self._windows.pop(name)
                window.hide()
                window.deleteLater()

        # 每块在线屏幕都要有一个窗口。
        context = self._current_context()
        for name, screen in screens.items():
            # 热插拔重建时补挂监听（子类按需实现）。
            self._before_reconcile_screen(screen)
            window = self._windows.get(name)
            if window is None:
                window = self._create_window(screen)
                self._windows[name] = window
            # 热插拔后重新对齐外观（共享 backdrop 已按 _current_style 失效重渲）。
            window.apply_style(self._current_style)
            window.apply_geometry(screen)
            window.apply_context(context)
            window.show()
            window.raise_()
            window.activateWindow()

    # ------------------------------------------------------------------
    # 拆解（公共骨架）
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """彻底拆除（锁屏关闭或应用退出时调用）。"""
        self._debounce.stop()
        self._shutdown_extra()
        self._disconnect_screens()
        self._teardown_extra()
        self.hide()

    def _disconnect_screens(self) -> None:
        """断开屏幕热插拔信号（幂等）。"""
        try:
            self._app.screenAdded.disconnect(self._on_screen_added)
            self._app.screenRemoved.disconnect(self._on_screen_removed)
        except (RuntimeError, TypeError):  # pragma: no cover - 已经断开
            pass

    def _destroy_windows(self) -> None:
        """隐藏并销毁全部锁屏窗口。"""
        for window in list(self._windows.values()):
            window.hide()
            window.deleteLater()
        self._windows.clear()

    # ------------------------------------------------------------------
    # 子类钩子（默认实现保留原管理器逐行行为）
    # ------------------------------------------------------------------
    def _has_context(self) -> bool:
        """当前是否有可渲染的上下文（无则跳过重建）。"""
        return True

    def _current_context(self):
        """返回当前要渲染的上下文对象。"""
        raise NotImplementedError

    def _create_window(self, screen: QScreen):
        """为单块屏幕创建并接好信号的锁屏窗口。"""
        raise NotImplementedError

    def _before_reconcile_screen(self, screen: QScreen) -> None:
        """对账循环里、为每块屏幕创建窗口前调用（overlay 在此补挂监听）。"""

    def _on_screen_added_extra(self, screen: QScreen) -> None:
        """屏幕接入时的子类额外动作（overlay 在此补挂监听）。"""

    def _on_screen_removed_extra(self, screen: QScreen, name: str) -> None:
        """屏幕移除时的子类额外动作（overlay 在此取消监听）。"""

    def _shutdown_extra(self) -> None:
        """停掉子类专属定时器（gate 停心跳 / overlay 停自动关机心跳）。"""

    def _teardown_extra(self) -> None:
        """拆解时的子类收尾（overlay 取消屏幕监听 + 作废缓存）。"""


__all__ = ["MultiScreenLockManager"]
