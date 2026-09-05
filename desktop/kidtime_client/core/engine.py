"""Runtime orchestration: the 1-second tick loop and sync application.

This is the only module in :mod:`kidtime_client.core` that imports Qt.

Hard constraints enforced here:

* **#2 Single-Writer** -- every database write below happens on the GUI main
  thread. :meth:`RuntimeEngine.on_sync_response` is a ``Slot`` connected with
  ``Qt.QueuedConnection`` so responses produced by the worker thread are
  applied here, never there.
* **#4 Rollback detection** -- :class:`~kidtime_client.core.time_guard.TimeGuard`
  supplies a clamped monotonic delta plus a never-decreasing effective date.
* **#5 Credit idempotency** -- crediting is delegated to
  :class:`~kidtime_client.core.credit.CreditService`; the engine only re-sends
  confirmations, it never re-adds minutes.
* **G1/D50** -- :meth:`_on_tick` wraps everything in ``try/except`` so a bug can
  never stop the QTimer and leave the child permanently locked or unlocked.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from kidtime_client.constants import (
    CLIENT_VERSION,
    DEFAULT_TIMEZONE,
    EXTENSION_MAX_MINUTES,
    EXTENSION_MIN_MINUTES,
    EventSeverity,
    EventType,
    LOCK_STYLE_DEFAULT,
    LOCK_STYLE_ORDER,
    MAX_PENDING_EXTENSIONS_PER_DAY,
    OutboxKind,
    SETTING_ENFORCEMENT_PAUSED,
    SETTING_LAST_DAILY_EVENT_DATE,
    SETTING_LOCK_ALLOW_CHILD_SWITCH,
    SETTING_LOCK_STYLE,
    SETTING_TIMEZONE,
    SETTING_UNLOCK_UNTIL,
    SYNC_MAX_COMMAND_ACK_ITEMS,
    SYNC_MAX_CREDIT_CONFIRM_ITEMS,
    SYNC_MAX_EVENT_ITEMS,
    SYNC_MAX_EXTENSION_ITEMS,
    SYNC_MAX_USAGE_ITEMS,
    normalize_lock_style,
)
from kidtime_client.core.clock import Clock
from kidtime_client.core.credit import CreditService
from kidtime_client.core.idle_monitor import IdleSource
from kidtime_client.core.rules import RuleSnapshot, base_quota_for
from kidtime_client.core.state_machine import (
    OVERLAY_BREAK,
    OVERLAY_TAMPER,
    State,
    StateMachine,
    TickInput,
    TickOutput,
)
from kidtime_client.core.time_guard import TimeGuard
from kidtime_client.core.usage_tracker import UsageTracker
from kidtime_client.storage.database import LocalDatabase
from kidtime_client.storage.repositories import Repositories
from kidtime_client.sync.payloads import (
    CommandAck,
    EventUploadItem,
    ExtensionUploadItem,
    SyncRequest,
    SyncResponse,
    UsageUploadItem,
)

logger = logging.getLogger(__name__)

#: Tick period in milliseconds. Fixed at 1s by the design.
TICK_INTERVAL_MS: Final[int] = 1000

#: How long uploaded events / sent outbox rows are retained before purging.
RETENTION_DAYS: Final[int] = 7


class RuntimeEngine(QObject):
    """Drives the state machine, persists state, and applies sync responses.

    Args:
        db: Local database handle.
        repos: Aggregated repositories.
        clock: Injected clock.
        time_guard: Monotonic/wall-clock guard.
        machine: The pure state machine.
        usage: Daily usage tracker.
        credit: Credit service (idempotent minute crediting).
        idle_source: Keyboard/mouse idle source.
        parent_mode: Parent-mode controller.
        settings: Settings repository (also reachable via ``repos``; kept as an
            explicit argument to match the architecture signature).
    """

    stateChanged = Signal(object, object, str)  # (from: State, to: State, reason)
    tickCompleted = Signal(object)  # TickOutput
    overlayRequested = Signal(object)  # OverlayContext-like dict | None
    reminderFired = Signal(int, float)  # (point_minutes, remaining_minutes)
    dayRolled = Signal(object)  # date
    syncStatusChanged = Signal(bool, str)  # (ok, message)
    lockStyleChanged = Signal(str, bool)  # (style, allow_child_switch)

    def __init__(
        self,
        *,
        db: LocalDatabase,
        repos: Repositories,
        clock: Clock,
        time_guard: TimeGuard,
        machine: StateMachine,
        usage: UsageTracker,
        credit: CreditService,
        idle_source: IdleSource,
        parent_mode: Any,
        settings: Any,
    ) -> None:
        super().__init__()
        self._db = db
        self._repos = repos
        self._clock = clock
        self._guard = time_guard
        self._machine = machine
        self._usage = usage
        self._credit = credit
        self._idle = idle_source
        self._parent = parent_mode
        self._settings = settings

        # 1.4.2（PRD FR-9）：加载持久化的锁屏外观与控制开关。
        self._lock_style: str = normalize_lock_style(
            repos.settings.get(SETTING_LOCK_STYLE)
        )
        self._lock_allow_child: bool = repos.settings.get_bool(
            SETTING_LOCK_ALLOW_CHILD_SWITCH, False
        )

        self._rules: RuleSnapshot = repos.settings.get_rules()
        self._timezone_name: str = repos.settings.get(SETTING_TIMEZONE) or DEFAULT_TIMEZONE
        self._tz = self._resolve_tz(self._timezone_name)

        state = repos.runtime.load()
        self._effective_date: date = state.last_effective_date or self._today_local()
        self._grace_until: datetime | None = state.grace_until
        self._break_end_at: datetime | None = state.break_end_at
        self._continuous_seconds: float = float(state.continuous_use_seconds)
        self._fired_reminders: frozenset[int] = state.fired_reminders
        self._state_changed_at: datetime = state.state_changed_at or clock.now_utc()
        self._last_state: State = state.last_state
        self._machine._state = state.last_state  # restore across restarts

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_INTERVAL_MS)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._on_tick)

        self._running = False
        self._session_locked = False
        self._last_purge_date: date | None = None
        self._pending_rule_refresh = False
        # 1.4.5：检测到系统时间被篡改后置位，强制弹出不可跳过的家长锁屏，
        # 直到家长校正系统时间后调用 :meth:`clear_tamper_lock` 解除。
        self._tamper_locked: bool = False

        # Restore the persisted temporary unlock (survives a restart).
        unlock_raw = repos.settings.get(SETTING_UNLOCK_UNTIL)
        if unlock_raw:
            try:
                unlock_until = datetime.fromisoformat(unlock_raw)
                if unlock_until.tzinfo is None:
                    unlock_until = unlock_until.replace(tzinfo=timezone.utc)
                if unlock_until > clock.now_utc():
                    self._grace_until = self._later(self._grace_until, unlock_until)
            except ValueError:
                logger.debug("Ignoring malformed unlock_temp_until value")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def rules(self) -> RuleSnapshot:
        """The rule snapshot currently in effect."""
        return self._rules

    @property
    def state(self) -> State:
        """The current state machine state."""
        return self._machine.state

    @property
    def effective_date(self) -> date:
        """The business date the engine is currently accounting against."""
        return self._effective_date

    @property
    def timezone_name(self) -> str:
        """IANA timezone name used for local-time decisions."""
        return self._timezone_name

    @property
    def lock_style(self) -> str:
        """当前锁屏外观标识（``LOCK_STYLE_ORDER`` 之一）。"""
        return self._lock_style

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, keep_parent_mode: bool = False) -> None:
        """Load today's usage, record any abnormal exit, and start the timer.

        🔴 1.3（PRD P0-1）：新增默认参数 ``keep_parent_mode``。默认 ``False``
        时行为与 v1.2 完全一致（启动即退出家长会话）；家长解锁直达桌面路径
        传 ``True``，跳过 ``self._parent.exit("restart")``，保留已激活的家长
        会话 —— 状态机在首个 tick 按 ``parent_mode_active=True`` 自然落
        PARENT 态（不扣孩子时间、不弹锁屏）。

        Args:
            keep_parent_mode: ``True`` 时保留已激活的家长模式会话（仅家长
                解锁路径使用）；默认 ``False`` 使孩子路径与全部既有调用点
                行为零变化。
        """
        if self._running:
            return
        now = self._clock.now_utc()

        was_abnormal = self._repos.runtime.mark_started()
        if was_abnormal:
            self._repos.events.append(
                EventType.ABNORMAL_EXIT.value,
                EventSeverity.WARNING.value,
                {"detected_at": now.isoformat()},
                now,
            )
            logger.warning("Previous session did not shut down cleanly")

        self._guard.reset()
        # 持久化的业务日（修正前 _effective_date 即此值；若为 None 则取今天）。
        persisted_date = self._effective_date
        self._effective_date = self._guard.effective_date(self._tz, self._effective_date)
        # 根因修复 A（1.4.5）：跨业务日重启时，``continuous_use_seconds`` 必须清零。
        # 原来 start() 在首个 tick 之前就把 _effective_date 修正为今天，导致首个
        # tick 不再跨天、_roll_day 不执行，昨日残留的连续使用秒数被直接带入今日，
        # 使孩子点"使用"几秒~十几分钟就触顶 45 分钟强制休息。此处显式兜底清零，
        # 与 _roll_day 的语义保持一致。
        if persisted_date is not None and persisted_date != self._effective_date:
            self._continuous_seconds = 0.0
            self._break_end_at = None
            self._fired_reminders = frozenset()
            self._persist_runtime()
            logger.info(
                "Cross-day restart: cleared continuous use/break (prev=%s, now=%s)",
                persisted_date.isoformat(),
                self._effective_date.isoformat(),
            )
        self._usage.load(self._effective_date, base_quota_for(self._rules, self._effective_date))
        if not keep_parent_mode:
            self._parent.exit("restart")

        self._running = True
        self._timer.start()
        logger.info(
            "RuntimeEngine started (state=%s, date=%s, tz=%s, keep_parent_mode=%s)",
            self._machine.state.value,
            self._effective_date.isoformat(),
            self._timezone_name,
            keep_parent_mode,
        )

    def clear_tamper_lock(self) -> None:
        """解除时间篡改锁定。

        家长将系统时间校正为正确值、确认设备不再跳变后调用，使下一个 tick
        不再强制弹出篡改锁屏。运行期内为内存标志；若启动时系统时间仍被篡改，
        下一 tick 会再次检测并重新锁定（无需持久化即可自愈）。
        """
        self._tamper_locked = False
        logger.info("Tamper lock cleared by parent")

    def stop(self) -> None:
        """Stop the timer, flush usage, and mark a clean shutdown."""
        if not self._running:
            return
        self._running = False
        self._timer.stop()
        try:
            self._usage.maybe_flush(force=True)
            self._persist_runtime()
            self._repos.runtime.mark_clean_exit()
            self._db.checkpoint()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Error while shutting down RuntimeEngine")
        logger.info("RuntimeEngine stopped")

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------
    @Slot()
    def _on_tick(self) -> None:
        """Run one full tick. Never raises (D50/G1)."""
        try:
            self._tick_once()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Tick failed")
            try:
                self._repos.events.append(
                    EventType.STATE_CHANGED.value,
                    EventSeverity.ERROR.value,
                    {"error": str(exc)[:500], "phase": "tick"},
                    self._clock.now_utc(),
                )
            except Exception:
                logger.exception("Could not record tick failure event")

    def _tick_once(self) -> None:
        """The tick body, following the sequence in ARCHITECTURE.md section 6.1."""
        reading = self._guard.read()
        now_utc = reading.trusted_now_utc

        # 1. Tamper detection (hard constraint #4).
        if reading.tamper is not None:
            self._tamper_locked = True
            self._repos.events.append(
                EventType.TIME_TAMPER.value,
                EventSeverity.WARNING.value,
                {
                    "kind": reading.tamper.value,
                    "drift_seconds": round(reading.drift_seconds, 2),
                    "blocked": reading.tamper_blocked,
                },
                now_utc,
            )
            logger.warning(
                "Clock tamper detected: %s (drift=%.1fs, blocked=%s)",
                reading.tamper.value,
                reading.drift_seconds,
                reading.tamper_blocked,
            )

        # 2. Effective date, never rolling backwards.
        #    用经篡改拦截的可信 ``now`` 推算业务日（D15 不回退），避免把墙钟
        #    往前拨直接跨天刷新配额。
        new_date = max(now_utc.astimezone(self._tz).date(), self._effective_date)
        if new_date != self._effective_date:
            self._roll_day(new_date, now_utc)

        # 3. Environment inputs.
        idle_seconds = float(self._idle.idle_seconds())
        session_locked = bool(self._idle.session_locked())
        if session_locked and not self._session_locked:
            self._parent.exit("session_lock")
        self._session_locked = session_locked
        self._parent.check_timeout(now_utc)

        # Expired grace windows are dropped so GRACE cannot stick forever.
        if self._grace_until is not None and now_utc >= self._grace_until:
            self._grace_until = None
            self._repos.settings.delete(SETTING_UNLOCK_UNTIL)

        record = self._usage.current
        now_local = now_utc.astimezone(self._tz)

        tick_input = TickInput(
            now_utc=now_utc,
            now_local=now_local,
            effective_date=self._effective_date,
            monotonic_delta=reading.monotonic_delta,
            rules=self._rules,
            used_seconds=record.used_seconds,
            bonus_minutes=record.bonus_minutes,
            grace_until=self._grace_until,
            parent_mode_active=bool(self._parent.is_active),
            break_end_at=self._break_end_at,
            continuous_use_seconds=self._continuous_seconds,
            idle_seconds=idle_seconds,
            session_locked=session_locked,
            fired_reminders=self._fired_reminders,
        )

        output = self._machine.tick(tick_input)

        # 4. Apply the accounting results.
        if output.child_delta_seconds > 0:
            self._usage.add_child_seconds(output.child_delta_seconds)
        if output.parent_delta_seconds > 0:
            self._usage.add_parent_seconds(output.parent_delta_seconds)
        if output.break_started:
            self._usage.increment_break_count()

        self._continuous_seconds = output.continuous_use_seconds
        self._break_end_at = output.break_end_at
        if output.reminders_fired:
            self._fired_reminders = self._fired_reminders | set(output.reminders_fired)

        # 5. Persist (throttled) and emit UI signals.
        self._usage.maybe_flush()
        self._persist_runtime(now_utc if output.changed else None)

        for event in output.events:
            self._repos.events.append(
                event.event_type, event.severity, event.payload, now_utc
            )

        if output.changed:
            self._state_changed_at = now_utc
            self._last_state = output.state
            self.stateChanged.emit(output.previous_state, output.state, output.reason)

        for point in output.reminders_fired:
            self.reminderFired.emit(int(point), float(output.remaining_minutes))

        self.overlayRequested.emit(self._overlay_context(output))
        self.tickCompleted.emit(output)

        self._maybe_purge(now_utc)

    def _roll_day(self, new_date: date, now_utc: datetime) -> None:
        """Handle a business-date change (D17).

        Args:
            new_date: The new effective date.
            now_utc: Current UTC time.
        """
        previous = self._usage.current
        last_event_date = self._repos.settings.get(SETTING_LAST_DAILY_EVENT_DATE)
        if previous.usage_date.isoformat() != last_event_date:
            self._repos.events.append(
                EventType.DAILY_USAGE.value,
                EventSeverity.INFO.value,
                {
                    "date": previous.usage_date.isoformat(),
                    "used_minutes": previous.used_minutes,
                    "parent_minutes": previous.parent_minutes,
                    "bonus_minutes": previous.bonus_minutes,
                    "break_count": previous.break_count,
                },
                now_utc,
            )
            self._repos.settings.set(
                SETTING_LAST_DAILY_EVENT_DATE, previous.usage_date.isoformat()
            )

        self._usage.rollover(new_date, base_quota_for(self._rules, new_date))
        self._parent.exit("day_roll")
        self._machine.reset_for_new_day()

        self._effective_date = new_date
        self._continuous_seconds = 0.0
        self._break_end_at = None
        self._fired_reminders = frozenset()
        self._pending_rule_refresh = True

        self._repos.runtime.patch(
            last_effective_date=new_date,
            continuous_use_seconds=0.0,
            break_end_at=None,
            fired_reminders=frozenset(),
        )
        logger.info("Rolled over to %s", new_date.isoformat())
        self.dayRolled.emit(new_date)

    def _persist_runtime(self, state_changed_at: datetime | None = None) -> None:
        """Write the volatile runtime fields back to SQLite."""
        fields: dict[str, Any] = {
            "last_state": self._machine.state,
            "continuous_use_seconds": self._continuous_seconds,
            "break_end_at": self._break_end_at,
            "grace_until": self._grace_until,
            "fired_reminders": self._fired_reminders,
            "last_effective_date": self._effective_date,
        }
        if state_changed_at is not None:
            fields["state_changed_at"] = state_changed_at
        self._repos.runtime.patch(**fields)

    def _overlay_context(self, output: TickOutput) -> dict[str, Any] | None:
        """Build the overlay descriptor for the UI layer.

        Args:
            output: The tick result.

        Returns:
            A plain dict consumed by ``OverlayManager``, or ``None`` when no
            overlay should be shown.
        """
        # 1.4.5：篡改锁定优先于一切常规锁屏——孩子无法通过改表绕过管控，
        # 必须交由家长校正系统时间后解除。放在早返回之前，确保无常规锁屏时
        # （如 ACTIVE 态）也能强制弹出篡改锁屏。
        if self._tamper_locked:
            return {
                "reason": OVERLAY_TAMPER,
                "title": "系统时间被篡改",
                "detail": (
                    "检测到系统时间被手动修改。为防止绕过使用时长与强制休息，"
                    "设备已锁定。请家长将系统时间校正为正确时间后，重启本程序解除。"
                ),
                "countdown_seconds": None,
                "next_available_text": None,
                # 篡改锁屏不可申请延时、不可关机，只能由家长校正时间后重启。
                "allow_extension_request": False,
                "allow_shutdown": False,
            }
        if not output.overlay_visible or output.overlay_reason is None:
            return None
        reason = output.overlay_reason
        titles = {
            "CURFEW": "现在不是上机时间",
            "QUOTA": "今天的上机时间用完啦",
            "BREAK": "该休息一下眼睛了",
        }
        details = {
            "CURFEW": f"允许时段为 {self._rules.allowed_start} - {self._rules.allowed_end}",
            "QUOTA": f"今日额度 {output.effective_quota_minutes} 分钟已用完",
            "BREAK": f"连续使用已满 {self._rules.continuous_limit_minutes} 分钟",
        }
        next_text: str | None = None
        if output.next_available_local is not None:
            next_text = output.next_available_local.strftime("%m-%d %H:%M")
        return {
            "reason": reason,
            "title": titles.get(reason, "已锁定"),
            "detail": details.get(reason, ""),
            "countdown_seconds": output.overlay_countdown_seconds,
            "next_available_text": next_text,
            # D25: a forced break cannot be skipped by requesting more time.
            "allow_extension_request": reason != OVERLAY_BREAK,
            # 1.4.3（需求 2a）：强制休息不显示关机按钮、不参与自动关机。
            "allow_shutdown": reason != OVERLAY_BREAK,
        }

    def _maybe_purge(self, now_utc: datetime) -> None:
        """Purge uploaded events and sent outbox rows once per day."""
        today = now_utc.astimezone(self._tz).date()
        if self._last_purge_date == today:
            return
        self._last_purge_date = today
        cutoff = now_utc - timedelta(days=RETENTION_DAYS)
        try:
            removed_events = self._repos.events.purge_uploaded(cutoff)
            removed_outbox = self._repos.outbox.purge_sent(cutoff)
            if removed_events or removed_outbox:
                logger.info(
                    "Purged %d events and %d outbox rows older than %d days",
                    removed_events,
                    removed_outbox,
                    RETENTION_DAYS,
                )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Retention purge failed")

    # ------------------------------------------------------------------
    # Slots invoked from the UI / sync layer (always on the main thread)
    # ------------------------------------------------------------------
    @Slot(object)
    def apply_rules(self, rules: RuleSnapshot) -> None:
        """Adopt a new rule snapshot and persist it.

        Args:
            rules: The new snapshot (from ``/client/sync`` or the parent panel).
        """
        previous_version = self._rules.version
        self._rules = rules
        self._repos.settings.save_rules(rules)
        self._usage.load(self._effective_date, base_quota_for(rules, self._effective_date))
        self._pending_rule_refresh = False
        self._repos.events.append(
            EventType.RULE_UPDATED.value,
            EventSeverity.INFO.value,
            {"from_version": previous_version, "to_version": rules.version},
            self._clock.now_utc(),
        )
        logger.info("Rules updated: v%d -> v%d", previous_version, rules.version)

    @Slot(str, int)
    def apply_unlock_temp(self, command_id: str, minutes: int) -> None:
        """Extend the grace window by ``minutes`` (A15: the later end wins).

        Args:
            command_id: Originating command id (audit only).
            minutes: Unlock duration in minutes.
        """
        deadline = self._clock.now_utc() + timedelta(minutes=max(1, int(minutes)))
        self._grace_until = self._later(self._grace_until, deadline)
        if self._grace_until is not None:
            self._repos.settings.set(SETTING_UNLOCK_UNTIL, self._grace_until.isoformat())
        logger.info(
            "Temporary unlock active until %s (command=%s)",
            self._grace_until.isoformat() if self._grace_until else "-",
            command_id,
        )

    @Slot()
    def clear_grace(self) -> None:
        """Cancel any active temporary unlock immediately (D19 ``RESUME``).

        ``RESUME_ENFORCEMENT`` must take effect at once; without this the
        in-memory grace window would keep the child unlocked until it expired
        naturally even though the persisted deadline was already removed.
        """
        if self._grace_until is None:
            return
        self._grace_until = None
        self._repos.settings.delete(SETTING_UNLOCK_UNTIL)
        self._persist_runtime()
        logger.info("Temporary unlock cleared")

    @Slot(int)
    def grant_emergency_grace(self, minutes: int) -> None:
        """Grant an emergency grace window after repeated password failures.

        Args:
            minutes: Grace duration in minutes (15 per D20/D43).
        """
        deadline = self._clock.now_utc() + timedelta(minutes=max(1, int(minutes)))
        self._grace_until = self._later(self._grace_until, deadline)
        self._persist_runtime()
        logger.warning("Emergency grace granted for %d minutes", minutes)

    @Slot(bool)
    def apply_enforcement(self, enabled: bool) -> None:
        """Enable or disable rule enforcement locally.

        Args:
            enabled: ``False`` puts the client into ``DISABLED``.
        """
        self._rules = RuleSnapshot(
            **{**self._rules.to_dict(), "enforcement_enabled": bool(enabled)}
        )
        self._repos.settings.save_rules(self._rules)
        self._repos.settings.set_bool(SETTING_ENFORCEMENT_PAUSED, not enabled)
        logger.info("Enforcement %s", "enabled" if enabled else "paused")

    @Slot(str, bool)
    def apply_lock_style(self, style: str, allow_child_switch: bool) -> None:
        """设置锁屏外观并持久化 + 广播（单机恒定 eyecare，双保险入口）。

        家长面板初始化/纠正与（未来）管理命令都走同一入口，保证语义一致。

        Args:
            style: 锁屏外观（``"default"|"eyecare"|"eyecare2"``）。
            allow_child_switch: 是否允许孩子自主切换（单机恒定 ``False``）。
        """
        if style not in LOCK_STYLE_ORDER:
            style = LOCK_STYLE_DEFAULT
        self._lock_style = style
        self._lock_allow_child = bool(allow_child_switch)
        self._repos.settings.set(SETTING_LOCK_STYLE, style)
        self._repos.settings.set_bool(
            SETTING_LOCK_ALLOW_CHILD_SWITCH, bool(allow_child_switch)
        )
        self.lockStyleChanged.emit(style, bool(allow_child_switch))
        logger.info("锁屏外观切换为 %s（allow_child_switch=%s）", style, allow_child_switch)

    @Slot(object)
    def apply_credits(self, credits: list) -> None:
        """Credit approved extension minutes (hard constraint #5).

        Args:
            credits: :class:`~kidtime_client.sync.payloads.CreditItem` list.
        """
        credited_any = False
        for item in credits or []:
            try:
                result = self._credit.credit(
                    item.request_id,
                    int(item.approved_minutes),
                    item.target_date,
                    self._effective_date,
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Crediting %s failed", item.request_id)
                continue
            if result.value == "CREDITED":
                credited_any = True
        if credited_any:
            # Refresh the in-memory bonus so the very next tick sees it.
            self._usage.refresh_bonus_from_db()

    def submit_extension_request(self, minutes: int, reason: str = "") -> str | None:
        """Queue a new extension request for the next sync.

        Args:
            minutes: Requested minutes (clamped to the allowed range).
            reason: Optional free-text reason shown to the parent.

        Returns:
            The generated request id, or ``None`` when the daily pending limit
            (:data:`MAX_PENDING_EXTENSIONS_PER_DAY`) is already reached.
        """
        pending = self._repos.outbox.due_by_kind(
            OutboxKind.EXTENSION_REQUEST.value, self._clock.now_utc(), limit=50
        )
        today_pending = [
            item
            for item in pending
            if item.payload.get("target_date") == self._effective_date.isoformat()
        ]
        if len(today_pending) >= MAX_PENDING_EXTENSIONS_PER_DAY:
            logger.info("Extension request rejected locally: daily pending limit reached")
            return None

        clamped = max(EXTENSION_MIN_MINUTES, min(EXTENSION_MAX_MINUTES, int(minutes)))
        now = self._clock.now_utc()
        request_id = str(uuid.uuid4())
        item = ExtensionUploadItem(
            id=request_id,
            target_date=self._effective_date,
            requested_minutes=clamped,
            reason=(reason or "")[:500],
            client_created_at=now,
        )
        self._repos.outbox.enqueue(
            request_id, OutboxKind.EXTENSION_REQUEST.value, item.to_json()
        )
        self._repos.events.append(
            EventType.EXTENSION_SUBMITTED.value,
            EventSeverity.INFO.value,
            {"request_id": request_id, "minutes": clamped},
            now,
        )
        logger.info("Extension request %s queued (%d minutes)", request_id, clamped)
        return request_id

    # ------------------------------------------------------------------
    # Sync integration
    # ------------------------------------------------------------------
    def build_sync_request(self) -> SyncRequest | None:
        """Assemble the next :class:`SyncRequest` from local state.

        Reads (main thread only): runtime state, unsynced daily usage,
        un-uploaded events, pending outbox rows, unconfirmed ledger entries.

        Returns:
            The request, or ``None`` when the engine is not ready.
        """
        now = self._clock.now_utc()

        usage_records = self._usage.pending_upload(limit=SYNC_MAX_USAGE_ITEMS)
        usage_items = tuple(
            UsageUploadItem(
                usage_date=record.usage_date,
                used_minutes=record.used_minutes,
                bonus_minutes=record.bonus_minutes,
                parent_minutes=record.parent_minutes,
                break_count=record.break_count,
                base_quota_minutes=record.base_quota_minutes,
            )
            for record in usage_records
        )

        local_events = self._repos.events.pending_upload(limit=SYNC_MAX_EVENT_ITEMS)
        event_items = tuple(
            EventUploadItem(
                client_event_id=event.id,
                event_type=event.event_type,
                severity=event.severity,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )
            for event in local_events
        )

        extension_rows = self._repos.outbox.due_by_kind(
            OutboxKind.EXTENSION_REQUEST.value, now, limit=SYNC_MAX_EXTENSION_ITEMS
        )
        extension_items = tuple(
            ExtensionUploadItem.from_json(row.payload) for row in extension_rows
        )

        ack_rows = self._repos.outbox.due_by_kind(
            OutboxKind.COMMAND_ACK.value, now, limit=SYNC_MAX_COMMAND_ACK_ITEMS
        )
        ack_items = tuple(CommandAck.from_json(row.payload) for row in ack_rows)

        confirms = tuple(
            self._credit.unconfirmed_request_ids()[:SYNC_MAX_CREDIT_CONFIRM_ITEMS]
        )

        outbox_ids = tuple(row.id for row in extension_rows) + tuple(
            row.id for row in ack_rows
        )

        return SyncRequest(
            client_version=CLIENT_VERSION,
            timezone=self._timezone_name,
            device_time=now,
            effective_date=self._effective_date,
            state=self._machine.state.value,
            state_changed_at=self._state_changed_at,
            rule_version=self._rules.version,
            usage=usage_items,
            events=event_items,
            extension_requests=extension_items,
            command_acks=ack_items,
            credit_confirms=confirms,
            outbox_row_ids=outbox_ids,
            usage_dates=tuple(record.usage_date for record in usage_records),
        )

    @Slot(object)
    def on_sync_response(self, resp: object) -> None:
        """Apply a sync response. Runs on the GUI main thread only.

        Order matters: rules first (they change quotas), then credits (they need
        the current effective date), then commands (they may reference the new
        rules), then bookkeeping.

        Args:
            resp: The :class:`SyncResponse` delivered by ``SyncEngine``.
        """
        if not isinstance(resp, SyncResponse):
            logger.warning("Ignoring sync response of unexpected type %r", type(resp))
            return
        now = self._clock.now_utc()
        try:
            if resp.rules:
                snapshot = RuleSnapshot.from_dict(dict(resp.rules))
                if snapshot.version != self._rules.version:
                    self.apply_rules(snapshot)

            if resp.credits:
                self.apply_credits(list(resp.credits))

            request = resp.request
            if request is not None:
                if request.usage_dates:
                    self._repos.usage.mark_synced(list(request.usage_dates), now)
                if request.events:
                    self._repos.events.mark_uploaded(
                        [item.client_event_id for item in request.events], now
                    )
                if request.outbox_row_ids:
                    self._repos.outbox.mark_sent(list(request.outbox_row_ids), now)
                if request.credit_confirms:
                    self._credit.mark_confirmed(list(request.credit_confirms))

            self._repos.runtime.patch(last_sync_at=now, last_sync_ok=True)
            self.syncStatusChanged.emit(True, "同步成功")
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Applying sync response failed")
            self._repos.runtime.patch(last_sync_at=now, last_sync_ok=False)
            self.syncStatusChanged.emit(False, f"同步落库失败：{exc}")

    @Slot(str)
    def on_sync_failed(self, message: str) -> None:
        """Record a failed sync attempt.

        Args:
            message: Human-readable error message from the worker.
        """
        now = self._clock.now_utc()
        try:
            self._repos.events.append(
                EventType.SYNC_FAILED.value,
                EventSeverity.WARNING.value,
                {"error": (message or "")[:500]},
                now,
            )
            self._repos.runtime.patch(last_sync_at=now, last_sync_ok=False)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not record sync failure")
        self.syncStatusChanged.emit(False, message)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def set_session_locked(self, locked: bool) -> None:
        """Propagate a Windows session lock/unlock notification.

        Args:
            locked: ``True`` when the session was locked.
        """
        setter = getattr(self._idle, "set_session_locked", None)
        if callable(setter):
            setter(locked)

    def set_timezone(self, name: str) -> None:
        """Change the timezone used for local-time decisions.

        Args:
            name: IANA timezone name.
        """
        self._timezone_name = name
        self._tz = self._resolve_tz(name)
        self._repos.settings.set(SETTING_TIMEZONE, name)

    def _today_local(self) -> date:
        """Return today's date in the configured timezone."""
        return self._clock.now_utc().astimezone(self._tz).date()

    @staticmethod
    def _later(first: datetime | None, second: datetime | None) -> datetime | None:
        """Return the later of two optional datetimes."""
        if first is None:
            return second
        if second is None:
            return first
        return max(first, second)

    @staticmethod
    def _resolve_tz(name: str) -> ZoneInfo:
        """Resolve a timezone name, falling back to the project default."""
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("Unknown timezone %r; falling back to %s", name, DEFAULT_TIMEZONE)
            try:
                return ZoneInfo(DEFAULT_TIMEZONE)
            except Exception:  # pragma: no cover - tzdata missing entirely
                return ZoneInfo("UTC")


__all__ = ["RETENTION_DAYS", "RuntimeEngine", "TICK_INTERVAL_MS"]
