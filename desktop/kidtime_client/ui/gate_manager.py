"""启动门禁的多屏编排（1.2 增量 · 架构文档 §B）。

职责与 :class:`~kidtime_client.ui.overlay_manager.OverlayManager` 完全对称：
每块屏幕一个 :class:`~kidtime_client.ui.gate_window.GateWindow`，按
``QScreen.name()`` 索引，热插拔时防抖重建，绝不留下没被盖住的屏幕——那会是
孩子绕开门禁的现成缺口。

额外多干一件事：持有 1 秒心跳定时器，驱动
:meth:`~kidtime_client.core.startup_gate.StartupGateController.tick`，并且
**只在 tick 报告上下文真的变了的时候**才刷新窗口。每秒无条件 ``update()``
一整屏毛玻璃是纯粹的浪费（虽然背景本身走缓存，但 Qt 的合成开销省不掉）。

多屏对账 / 防抖 / 外观与关机倒计时透传等公共部分已上提至
:class:`~kidtime_client.ui.multi_screen_lock_manager.MultiScreenLockManager`（D5），
本模块只保留门禁专属的门禁上下文、心跳与入口信号转发。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QScreen

from kidtime_client.constants import (
    GATE_REBUILD_DEBOUNCE_MS,
    GATE_TICK_INTERVAL_MS,
)
from kidtime_client.core.gate_log import GateLog
from kidtime_client.core.lock_auto_shutdown import AutoShutdownController
from kidtime_client.core.startup_gate import GateContext, GatePhase, StartupGateController
from kidtime_client.ui.blur_backdrop import BrandBlurBackdrop
from kidtime_client.ui.gate_window import GateWindow
from kidtime_client.ui.multi_screen_lock_manager import MultiScreenLockManager

logger = logging.getLogger(__name__)


class GateManager(MultiScreenLockManager):
    """让门禁锁屏覆盖每一块屏幕，并驱动门禁心跳。

    Args:
        app: 正在运行的 :class:`QGuiApplication`。
        controller: 门禁逻辑核心。
        backdrop: 共享毛玻璃底座；传 ``None`` 时自建一个。生产环境应当把
            ``OverlayManager.backdrop`` 传进来，两块锁屏共用一份缓存。
        gate_log: 门禁本地日志（Q-A12），可空。
        auto_shutdown: 共享的自动关机控制器（与 overlay 共享同一实例）。
        parent: 可选的 Qt 父对象。

    Attributes:
        childChosen: 孩子入口被点击。
        parentChosen: 家长入口被点击（尚未验证）。
        parentPanelRequested: 家长待机态下请求打开家长面板。
        shutdownRequested: 关机按钮被点击（继承自基类）。
    """

    childChosen = Signal()
    parentChosen = Signal()
    parentPanelRequested = Signal()

    def __init__(
        self,
        app: QGuiApplication,
        controller: StartupGateController,
        backdrop: BrandBlurBackdrop | None = None,
        gate_log: GateLog | None = None,
        auto_shutdown: AutoShutdownController | None = None,
        parent: QObject | None = None,  # noqa: F821
    ) -> None:
        super().__init__(
            app, parent, backdrop, auto_shutdown, rebuild_debounce_ms=GATE_REBUILD_DEBOUNCE_MS
        )
        self._controller = controller
        self._gate_log = gate_log
        #: 当前门禁上下文（show/refresh 时从 controller 重新取）。
        self._context: GateContext = controller.context
        #: 1.4.3（需求 2b）：锁屏停留累计 → 15 分钟自动关机。门禁与日常锁屏
        #: 共享同一个控制器实例（app.py 创建后分别注入 GateManager 与
        #: OverlayManager；两者时序上互斥，不会同时累计）。
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(GATE_TICK_INTERVAL_MS)
        self._heartbeat.setTimerType(Qt.CoarseTimer)
        self._heartbeat.timeout.connect(self._on_heartbeat)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def controller(self) -> StartupGateController:
        """底层的门禁逻辑核心。"""
        return self._controller

    @property
    def gate_log(self) -> GateLog | None:
        """门禁本地日志（Q-A12）。"""
        return self._gate_log

    # ------------------------------------------------------------------
    # 子类钩子：窗口创建与上下文来源
    # ------------------------------------------------------------------
    def _has_context(self) -> bool:
        """门禁随时都有 controller 上下文，无需上下文守卫。"""
        return True

    def _current_context(self) -> GateContext:
        return self._context

    def _create_window(self, screen: QScreen) -> GateWindow:
        window = GateWindow(screen, self._backdrop, style=self._current_style)
        window.childChosen.connect(self._on_child_chosen, Qt.UniqueConnection)
        window.parentChosen.connect(self._on_parent_chosen, Qt.UniqueConnection)
        window.parentPanelRequested.connect(self.parentPanelRequested, Qt.UniqueConnection)
        window.shutdownRequested.connect(self.shutdownRequested, Qt.UniqueConnection)
        return window

    def _shutdown_extra(self) -> None:
        """停掉门禁心跳定时器。"""
        self._heartbeat.stop()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def show(self) -> None:
        """铺满所有屏幕并启动心跳。"""
        if self._visible:
            return
        self._visible = True
        self._context = self._controller.context
        self._rebuild()
        self._heartbeat.start()
        # 1.4.3（需求 2b）：门禁显示 → 开始累计停留时长。
        if self._auto_shutdown is not None:
            self._auto_shutdown.start()
        # Q-A12 ①：记录门禁弹出。
        if self._gate_log is not None:
            self._gate_log.log_gate_open(len(self._windows))
        logger.info("启动门禁已显示，覆盖 %d 块屏幕", len(self._windows))

    def refresh(self) -> None:
        """从 controller 重新取上下文并刷新所有窗口。

        阶段切换（家长验证通过、家长模式退出）后由 ``app.py`` 调用。
        """
        if not self._visible:
            return
        self._context = self._controller.context
        # 屏幕可能在两次刷新之间被插拔，顺手对账一次。
        if len(self._windows) != len(self._app.screens()):
            self._schedule_rebuild()
            return
        for window in self._windows.values():
            window.apply_context(self._context)

    def hide(self) -> None:
        """停掉心跳，销毁全部门禁窗口。"""
        self._heartbeat.stop()
        if not self._visible and not self._windows:
            return
        self._visible = False
        # 1.4.3（需求 2b）：门禁消失（孩子开始使用/家长解锁）→ 停止累计。
        if self._auto_shutdown is not None:
            self._auto_shutdown.stop()
        self._destroy_windows()
        logger.info("启动门禁已关闭")

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    @Slot()
    def _on_heartbeat(self) -> None:
        """1 秒心跳：推进锁屏停留累计，阶段变化时刷新窗口。

        1.4.3（需求 2b）：门禁固定显示期间每秒喂给自动关机控制器 1 秒；
        「家长待机态不计入累计」在这里落地（``PARENT_STANDBY`` 暂停）。
        1.2 的「5 分钟无操作露出关机按钮」逻辑已废弃，无需再读 idle 判定
        上下文变化——阶段切换一律由 app 槽函数显式 ``refresh()``。
        """
        if not self._visible:
            return
        if self._auto_shutdown is not None:
            # 家长待机（已验证通过、家长正在使用电脑）不计入累计。
            self._auto_shutdown.set_counting(
                self._controller.phase is not GatePhase.PARENT_STANDBY
            )
            self._auto_shutdown.tick()

    @Slot()
    def _on_child_chosen(self) -> None:
        """孩子入口被选择（记录后转发给 app 层）。"""
        if self._gate_log is not None:
            self._gate_log.log_entrance("child")
        self.childChosen.emit()

    @Slot()
    def _on_parent_chosen(self) -> None:
        """家长入口被选择（记录后转发给 app 层）。"""
        if self._gate_log is not None:
            self._gate_log.log_entrance("parent")
        self.parentChosen.emit()


__all__ = ["GateManager"]
