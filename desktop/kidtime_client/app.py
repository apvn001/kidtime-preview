"""Application assembly: wires every component together on the GUI main thread.

This module is the single place where the object graph is built. Nothing else
in the client knows about the full wiring, which keeps the individual layers
unit-testable in isolation.

Hard constraints honoured here:

* **#1 Safe DB path** -- :class:`~kidtime_client.storage.database.LocalDatabase`
  runs ``assert_db_path_safe`` during construction; a failure aborts start-up
  with an actionable message instead of silently corrupting a synced file.
* **#2 Single-Writer** -- every object created below lives on the GUI main
  thread. The only exception is
  :class:`~kidtime_client.sync.sync_engine.SyncWorker`, which receives frozen
  dataclasses over ``Qt.QueuedConnection`` and performs HTTP only. The
  ``api_factory`` handed to it closes over an immutable
  :class:`~kidtime_client.security.dpapi.Credentials` snapshot, so it never
  touches SQLite from the worker thread.
* **#6 Secrets** -- the device secret is registered with the logging redaction
  filter as soon as it is known and is never written to the log.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Final

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QApplication, QMessageBox

from kidtime_client.config import (
    ClientConfig,
    env_bootstrap_credentials,
)
from kidtime_client.constants import (
    APP_DISPLAY_NAME,
    DEFAULT_PARENT_MODE_TIMEOUT_MINUTES,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    EventSeverity,
    EventType,
    LOCK_STYLE_EYECARE,
    OutboxKind,
    SETTING_AUTOSTART,
    SETTING_BASE_URL,
    SETTING_DEVICE_NAME,
    SETTING_LOCK_ALLOW_CHILD_SWITCH,
    SETTING_LOCK_STYLE,
    SETTING_SETUP_DONE,
    SETTING_TIMEZONE,
)
from kidtime_client.core.clock import Clock, SystemClock
from kidtime_client.core.credit import CreditService
from kidtime_client.core.engine import RuntimeEngine
from kidtime_client.core.gate_log import GateLog
from kidtime_client.core.idle_monitor import IdleSource, Win32IdleSource
from kidtime_client.core.lock_auto_shutdown import AutoShutdownController
from kidtime_client.core.startup_gate import StartupGateController
from kidtime_client.core.state_machine import State, StateMachine
from kidtime_client.core.time_guard import TimeGuard
from kidtime_client.core.usage_tracker import UsageTracker
from kidtime_client.logging_setup import register_secret
from kidtime_client.platform.autostart import is_autostart_enabled, set_autostart
from kidtime_client.platform.power import PowerController, Win32PowerController
from kidtime_client.platform.session_events import SessionEventListener
from kidtime_client.security.dpapi import Credentials, CredentialStore
from kidtime_client.storage.database import LocalDatabase, UnsafeDatabasePathError
from kidtime_client.storage.repositories import Repositories
from kidtime_client.sync.api_client import ApiClient
from kidtime_client.sync.command_handler import CommandHandler
from kidtime_client.sync.payloads import SyncResponse
from kidtime_client.sync.sync_engine import SyncEngine

from kidtime_client.ui import notifications
from kidtime_client.ui.floating_widget import FloatingWidget
from kidtime_client.ui.gate_manager import GateManager
from kidtime_client.ui.overlay_manager import OverlayManager
from kidtime_client.ui.parent_login_dialog import ParentLoginDialog
from kidtime_client.ui.parent_mode import ParentModeController
from kidtime_client.ui.parent_panel import ParentPanel
from kidtime_client.ui.message_boxes import confirm_on_top, warn_on_top
from kidtime_client.ui.reminder_popup import ReminderPopup
from kidtime_client.ui.shutdown_warning import ShutdownWarningWindow
from kidtime_client.ui.theme import APP_STYLESHEET
from kidtime_client.ui.tray import build_icon, TrayIcon
from kidtime_client.ui.window_policy import (
    apply_always_on_top,
    center_within_available_geometry,
)

logger = logging.getLogger(__name__)

#: States that should trigger the "locked" audio cue when entered.
_LOCK_STATES: Final[frozenset[State]] = frozenset(
    {State.LOCKED_CURFEW, State.LOCKED_QUOTA, State.BREAK}
)


class BootstrapError(RuntimeError):
    """Raised when the client cannot start (bad DB path, no credentials, ...)."""


class KidTimeApp(QObject):
    """Owns the whole client object graph.

    Typical use::

        qt_app = QApplication(sys.argv)
        app = KidTimeApp(ClientConfig.load(), qt_app)
        if not app.bootstrap():
            return 1
        return app.run()

    Args:
        config: Immutable runtime configuration.
        qt_app: The already-created :class:`QApplication`.
        clock: Injected clock (tests pass a ``FakeClock``).
        idle_source: Injected idle source (tests pass a ``FakeIdleSource``).
        power: Injected power controller (tests pass a ``FakePowerController``
            so the 1.2 startup gate's shutdown button never turns off CI).
    """

    def __init__(
        self,
        config: ClientConfig,
        qt_app: QApplication,
        clock: Clock | None = None,
        idle_source: IdleSource | None = None,
        power: PowerController | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._qt = qt_app
        self._clock: Clock = clock or SystemClock()
        self._idle: IdleSource = idle_source or Win32IdleSource()
        self._power: PowerController = power or Win32PowerController()

        self._db: LocalDatabase | None = None
        self._repos: Repositories | None = None
        self._store = CredentialStore(config.credentials_path)
        self._credentials: Credentials | None = None

        self._parent_mode: ParentModeController | None = None
        self._engine: RuntimeEngine | None = None
        self._usage: UsageTracker | None = None
        self._commands: CommandHandler | None = None
        self._sync: SyncEngine | None = None

        self._tray: TrayIcon | None = None
        self._floating: FloatingWidget | None = None
        self._overlays: OverlayManager | None = None
        self._panel: ParentPanel | None = None
        self._session: SessionEventListener | None = None

        # --- 1.2 启动门禁（Route A：门禁只活在编排层，状态机零改动） ---
        self._gate_ctrl: StartupGateController | None = None
        self._gate_ui: GateManager | None = None
        self._gate_log: GateLog | None = None
        #: 孩子会话是否已经开始（``engine.start()`` 是否执行过）。
        #: 与 ``_gate_ctrl.phase`` 冗余，但它在门禁被销毁后依然有效。
        self._child_session_started = False

        # --- 1.4.3 锁屏自动关机 + 倒计时提醒弹窗 ---------------------------
        #: 锁屏停留累计控制器（启动门禁与日常锁屏共享，需求 2b）。
        self._auto_shutdown: AutoShutdownController | None = None
        #: 30 秒关机预告窗口（需求 2b）。
        self._shutdown_warning: ShutdownWarningWindow | None = None
        #: 15/5/1 分钟倒计时置顶提醒弹窗（需求 1，单实例复用）。
        self._reminder_popup: ReminderPopup | None = None

        self._started = False
        self._quitting = False

    # ------------------------------------------------------------------
    # Public accessors (used by tests and by __main__)
    # ------------------------------------------------------------------
    @property
    def engine(self) -> RuntimeEngine | None:
        """The runtime engine, available after :meth:`bootstrap`."""
        return self._engine

    @property
    def repositories(self) -> Repositories | None:
        """The repository bundle, available after :meth:`bootstrap`."""
        return self._repos

    @property
    def credentials(self) -> Credentials | None:
        """The device credentials in use (secret never logged)."""
        return self._credentials

    @property
    def started(self) -> bool:
        """``True`` between a successful :meth:`bootstrap` and :meth:`shutdown`."""
        return self._started

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    def bootstrap(self, run_wizard: bool = True) -> bool:
        """Build the object graph and start the timers.

        单机形态：设备凭据由便携引导在启动前注入环境变量，无服务器配对向导。

        Args:
            run_wizard: 保留参数（兼容既有调用/测试签名）；单机形态下恒为 no-op。

        Returns:
            ``True`` when the client is ready to enter the Qt event loop.
        """
        self._qt.setApplicationName(APP_DISPLAY_NAME)
        self._qt.setApplicationVersion(self._config.client_version)
        self._qt.setQuitOnLastWindowClosed(False)
        self._qt.setStyleSheet(APP_STYLESHEET)
        try:
            self._qt.setWindowIcon(build_icon())
        except Exception:  # pragma: no cover - headless platforms
            logger.debug("Could not set the application icon", exc_info=True)

        try:
            self._open_database()
        except UnsafeDatabasePathError as exc:
            self._fatal("数据目录不安全", str(exc))
            return False
        except Exception as exc:  # pragma: no cover - disk/permission failures
            logger.exception("Opening the local database failed")
            self._fatal("无法打开本地数据库", str(exc))
            return False

        assert self._repos is not None  # narrowed for type checkers
        self._parent_mode = ParentModeController(
            settings_repo=self._repos.settings,
            runtime_repo=self._repos.runtime,
            events=self._repos.events,
            clock=self._clock,
            timeout_provider=self._parent_timeout_minutes,
        )

        credentials = self._resolve_credentials(run_wizard=run_wizard)
        if credentials is None:
            logger.warning("Start-up aborted: the device is not paired")
            return False
        self._credentials = credentials
        register_secret(credentials.device_secret)

        self._build_core()
        self._build_ui()
        self._connect_signals()
        # 1.2 Route A：服务立刻起（设备上线），但**不**启动引擎——
        # 计时要等孩子在门禁上点了「孩子使用」之后才开始。
        self._start_services()
        self._started = True
        logger.info(
            "%s %s ready (device=%s)",
            APP_DISPLAY_NAME,
            self._config.client_version,
            credentials.device_id,
        )
        # 首启最小向导：本地库无家长密码时在门禁前强制设置——无密码则家长模式
        # 验证恒失败（死锁）、托盘退出跳过验证（孩子可直接退）。拒绝设置即终止。
        if not self._parent_mode.has_password():
            from kidtime_client.ui.password_setup_dialog import run_password_setup

            password = run_password_setup(None)
            if password is None:
                self._fatal(
                    "尚未设置家长密码",
                    "首次使用必须设置家长密码，下次启动将再次提示。",
                )
                return False
            self._parent_mode.set_parent_password(password)
            logger.info("首启：家长密码已设置")
        self._show_startup_gate()
        return True

    def run(self) -> int:
        """Enter the Qt event loop.

        Returns:
            The process exit code.
        """
        self._qt.aboutToQuit.connect(self.shutdown)
        return int(self._qt.exec())

    @Slot()
    def shutdown(self) -> None:
        """Stop timers, flush state, and release every resource. Idempotent."""
        if not self._started:
            return
        self._started = False
        logger.info("Shutting down KidTime client")

        if self._session is not None:
            try:
                self._session.stop(self._qt)
            except Exception:  # pragma: no cover - defensive
                logger.debug("Stopping the session listener failed", exc_info=True)
        if self._sync is not None:
            try:
                self._sync.stop()
            except Exception:  # pragma: no cover - defensive
                logger.debug("Stopping the sync engine failed", exc_info=True)
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Stopping the runtime engine failed")
        self._close_startup_gate()
        if self._gate_log is not None:
            self._gate_log.close()
            self._gate_log = None
        if self._overlays is not None:
            self._overlays.shutdown()
        if self._panel is not None:
            self._panel.close()
            self._panel = None
        if self._floating is not None:
            self._floating.hide()
        if self._tray is not None:
            self._tray.hide()
        if self._db is not None:
            try:
                self._db.close_all()
            except Exception:  # pragma: no cover - defensive
                logger.debug("Closing the database failed", exc_info=True)

        logger.info("Shutdown complete")

    # ------------------------------------------------------------------
    # Bootstrap steps
    # ------------------------------------------------------------------
    def _open_database(self) -> None:
        """Open (and migrate) the local SQLite database."""
        self._db = LocalDatabase(self._config.db_path, allow_unsafe=self._config.allow_unsafe_db)
        self._db.initialize()
        self._repos = Repositories.create(self._db)
        logger.info("Local database ready at %s", self._config.db_path)

    def _resolve_credentials(self, run_wizard: bool) -> Credentials | None:
        """Load device credentials.

        单机形态下凭据必然由便携引导（``PortableBootstrapper._export_credentials``）
        在启动本应用前注入环境变量（env 分支命中）；encrypted store 是本地已配对
        数据的兜底。已无服务器配对向导（SetupWizard 已随去耦合删除）。

        Args:
            run_wizard: 保留参数（兼容既有调用/测试签名）；单机形态无向导可跑。

        Returns:
            The credentials, or ``None`` when the device is not paired.
        """
        assert self._repos is not None
        env = env_bootstrap_credentials()
        if env is not None:
            base_url, device_id, device_secret = env
            logger.info("Using bootstrap credentials from the environment")
            credentials = Credentials(
                base_url=base_url, device_id=device_id, device_secret=device_secret
            )
            self._repos.settings.set(SETTING_BASE_URL, base_url)
            self._repos.settings.set_bool(SETTING_SETUP_DONE, True)
            return credentials

        stored = self._store.load()
        setup_done = self._repos.settings.get_bool(SETTING_SETUP_DONE, False)
        if stored is not None and setup_done:
            return stored
        return None

    def _build_core(self) -> None:
        """Create the pure-logic components and the runtime engine."""
        assert self._db is not None and self._repos is not None
        assert self._parent_mode is not None

        repos = self._repos
        if not repos.settings.get(SETTING_TIMEZONE):
            repos.settings.set(SETTING_TIMEZONE, self._config.timezone)

        # 锁屏外观统一为米黄淡绿护眼样式（LOCK_STYLE_EYECARE）。
        # 🔴 必须在 RuntimeEngine 构造前写库：引擎构造时 normalize(None)→default
        # （蓝黑渐变），全新库不加这一段首启即蓝黑。
        repos.settings.set(SETTING_LOCK_STYLE, LOCK_STYLE_EYECARE)
        repos.settings.set_bool(SETTING_LOCK_ALLOW_CHILD_SWITCH, False)

        self._usage = UsageTracker(repos.usage, self._clock)
        credit = CreditService(
            db=self._db,
            ledger=repos.ledger,
            usage=repos.usage,
            clock=self._clock,
            events=repos.events,
            outbox=repos.outbox,
        )
        self._engine = RuntimeEngine(
            db=self._db,
            repos=repos,
            clock=self._clock,
            time_guard=TimeGuard(self._clock),
            machine=StateMachine(),
            usage=self._usage,
            credit=credit,
            idle_source=self._idle,
            parent_mode=self._parent_mode,
            settings=repos.settings,
        )
        self._commands = CommandHandler(
            settings=repos.settings,
            events=repos.events,
            outbox=repos.outbox,
            clock=self._clock,
            usage=self._usage,
            on_unlock=self._on_command_unlock,
            on_enforcement=self._on_command_enforcement,
            on_sync_now=self._on_command_sync_now,
            on_reset_usage=self._on_command_reset_usage,
            on_lock_style=self._on_command_lock_style,
        )
        self._sync = SyncEngine(
            api_factory=self._make_api_client,
            clock=self._clock,
            interval_provider=self._sync_interval_seconds,
        )

    def _build_ui(self) -> None:
        """Create the tray icon, floating badge, overlays, and OS hooks."""
        assert self._repos is not None
        self._tray = TrayIcon()
        self._floating = FloatingWidget(self._repos.settings)

        # 1.4.3：锁屏自动关机控制器（门禁与日常锁屏共享）+ 预告窗口 + 提醒弹窗。
        # 控制器先建，供两个管理器注入；回调接线在 _connect_signals 完成。
        self._auto_shutdown = AutoShutdownController()
        self._shutdown_warning = ShutdownWarningWindow()
        self._reminder_popup = ReminderPopup()
        self._overlays = OverlayManager(
            self._qt, auto_shutdown=self._auto_shutdown
        )
        self._session = SessionEventListener()

        # 1.2 启动门禁。🔴 门禁窗与倒计时锁屏**共用同一个** BrandBlurBackdrop：
        # 各建一个会让缓存翻倍，4K 场景直接超出 40MB 内存预算（架构 §D.5）。
        self._gate_ctrl = StartupGateController()
        # Q-A12：门禁本地日志与客户端日志同目录（config.log_dir =
        # default_data_dir()/logs，架构 §C.5 规定），只写本地文件。
        self._gate_log = GateLog(self._config.log_dir)
        self._gate_ui = GateManager(
            self._qt,
            self._gate_ctrl,
            backdrop=self._overlays.backdrop,
            gate_log=self._gate_log,
            auto_shutdown=self._auto_shutdown,
        )

    def _connect_signals(self) -> None:
        """Wire every signal. All slots run on the GUI main thread."""
        engine = self._engine
        sync = self._sync
        tray = self._tray
        floating = self._floating
        overlays = self._overlays
        parent_mode = self._parent_mode
        session = self._session
        assert engine and sync and tray and floating and overlays
        assert parent_mode and session

        # --- engine -> UI -------------------------------------------------
        engine.overlayRequested.connect(overlays.apply)
        engine.tickCompleted.connect(self._on_tick)
        engine.stateChanged.connect(self._on_state_changed)
        engine.reminderFired.connect(self._on_reminder)
        engine.lockStyleChanged.connect(self._on_lock_style_changed)
        engine.syncStatusChanged.connect(tray.update_sync_status)

        # --- sync <-> engine (queued: responses are applied on this thread) -
        sync.requestBuildNeeded.connect(self._on_build_request)
        sync.syncSucceeded.connect(self._on_sync_succeeded, Qt.QueuedConnection)
        sync.syncFailed.connect(self._on_sync_failed, Qt.QueuedConnection)

        # --- user entry points -------------------------------------------
        tray.parentPanelRequested.connect(self._on_parent_panel_requested)
        tray.toggleFloatingRequested.connect(self._on_toggle_floating)
        tray.quitRequested.connect(self._on_quit_requested)

        floating.doubleClicked.connect(self._on_parent_panel_requested)

        overlays.parentUnlockRequested.connect(self._on_parent_panel_requested)
        # 1.4.3（需求 2a）：日常锁屏（QUOTA/CURFEW）的关机按钮，与门禁共用确认流程。
        overlays.shutdownRequested.connect(self._on_shutdown_requested)

        # --- 1.4.3 锁屏自动关机（需求 2b）----------------------------------
        # 控制器回调由 GateManager/OverlayManager 的 1 秒心跳驱动；这里只把
        # 状态变化接到预告窗口与系统关机。
        auto_shutdown = self._auto_shutdown
        assert auto_shutdown is not None
        auto_shutdown.on_grace_started = self._on_auto_grace_started
        auto_shutdown.on_grace_tick = self._on_auto_grace_tick
        auto_shutdown.on_grace_cancelled = self._on_auto_grace_cancelled
        auto_shutdown.on_shutdown = self._on_auto_shutdown
        # 1.4.4（增量）：累计期每秒回调「距自动关机」剩余秒数，供锁屏倒计时。
        auto_shutdown.on_counting_tick = self._on_auto_counting_tick
        warning = self._shutdown_warning
        assert warning is not None
        warning.cancelRequested.connect(self._on_auto_shutdown_cancelled)

        # --- 1.2 启动门禁 ---------------------------------------------------
        gate_ui = self._gate_ui
        assert gate_ui is not None
        gate_ui.childChosen.connect(self._on_gate_child_chosen)
        gate_ui.parentChosen.connect(self._on_gate_parent_chosen)
        gate_ui.parentPanelRequested.connect(self._on_gate_parent_panel_requested)
        gate_ui.shutdownRequested.connect(self._on_gate_shutdown_requested)

        # --- parent mode ---------------------------------------------------
        parent_mode.entered.connect(self._on_parent_entered)
        parent_mode.exited.connect(self._on_parent_exited)
        parent_mode.emergencyGrace.connect(engine.grant_emergency_grace)

        # --- Windows session / power --------------------------------------
        session.sessionLocked.connect(self._on_session_locked)
        session.sessionUnlocked.connect(self._on_session_unlocked)
        session.systemSuspending.connect(self._on_system_suspending)
        session.systemResumed.connect(self._on_system_resumed)

        # 1.4.2：把引擎加载的持久化锁屏外观推送到各 UI（首屏即生效，FR-1）。
        self._push_initial_lock_style()

    def _start_services(self) -> None:
        """启动「与孩子是否开始使用无关」的一切（1.2 Route A 的前半段）。

        🔴 这里**刻意不调用** ``engine.start()``。1.1 把「设备在线」和「孩子
        开始计时」捆在一起，导致开机后没人用的那段时间也算在孩子头上。1.2 把
        两件事拆开：

        * :meth:`_start_services` —— 同步、托盘、悬浮窗、会话监听。设备一启动
          就上线，家长在远端能看到心跳、能下发指令。
        * :meth:`_start_child_session` —— **只有孩子入口被点击后**才执行
          ``engine.start()``，计时从那一刻才开始。

        ``sync.start()`` 必须先于门禁显示：门禁可能停留很久，这期间设备得是
        在线的，否则家长会以为电脑没开机。
        """
        assert self._engine and self._sync and self._tray and self._floating
        assert self._session is not None

        self._sync.start()
        self._tray.show()
        self._floating.restore_visibility()
        self._session.start(self._qt)

        # Keep the registry entry in step with the stored preference (the user
        # may have removed it manually, or the executable may have moved).
        assert self._repos is not None
        wanted = self._repos.settings.get_bool(SETTING_AUTOSTART, False)
        if wanted != is_autostart_enabled():
            set_autostart(wanted)

    def _start_child_session(self) -> None:
        """孩子入口 或 家长解锁共用的 ``engine.start()`` 幂等守卫。

        🔴 这是整个 1.2 里唯一调用 ``engine.start()`` 的地方；1.3（PRD 硬约束
        #7）扩展为「孩子入口 | 家长解锁」共用。幂等——重复点击或多屏上各点一次
        都只会启动一次引擎。

        ``keep_parent_mode`` 由 ``parent_mode.is_active`` 自动推导：家长解锁
        路径下家长会话已激活 → 传 ``True`` 保留家长模式（状态机落 PARENT，
        不扣孩子时间）；孩子路径下未激活 → 传 ``False``（默认行为不变）。
        """
        if self._child_session_started:
            return
        assert self._engine is not None
        self._child_session_started = True
        keep_parent = bool(self._parent_mode is not None and self._parent_mode.is_active)
        self._engine.start(keep_parent_mode=keep_parent)
        logger.info("运行时会话开始（keep_parent_mode=%s）", keep_parent)

    def _show_startup_gate(self) -> None:
        """显示双入口门禁锁屏。

        决策 2：**每次启动都弹**——手动启动、开机自启、崩溃重启一视同仁。
        没有「上次选过就记住」这种逻辑，因为那等于把绕过门禁的方法教给孩子。
        """
        assert self._gate_ui is not None
        self._gate_ui.show()

    def _close_startup_gate(self) -> None:
        """关闭并拆除门禁（孩子入口被选中后）。"""
        if self._gate_ui is None:
            return
        self._gate_ui.shutdown()
        self._gate_ui = None
        logger.info("启动门禁已拆除")

    # ------------------------------------------------------------------
    # Providers / factories
    # ------------------------------------------------------------------
    def _parent_timeout_minutes(self) -> int:
        """Return the parent-mode idle timeout in minutes."""
        try:
            if self._engine is not None:
                return int(self._engine.rules.parent_mode_timeout_minutes)
            if self._repos is not None:
                return int(self._repos.settings.get_rules().parent_mode_timeout_minutes)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Falling back to the default parent-mode timeout", exc_info=True)
        return DEFAULT_PARENT_MODE_TIMEOUT_MINUTES

    def _sync_interval_seconds(self) -> int:
        """Return the desired sync interval in seconds."""
        try:
            if self._engine is not None:
                return int(self._engine.rules.sync_interval_seconds)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Falling back to the default sync interval", exc_info=True)
        return DEFAULT_SYNC_INTERVAL_SECONDS

    def _make_api_client(self) -> ApiClient | None:
        """Create an :class:`ApiClient` for the worker thread.

        Only the immutable credential snapshot is read here, so this is safe to
        call from ``SyncWorker`` (hard constraint #2: no DB access off-thread).

        单机形态：内嵌后端固定监听 ``http://127.0.0.1`` 明文，无 HTTPS 证书
        概念——``verify_tls=False`` 恒定，不做任何证书校验。

        Returns:
            A ready client, or ``None`` when the device is not paired.
        """
        credentials = self._credentials
        if credentials is None:
            return None
        return ApiClient(
            base_url=credentials.base_url,
            device_id=credentials.device_id,
            device_secret=credentials.device_secret,
            verify_tls=False,
            client_version=self._config.client_version,
        )

    # ------------------------------------------------------------------
    # Engine slots
    # ------------------------------------------------------------------
    @Slot(object)
    def _on_tick(self, output: object) -> None:
        """Push the tick result into the tray, badge, and parent panel."""
        if self._tray is not None:
            self._tray.update_from_tick(output)
        if self._floating is not None and self._floating.isVisible():
            self._floating.update_from_tick(output)

    @Slot(object, object, str)
    def _on_state_changed(self, previous: object, current: object, reason: str) -> None:
        """Play an audio cue and log the transition.

        Args:
            previous: The previous :class:`State`.
            current: The new :class:`State`.
            reason: Human-readable transition reason.
        """
        logger.info(
            "State %s -> %s (%s)",
            getattr(previous, "value", previous),
            getattr(current, "value", current),
            reason,
        )
        entering_lock = current in _LOCK_STATES
        leaving_lock = previous in _LOCK_STATES
        if entering_lock and not leaving_lock:
            notifications.play_lock()
        elif leaving_lock and not entering_lock:
            notifications.play_unlock()
        # v1.4（PRD P0-1 / 用户复核）：家长面板一律不置顶，与锁屏状态无关。
        # 锁屏窗口自身的置顶由 GateManager/OverlayManager 独立管理。若面板在
        # 锁屏期间仍开着，保持普通层级即可（锁屏窗口会覆盖在它之上）。
        if self._panel is not None and self._panel.isVisible():
            apply_always_on_top(self._panel, False)

    @Slot(int, float)
    def _on_reminder(self, point_minutes: int, remaining_minutes: float) -> None:
        """15/5/1 分钟提醒：置顶弹窗 + 托盘气泡双通道 + 提示音。

        1.4.3（需求 1）：15/5/1 三个提醒点全部升级为「置顶弹窗 + 托盘气泡」
        双通道。弹窗置顶并抢焦点、可点「知道了」关闭、5 秒自动关闭
        （``ReminderPopup`` 内部实现）。
        """
        title, body = notifications.reminder_message(point_minutes, remaining_minutes)
        notifications.play_reminder(point_minutes)
        if self._reminder_popup is not None:
            self._reminder_popup.show_reminder(title, body)
        if self._tray is not None:
            self._tray.notify(title, body, warning=point_minutes <= 5)

    # ------------------------------------------------------------------
    # Sync slots
    # ------------------------------------------------------------------
    @Slot()
    def _on_build_request(self) -> None:
        """Build a :class:`SyncRequest` on the main thread and submit it."""
        if self._engine is None or self._sync is None:
            return
        try:
            request = self._engine.build_sync_request()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Building the sync request failed")
            self._engine.on_sync_failed(f"组装同步请求失败：{exc}")
            self._sync.submit(None)  # type: ignore[arg-type]
            return
        self._sync.submit(request)  # type: ignore[arg-type]

    @Slot(object)
    def _on_sync_succeeded(self, response: object) -> None:
        """Apply a sync response, then execute the delivered commands.

        Order matters: the engine writes rules/credits/bookkeeping first so the
        command handler observes a consistent database.

        Args:
            response: The :class:`SyncResponse` from the worker thread.
        """
        if self._engine is None or not isinstance(response, SyncResponse):
            return
        self._engine.on_sync_response(response)
        if self._commands is None or not response.commands:
            return
        try:
            outcomes = self._commands.handle_all(list(response.commands))
        except Exception:  # pragma: no cover - defensive
            logger.exception("Executing delivered commands failed")
            return
        if any(outcome.trigger_sync for outcome in outcomes) and self._sync is not None:
            self._sync.trigger_now()

    @Slot(str)
    def _on_sync_failed(self, message: str) -> None:
        """Record the failure through the engine (single writer)."""
        if self._engine is not None:
            self._engine.on_sync_failed(message)

    # ------------------------------------------------------------------
    # Command handler callbacks
    # ------------------------------------------------------------------
    def _on_command_unlock(self, deadline: datetime) -> None:
        """Apply an ``UNLOCK_TEMP`` deadline (or clear it on resume).

        Args:
            deadline: The unlock deadline computed by the command handler. A
                deadline in the past means "cancel the current unlock".
        """
        if self._engine is None:
            return
        remaining = (deadline - self._clock.now_utc()).total_seconds()
        if remaining <= 0:
            self._engine.clear_grace()
            return
        minutes = max(1, int(round(remaining / 60.0)))
        self._engine.apply_unlock_temp("command", minutes)
        if self._tray is not None:
            self._tray.notify("家长已临时解锁", f"接下来 {minutes} 分钟可以正常使用。")

    def _on_command_enforcement(self, enabled: bool) -> None:
        """Apply a ``PAUSE_ENFORCEMENT`` / ``RESUME_ENFORCEMENT`` command."""
        if self._engine is not None:
            self._engine.apply_enforcement(enabled)
        if self._tray is not None:
            self._tray.notify(
                "管控已恢复" if enabled else "管控已暂停",
                "本机重新按规则计时。" if enabled else "本机暂时不再限制上机时间。",
                warning=not enabled,
            )

    def _on_command_sync_now(self) -> None:
        """Handle a ``SYNC_NOW`` command."""
        if self._sync is not None:
            self._sync.trigger_now()

    def _on_command_reset_usage(self) -> None:
        """Handle a ``RESET_USAGE`` command: push the ack to the server promptly."""
        if self._sync is not None:
            self._sync.trigger_now()

    def _on_command_lock_style(self, style: str, allow_child_switch: bool) -> None:
        """Handle a ``SET_LOCK_STYLE`` command: apply via the engine.

        The engine persists and broadcasts ``lockStyleChanged``, which the signal
        wiring turns into UI updates -- so we only need to forward here.
        """
        if self._engine is not None:
            self._engine.apply_lock_style(style, allow_child_switch)

    @Slot(str, bool)
    def _on_lock_style_changed(self, style: str, allow_child_switch: bool) -> None:
        """引擎广播：同步共享毛玻璃底座 + 透传各锁屏窗口 + 浮窗。

        单机形态：锁屏外观恒定米黄淡绿（eyecare），此回调仅把引擎侧样式
        推送到需要按样式渲染的 UI 组件（浮窗经 ``set_style`` 重绘）。
        """
        self._push_lock_style(style)

    def _push_lock_style(self, style: str) -> None:
        """把当前锁屏外观推送到所有需要按样式渲染的 UI 组件。"""
        if self._overlays is not None:
            self._overlays.set_style(style)
        if self._gate_ui is not None:
            self._gate_ui.set_style(style)
        if self._floating is not None:
            self._floating.set_style(style)

    def _push_initial_lock_style(self) -> None:
        """启动后把引擎加载的持久化样式推送到 UI（首屏即生效）。"""
        if self._engine is None:
            return
        self._push_lock_style(self._engine.lock_style)

    # ------------------------------------------------------------------
    # User interactions
    # ------------------------------------------------------------------
    @Slot()
    def _pending_extension_count(self) -> int:
        """Count today's still-unsent extension requests."""
        if self._repos is None or self._engine is None:
            return 0
        try:
            rows = self._repos.outbox.due_by_kind(
                OutboxKind.EXTENSION_REQUEST.value, self._clock.now_utc(), limit=50
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("Counting pending extension requests failed", exc_info=True)
            return 0
        today = self._engine.effective_date.isoformat()
        return sum(1 for row in rows if row.payload.get("target_date") == today)

    # ------------------------------------------------------------------
    # 1.2 启动门禁槽函数
    # ------------------------------------------------------------------
    @Slot()
    def _on_gate_child_chosen(self) -> None:
        """孩子入口被点击：拆掉门禁并开始计时。"""
        if self._gate_ctrl is None:
            return
        if not self._gate_ctrl.choose_child():
            return
        self._close_startup_gate()
        self._start_child_session()

    @Slot()
    def _on_gate_parent_chosen(self) -> None:
        """家长入口被点击：验证密码或恢复码。

        🔴 决策 3（v1.2）：验证通过后**只**切到家长待机态（PARENT_STANDBY）。
        1.3（PRD P0-1）改为「家长解锁直达桌面」：验证通过 → 门禁全部关闭 →
        ``engine.start(keep_parent_mode=True)`` → 状态机落 PARENT 态（不扣
        孩子时间、不弹锁屏），桌面可直接使用，无面板自动弹出。

        🔴 恢复码复用直接走 :meth:`ParentLoginDialog.authenticate`，不在这里
        另写一套校验：密码/恢复码的比对、5 次冷却、10 次紧急放行全部由
        :class:`ParentModeController` 统一负责，重复实现必然走样。
        """
        if self._parent_mode is None or self._gate_ctrl is None:
            return
        if not self._parent_mode.has_password():
            QMessageBox.information(
                None,
                "尚未设置家长密码",
                "还没有设置家长密码，请先在首次设置向导里完成设置。",
            )
            return
        if not ParentLoginDialog.authenticate(
            self._parent_mode,
            locked=self._lock_active(),
            on_failure=self._on_gate_verify_failed,
        ):
            return
        # 先认证、后关门禁：认证取消/失败时门禁保持 PENDING，绝不放行。
        if not self._gate_ctrl.unlock_as_parent():
            return  # 幂等：重复点击或多屏点击只生效一次
        self._close_startup_gate()  # 多屏全部销毁（GateManager.shutdown()），无残留
        self._start_child_session()  # engine.start(keep_parent_mode=True)
        # 托盘家长模式提示已由 parent_mode.entered → _on_parent_entered 完成

    @Slot()
    def _on_gate_parent_panel_requested(self) -> None:
        """家长待机态下请求打开家长面板（家长已经验证过了）。"""
        self._show_parent_panel()

    def _on_gate_verify_failed(self, method: str, fail_count: int) -> None:
        """Q-A12 ③④：门禁家长验证失败记录。

        只记方式与累计次数；``ParentLoginDialog`` 已经保证不会把秘密内容
        传到这里。阈值触发（5 次冷却 / 10 次应急放行）在失败计数跨线的那
        一刻补记一条 lockout 记录。

        Args:
            method: ``"password"`` 或 ``"recovery_code"``。
            fail_count: 控制器里累计的连续失败次数。
        """
        gate_log = self._gate_log if self._gate_ui is not None else None
        if gate_log is None:
            return
        gate_log.log_verify_failed(method)
        if fail_count == ParentModeController.COOLDOWN_THRESHOLD:
            gate_log.log_lockout("cooldown", fail_count)
        elif fail_count == ParentModeController.EMERGENCY_THRESHOLD:
            gate_log.log_lockout("emergency_grace", fail_count)

    @Slot()
    def _on_gate_shutdown_requested(self) -> None:
        """门禁上的「关机」按钮被点击 → 走通用确认流程。"""
        self._on_shutdown_requested()

    @Slot()
    def _on_shutdown_requested(self) -> None:
        """锁屏（启动门禁 / 日常锁屏）上的「关机」按钮：确认后关机。

        1.4.3（需求 2a）：日常锁屏（QUOTA/CURFEW）新增关机按钮，与门禁共用
        同一确认流程。确认对话框保留——关机是不可逆的破坏性操作，真想关机的
        人不介意多点一下，误触的孩子则被这一步救回来。

        🔴 1.4.3 修复：确认框必须走 ``confirm_on_top`` —— 锁屏窗口带
        ``BypassWindowManagerHint`` 永远置顶，普通 ``QMessageBox`` 会被锁屏
        盖住（实测 bug：点了关机按钮却看不到确认框）。失败告警同理。
        """
        if not confirm_on_top(
            "关闭计算机",
            "确定现在关闭这台电脑吗？请先保存正在编辑的内容。",
        ):
            return
        if not self._power.shutdown():
            warn_on_top("无法关机", "系统拒绝了关机请求，请手动关闭计算机。")

    # ------------------------------------------------------------------
    # 1.4.3 锁屏自动关机（需求 2b）回调
    # ------------------------------------------------------------------
    def _push_shutdown_countdown(self, remaining_seconds: int) -> None:
        """把「距自动关机」倒计时推送到所有在岗锁屏（门禁 + 日常锁屏）。

        两个管理器时序互斥，同一时刻只有一个持有窗口；对没有窗口的那一侧
        调用是空转，不会产生副作用（1.4.4 增量）。
        """
        if self._gate_ui is not None:
            self._gate_ui.set_shutdown_countdown(int(remaining_seconds))
        if self._overlays is not None:
            self._overlays.set_shutdown_countdown(int(remaining_seconds))

    def _clear_shutdown_countdown(self) -> None:
        """收起所有锁屏上的关机倒计时（1.4.4 增量）。"""
        if self._gate_ui is not None:
            self._gate_ui.clear_shutdown_countdown()
        if self._overlays is not None:
            self._overlays.clear_shutdown_countdown()

    @Slot(int)
    def _on_auto_grace_started(self, seconds: int) -> None:
        """进入 30 秒关机预告：显示预告窗口 + 锁屏卡片倒计时。"""
        if self._shutdown_warning is not None:
            self._shutdown_warning.show_warning(int(seconds))
        # 1.4.4（增量）：预告期开始时锁屏卡片同步进入红色倒计时。
        self._push_shutdown_countdown(int(seconds))

    @Slot(int)
    def _on_auto_grace_tick(self, remaining_seconds: int) -> None:
        """预告期每秒刷新倒计时数字（弹窗 + 锁屏卡片）。"""
        if self._shutdown_warning is not None:
            self._shutdown_warning.update_remaining(int(remaining_seconds))
        self._push_shutdown_countdown(int(remaining_seconds))

    @Slot(int)
    def _on_auto_counting_tick(self, remaining_seconds: int) -> None:
        """累计期每秒推送「距自动关机」剩余秒数到锁屏卡片（1.4.4 增量）。

        预告期（GRACE）由 :meth:`_on_auto_grace_tick` 负责；累计期
        （COUNTING）在这里刷新，两条曲线无缝衔接成一条连续倒计时。
        """
        self._push_shutdown_countdown(int(remaining_seconds))

    @Slot()
    def _on_auto_grace_cancelled(self) -> None:
        """预告被取消（用户点「取消关机」或锁屏提前消失）：收起窗口与倒计时。"""
        if self._shutdown_warning is not None:
            self._shutdown_warning.dismiss()
        self._clear_shutdown_countdown()

    @Slot()
    def _on_auto_shutdown_cancelled(self) -> None:
        """预告窗口的「取消关机」按钮：交给控制器重新累计。"""
        if self._auto_shutdown is not None:
            self._auto_shutdown.cancel_grace()

    @Slot()
    def _on_auto_shutdown(self) -> None:
        """预告归零：执行关机（30 秒预告已给足取消机会，不再二次确认）。"""
        if self._shutdown_warning is not None:
            self._shutdown_warning.dismiss()
        self._clear_shutdown_countdown()
        if not self._power.shutdown():
            if self._tray is not None:
                self._tray.notify(
                    "无法关机",
                    "系统拒绝了关机请求，请手动关闭计算机。",
                    warning=True,
                )

    @Slot()
    def _on_parent_panel_requested(self) -> None:
        """Authenticate (when needed) and open the parent panel."""
        if self._parent_mode is None or self._engine is None or self._repos is None:
            return
        if not self._parent_mode.is_active:
            # 🔴 1.2 缺陷修复：``has_password`` 是**方法**不是 property，
            # 1.1 漏写括号导致这里永远拿到一个真值的 bound method，
            # 「尚未设置家长密码」的提示从来没有机会出现。
            # 修复走调用侧加括号，不给 parent_mode 加 @property——
            # 其他调用方已经按方法在用，加 property 会引发连锁回归。
            if not self._parent_mode.has_password():
                # 无密码兜底：就地调起首启设置（正常路径在门禁前已强制设置，
                # 此处只防御异常态进入）。
                from kidtime_client.ui.password_setup_dialog import (
                    run_password_setup,
                )

                password = run_password_setup(None)
                if password is None:
                    return
                self._parent_mode.set_parent_password(password)
            if not self._parent_mode.is_active:
                if not ParentLoginDialog.authenticate(
                    self._parent_mode, locked=self._lock_active()
                ):
                    return
        # 🔴 必须在方法体顶层调用：家长模式已激活（门禁解锁后 30 分钟会话内）
        # 时上面整块校验都会跳过，缩进进 if 内会导致面板永远打不开
        # （真机第十轮回归，第九轮修复时误把本行缩进进去了）。
        self._show_parent_panel()

    def _lock_active(self) -> bool:
        """v1.4（PRD P0-1）：「锁屏是否可见」的唯一定义。

        启动门禁或额度/宵禁/休息锁屏任一可见，家长窗口就需要置顶，否则保持
        普通层级。门禁（``GateManager.visible``）与锁屏（``OverlayManager.
        visible``）都是现成状态，不新造布尔量。
        """
        gate = self._gate_ui
        overlays = self._overlays
        return (gate is not None and gate.visible) or (
            overlays is not None and overlays.visible
        )

    def _show_parent_panel(self) -> None:
        """Create (once) and raise the parent panel."""
        assert self._engine is not None and self._repos is not None
        assert self._parent_mode is not None
        if self._panel is None:
            device_id = self._credentials.device_id if self._credentials else ""
            self._panel = ParentPanel(
                engine=self._engine,
                repos=self._repos,
                parent_mode=self._parent_mode,
                device_id=device_id,
                data_dir=self._config.data_dir,
            )
            self._panel.floatingToggled.connect(self._on_floating_toggled)
            self._panel.autostartToggled.connect(self._on_autostart_toggled)
            self._panel.quitRequested.connect(self._quit_now)
            self._panel.finished.connect(self._on_panel_finished)
        # v1.4（PRD P0-1 / 用户复核）：家长面板**无论通过何种途径进入，一律
        # 取消置顶**——「家长模式取消置顶」是全局原则，与是否经由锁屏无关。
        # 早期实现曾用 ``locked=self._lock_active()`` 在锁屏解锁瞬间误判为置顶、
        # 且不再重算，导致从锁屏解锁后的家长面板仍卡在最上层。锁屏窗口本身
        # （GateManager/OverlayManager）的置顶由各自窗口独立管理，与面板无关；
        # 登录/重绑对话框在锁屏期间弹出仍保留置顶（见下方构造的 locked 参数）。
        apply_always_on_top(self._panel, False)
        self._panel.refresh()
        # 1.4.2 BugFix：在 show() 之前先把面板夹进 availableGeometry()（不含
        # 任务栏）的可见区。此时窗口尚未显示，可基于 sizeHint() 准确计算整窗
        # 尺寸并用 setGeometry 原子定位，避免先闪一下 WM 默认位置（常落在任务栏
        # 之下）再跳动的可见跳变，也规避 Windows 上 show() 后布局未就绪导致按
        # 错误尺寸计算中心的计时问题。
        self._ensure_panel_visible(self._panel)
        self._panel.show()
        self._panel.raise_()
        self._panel.activateWindow()

    def _ensure_panel_visible(self, panel: QWidget) -> None:
        """把家长面板放进「扣除任务栏后」的可见屏幕区域，避免底部被桌面遮挡。

        1.4.2 BugFix：面板由 OS 默认放置时底部可能落在任务栏之下（尤其小屏 /
        高 DPI 缩放），导致面板底部被桌面遮挡。实际夹位逻辑在
        :func:`~kidtime_client.ui.window_policy.center_within_available_geometry`
        （用 ``availableGeometry()`` 即不含任务栏的可见区）。
        """
        center_within_available_geometry(panel)

    @Slot(int)
    def _on_panel_finished(self, _result: int) -> None:
        """Drop the panel reference so it is rebuilt with fresh data."""
        self._panel = None

    @Slot()
    def _on_toggle_floating(self) -> None:
        """Toggle the floating badge from the tray menu."""
        if self._floating is None:
            return
        self._floating.set_visible_persisted(not self._floating.isVisible())

    @Slot(bool)
    def _on_floating_toggled(self, visible: bool) -> None:
        """Apply the floating-badge preference from the parent panel."""
        if self._floating is not None:
            self._floating.setVisible(bool(visible))

    @Slot(bool)
    def _on_autostart_toggled(self, enabled: bool) -> None:
        """Apply the autostart preference from the parent panel."""
        ok = set_autostart(bool(enabled))
        if not ok and enabled:
            QMessageBox.warning(
                None, "无法设置开机自启", "写入注册表失败，请以当前用户身份重试。"
            )

    @Slot()
    def _on_quit_requested(self) -> None:
        """Quitting is a parent action: require authentication first."""
        if self._parent_mode is None:
            self._quit_now()
            return
        if not self._parent_mode.is_active:
            # 🔴 1.2 缺陷修复：同 ``_on_parent_panel_requested``，补上调用括号。
            # 修复前这里恒为真值，效果上"碰巧正确"（总是要求验证），但那是
            # 巧合而非设计；未设密码时会弹出一个永远验不过的对话框。
            if not self._parent_mode.has_password():
                # 无密码不能免验证退出（安全空洞）；正常路径已在门禁前强制设置，
                # 此处仅防御异常态。
                QMessageBox.warning(
                    None,
                    "无法退出",
                    "尚未设置家长密码，请先通过托盘「家长模式…」完成设置。",
                )
                return
            if not ParentLoginDialog.authenticate(self._parent_mode):
                return
        confirm = QMessageBox.question(
            None,
            f"退出 {APP_DISPLAY_NAME}",
            "退出后本机将不再统计和限制上机时间，确定退出吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self._quit_now()

    @Slot()
    def _quit_now(self) -> None:
        """Record the manual exit and leave the event loop."""
        if self._quitting:
            return
        self._quitting = True
        if self._repos is not None:
            try:
                self._repos.events.append(
                    EventType.PARENT_MODE_EXIT.value,
                    EventSeverity.WARNING.value,
                    {"reason": "app_quit"},
                    self._clock.now_utc(),
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug("Could not record the quit event", exc_info=True)
        self.shutdown()
        self._qt.quit()

    # ------------------------------------------------------------------
    # Parent mode / OS notifications
    # ------------------------------------------------------------------
    @Slot()
    def _on_parent_entered(self) -> None:
        """Reflect parent mode in the tray.

        进入家长模式不弹系统通知气泡（避免桌面提示干扰），只保留悬浮窗倒计时。
        """
        if self._tray is not None:
            self._tray.set_parent_mode(True)

    @Slot(str)
    def _on_parent_exited(self, reason: str) -> None:
        """Close the panel and reset the tray when parent mode ends.

        🔴 1.3（PRD P0-2）：家长模式退出（手动「切换到孩子模式」/ 30 分钟超时 /
        会话锁定 / 日切换）**自然流转**，绝不重弹门禁。家长解锁后门禁已关闭
        （phase=CHILD_STARTED），下方 ``back_to_pending()`` 恒返回 ``False``
        = no-op；引擎保持运行，状态机按 ``parent_mode_active=False`` 自然判定
        （允许时段且配额未耗尽 → ACTIVE 开始计孩子时间；已到宵禁/配额耗尽 →
        立即弹对应锁屏，无空窗）。
        """
        if self._tray is not None:
            self._tray.set_parent_mode(False)
        if self._panel is not None and reason != "manual":
            self._panel.close()
            self._panel = None
        # 家长模式结束时如果门禁还开着（1.2 待机路径：家长验证过但没让孩子
        # 开始使用），退回双入口首屏——否则会卡在一块写着「已进入家长模式」
        # 却其实已经退出了的锁屏上。1.3 主路径下门禁已关，此处恒为 no-op。
        if self._gate_ctrl is not None and self._gate_ctrl.back_to_pending():
            if self._gate_ui is not None:
                self._gate_ui.refresh()

    @Slot()
    def _on_session_locked(self) -> None:
        """Windows locked the session."""
        if self._engine is not None:
            self._engine.set_session_locked(True)

    @Slot()
    def _on_session_unlocked(self) -> None:
        """Windows unlocked the session."""
        if self._engine is not None:
            self._engine.set_session_locked(False)

    @Slot()
    def _on_system_suspending(self) -> None:
        """The machine is going to sleep: flush usage so nothing is lost."""
        if self._usage is not None:
            try:
                self._usage.maybe_flush(force=True)
            except Exception:  # pragma: no cover - defensive
                logger.debug("Flushing usage before suspend failed", exc_info=True)

    @Slot()
    def _on_system_resumed(self) -> None:
        """The machine woke up: treat it as an unlocked, freshly read clock."""
        if self._engine is not None:
            self._engine.set_session_locked(False)
        if self._sync is not None:
            self._sync.trigger_now()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _fatal(title: str, message: str) -> None:
        """Show a blocking error dialog (falls back to the log when headless).

        Args:
            title: Dialog title.
            message: Detail text.
        """
        logger.error("%s: %s", title, message)
        try:
            QMessageBox.critical(None, title, message)
        except Exception:  # pragma: no cover - headless
            logger.debug("Could not display the error dialog", exc_info=True)


def build_app(
    config: ClientConfig | None = None,
    qt_app: QApplication | None = None,
    clock: Clock | None = None,
    idle_source: IdleSource | None = None,
    power: PowerController | None = None,
) -> KidTimeApp:
    """Convenience factory used by ``__main__`` and by tests.

    Args:
        config: Configuration (defaults to :meth:`ClientConfig.load`).
        qt_app: An existing ``QApplication`` (defaults to the running instance).
        clock: Optional injected clock.
        idle_source: Optional injected idle source.
        power: Optional injected power controller (tests pass a fake so the
            startup gate's shutdown button cannot turn off the machine).

    Returns:
        A non-bootstrapped :class:`KidTimeApp`.

    Raises:
        BootstrapError: When no ``QApplication`` exists yet.
    """
    instance = qt_app or QApplication.instance()
    if instance is None:
        raise BootstrapError("必须先创建 QApplication 才能构建 KidTimeApp")
    return KidTimeApp(
        config or ClientConfig.load(),
        instance,  # type: ignore[arg-type]
        clock=clock,
        idle_source=idle_source,
        power=power,
    )


__all__ = ["BootstrapError", "KidTimeApp", "build_app"]
