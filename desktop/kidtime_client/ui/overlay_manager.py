"""Multi-monitor overlay orchestration (ARCHITECTURE.md §5.19).

One :class:`~kidtime_client.ui.overlay.OverlayWindow` per physical screen, keyed
by ``QScreen.name()``. Hot-plugging a monitor must never leave an uncovered
screen (a gap the child could use) nor a stale window on a removed screen.

Screen add/remove notifications from Windows arrive in bursts (docking, resume
from sleep, driver reset), so rebuilds are debounced by
:data:`REBUILD_DEBOUNCE_MS` and always run on the GUI main thread.

多屏对账 / 防抖 / 外观与关机倒计时透传等公共部分已上提至
:class:`~kidtime_client.ui.multi_screen_lock_manager.MultiScreenLockManager`（D5），
本模块只保留 overlay 专属的上下文（OverlayContext）、apply/update 时序、
屏幕几何/DPI 监听与自动关机心跳。
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QScreen

from kidtime_client.core.lock_auto_shutdown import AutoShutdownController
from kidtime_client.core.state_machine import OVERLAY_BREAK
from kidtime_client.ui.blur_backdrop import BrandBlurBackdrop
from kidtime_client.ui.multi_screen_lock_manager import MultiScreenLockManager
from kidtime_client.ui.overlay import OverlayContext, OverlayWindow

logger = logging.getLogger(__name__)

#: Screen hot-plug events are coalesced over this window (milliseconds).
REBUILD_DEBOUNCE_MS: Final[int] = 300

#: 1.4.3（需求 2b）：日常锁屏的停留累计心跳。与启动门禁的
#: ``GATE_TICK_INTERVAL_MS`` 保持同一节奏，便于日志对齐。
AUTO_SHUTDOWN_TICK_INTERVAL_MS: Final[int] = 1000


class OverlayManager(MultiScreenLockManager):
    """Keeps exactly one overlay window per connected screen.

    1.2 起，管理器还持有一个共享的
    :class:`~kidtime_client.ui.blur_backdrop.BrandBlurBackdrop`：所有屏幕上的
    锁屏窗口共用同一份毛玻璃缓存，同分辨率的双屏只渲染一次。缓存**只**在屏幕
    拓扑变化时失效（增删屏 / geometry 变化 / DPR 变化），倒计时每秒刷新走的是
    缓存命中路径，不会重算模糊。

    Args:
        app: The running :class:`QGuiApplication` (used for screen signals).
        parent: Optional Qt parent.
        backdrop: 共享毛玻璃底座；传 ``None`` 时自行创建一个。允许注入是为了
            让门禁窗口与倒计时锁屏复用**同一个**缓存实例。
        auto_shutdown: 共享的自动关机控制器（与 gate 共享同一实例）。

    Attributes:
        parentUnlockRequested: Emitted when the parent wants to unlock.
        shutdownRequested: Emitted when the child taps the shutdown button
            (1.4.3 需求 2a；BREAK 下按钮隐藏，不会触发)。继承自基类。
    """

    parentUnlockRequested = Signal()

    def __init__(
        self,
        app: QGuiApplication,
        parent: QObject | None = None,  # noqa: F821
        backdrop: BrandBlurBackdrop | None = None,
        auto_shutdown: AutoShutdownController | None = None,
    ) -> None:
        super().__init__(
            app, parent, backdrop, auto_shutdown, rebuild_debounce_ms=REBUILD_DEBOUNCE_MS
        )
        #: 当前锁屏上下文（None = 隐藏）；由 apply()/show()/update() 写入。
        self._context: OverlayContext | None = None
        #: 已挂上 geometry/DPI 监听的屏幕，按 ``QScreen.name()`` 记账，
        #: 避免热插拔反复重建时重复连接同一个信号。
        self._watched_screens: dict[str, QScreen] = {}
        #: 1.4.3：日常锁屏的停留累计心跳（仅在锁屏显示期间运行）。
        self._auto_shutdown_heartbeat = QTimer(self)
        self._auto_shutdown_heartbeat.setInterval(AUTO_SHUTDOWN_TICK_INTERVAL_MS)
        self._auto_shutdown_heartbeat.setTimerType(Qt.CoarseTimer)
        self._auto_shutdown_heartbeat.timeout.connect(self._on_auto_shutdown_heartbeat)
        for screen in app.screens():
            self._watch_screen(screen)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def context(self) -> OverlayContext | None:
        """The context currently rendered, or ``None`` when hidden."""
        return self._context

    # ------------------------------------------------------------------
    # 子类钩子：窗口创建、上下文来源与屏幕监听
    # ------------------------------------------------------------------
    def _has_context(self) -> bool:
        """无上下文（隐藏态）时跳过重建。"""
        return self._context is not None

    def _current_context(self) -> OverlayContext:
        assert self._context is not None
        return self._context

    def _create_window(self, screen: QScreen) -> OverlayWindow:
        # 🔴 注入共享 backdrop：锁屏窗口与启动门禁用同一份缓存，
        # 同分辨率的双屏只渲染一次（架构 §D.5）。
        window = OverlayWindow(screen, backdrop=self._backdrop, style=self._current_style)
        window.parentUnlockRequested.connect(self.parentUnlockRequested, Qt.UniqueConnection)
        window.shutdownRequested.connect(self.shutdownRequested, Qt.UniqueConnection)
        return window

    def _before_reconcile_screen(self, screen: QScreen) -> None:
        """对账循环里、为每块屏幕创建窗口前补挂监听（screenAdded 偶尔被合并掉）。"""
        self._watch_screen(screen)

    def _on_screen_added_extra(self, screen: QScreen) -> None:
        self._watch_screen(screen)

    def _on_screen_removed_extra(self, screen: QScreen, name: str) -> None:
        self._unwatch_screen(screen)
        self._watched_screens.pop(name, None)

    def _shutdown_extra(self) -> None:
        """停掉自动关机心跳定时器。"""
        self._auto_shutdown_heartbeat.stop()

    def _teardown_extra(self) -> None:
        """取消全部屏幕监听并作废毛玻璃缓存。"""
        for screen in list(self._watched_screens.values()):
            self._unwatch_screen(screen)
        self._watched_screens.clear()
        self._backdrop.invalidate()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @Slot(object)
    def apply(self, data: object) -> None:
        """Show, update, or hide the overlay from a ``RuntimeEngine`` signal.

        Args:
            data: The dict emitted by ``RuntimeEngine.overlayRequested``, or
                ``None`` when no overlay should be shown.
        """
        if data is None:
            self.hide()
            return
        if isinstance(data, OverlayContext):
            context = data
        elif isinstance(data, dict):
            context = OverlayContext.from_dict(data)
        else:  # pragma: no cover - defensive
            logger.warning("Ignoring overlay payload of type %r", type(data))
            return
        if self._visible:
            self.update(context)
        else:
            self.show(context)

    def show(self, ctx: OverlayContext) -> None:
        """Cover every screen with the overlay.

        Args:
            ctx: What to render.
        """
        self._context = ctx
        self._visible = True
        self._rebuild()
        # 1.4.3（需求 2b）：锁屏显示 → 开始累计停留时长；BREAK 不计入。
        if self._auto_shutdown is not None:
            self._auto_shutdown.start()
            self._auto_shutdown.set_counting(ctx.reason != OVERLAY_BREAK)
            self._auto_shutdown_heartbeat.start()
        logger.info("Overlay shown (%s) on %d screen(s)", ctx.reason, len(self._windows))

    def update(self, ctx: OverlayContext) -> None:
        """Refresh the text/countdown without re-creating windows.

        Args:
            ctx: The new context.
        """
        self._context = ctx
        if not self._visible:
            return
        # 1.4.3：锁定原因切换（QUOTA ↔ BREAK 等）时同步「是否计入累计」。
        if self._auto_shutdown is not None:
            self._auto_shutdown.set_counting(ctx.reason != OVERLAY_BREAK)
        # A screen may have appeared between two ticks; reconcile cheaply.
        if len(self._windows) != len(self._app.screens()):
            self._schedule_rebuild()
            return
        for window in self._windows.values():
            window.apply_context(ctx)

    def hide(self) -> None:
        """Hide and destroy every overlay window."""
        if not self._visible and not self._windows:
            return
        self._visible = False
        self._context = None
        # 1.4.3（需求 2b）：锁屏消失（解锁/状态切换）→ 停止累计。
        if self._auto_shutdown is not None:
            self._auto_shutdown_heartbeat.stop()
            self._auto_shutdown.stop()
        self._destroy_windows()
        logger.info("Overlay hidden")

    # ------------------------------------------------------------------
    # 1.4.3 自动关机心跳
    # ------------------------------------------------------------------
    @Slot()
    def _on_auto_shutdown_heartbeat(self) -> None:
        """每秒喂给自动关机控制器 1 秒（仅锁屏显示期间运行）。"""
        if not self._visible or self._auto_shutdown is None:
            return
        self._auto_shutdown.tick()

    # ------------------------------------------------------------------
    # 毛玻璃缓存失效（架构 §D.3.3：只有这三类事件才允许 invalidate）
    # ------------------------------------------------------------------
    def _watch_screen(self, screen: QScreen) -> None:
        """监听单个屏幕的 geometry / DPI 变化。

        Args:
            screen: 要监听的屏幕。
        """
        name = screen.name()
        if name in self._watched_screens:
            return
        self._watched_screens[name] = screen
        # geometryChanged：分辨率切换、旋转、多屏排布调整。
        try:
            screen.geometryChanged.connect(self._on_screen_geometry_changed)
        except (AttributeError, RuntimeError):  # pragma: no cover - 平台差异
            logger.debug("屏幕 %s 不支持 geometryChanged", name)
        # logicalDotsPerInchChanged：Windows 上 DPR 变化的实际信号载体
        # （拖到不同缩放比例的显示器、或在设置里改缩放）。
        try:
            screen.logicalDotsPerInchChanged.connect(self._on_screen_dpi_changed)
        except (AttributeError, RuntimeError):  # pragma: no cover - 平台差异
            logger.debug("屏幕 %s 不支持 logicalDotsPerInchChanged", name)

    def _unwatch_screen(self, screen: QScreen) -> None:
        """断开对某个屏幕的监听。

        Args:
            screen: 要取消监听的屏幕。
        """
        for signal, slot in (
            ("geometryChanged", self._on_screen_geometry_changed),
            ("logicalDotsPerInchChanged", self._on_screen_dpi_changed),
        ):
            try:
                getattr(screen, signal).disconnect(slot)
            except (AttributeError, RuntimeError, TypeError):
                # 屏幕已被销毁或本来就没连上，都不是错误。
                pass

    @Slot()
    def _on_screen_geometry_changed(self) -> None:
        """屏幕几何变化：缓存里的尺寸键全部作废。"""
        logger.info("屏幕几何变化，毛玻璃缓存失效")
        self._backdrop.invalidate()
        self._schedule_rebuild()

    @Slot()
    def _on_screen_dpi_changed(self) -> None:
        """屏幕 DPI/DPR 变化：物理像素数变了，必须重渲染。"""
        logger.info("屏幕 DPI 变化，毛玻璃缓存失效")
        self._backdrop.invalidate()
        self._schedule_rebuild()


__all__ = ["AUTO_SHUTDOWN_TICK_INTERVAL_MS", "REBUILD_DEBOUNCE_MS", "OverlayManager"]
