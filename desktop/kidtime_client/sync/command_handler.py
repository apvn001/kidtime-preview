"""Applies remote commands to local state and produces acknowledgements.

Five command types are supported (``backend/app/core/constants.py``):

============================  ==================================================
``UNLOCK_TEMP``               Temporarily lift enforcement for N minutes. When an
                              unlock is already active the **later** deadline
                              wins (A15) -- a shorter overlapping unlock never
                              shortens an existing one.
``PAUSE_ENFORCEMENT``         Stop enforcing rules until explicitly resumed.
``RESUME_ENFORCEMENT``        Resume enforcement, clearing any active unlock.
``SYNC_NOW``                  Trigger an immediate sync round trip.
``RESET_USAGE``               Zero the local daily usage for a target date
                              (V7 §2.3, 方案 A). The local reset happens as soon
                              as the command is applied; the ack reports success
                              to the backend which then finalises the clearing.
``DEDUCT_USAGE``              Deduct N minutes from today's allowance locally
                              (V7 §3.5). The backend has already applied the
                              deduction server-side; this only mirrors it so the
                              child's screen shows the shorter remaining time
                              immediately. The ack does NOT trigger a second
                              server write (no double deduction).
============================  ==================================================

All state changes are written by the GUI main thread only (hard constraint #2).
Every handled command produces exactly one ``command_ack`` outbox row, even on
failure, so the backend always learns the outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Final

from kidtime_client.constants import (
    CommandType,
    EventSeverity,
    EventType,
    LOCK_STYLE_ORDER,
    OutboxKind,
    SETTING_ENFORCEMENT_PAUSED,
    SETTING_LOCK_ALLOW_CHILD_SWITCH,
    SETTING_LOCK_STYLE,
    SETTING_UNLOCK_UNTIL,
    UNLOCK_TEMP_DEFAULT_MINUTES,
    UNLOCK_TEMP_MAX_MINUTES,
    UNLOCK_TEMP_MIN_MINUTES,
)
from kidtime_client.core.clock import Clock
from kidtime_client.storage.repositories import (
    EventRepository,
    OutboxRepository,
    SettingsRepository,
)
from kidtime_client.sync.payloads import CommandAck, CommandDeliverItem, to_utc_iso

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免循环导入
    from kidtime_client.core.usage_tracker import UsageTracker

logger = logging.getLogger(__name__)

#: Ack status values accepted by the backend.
ACK_SUCCESS: Final[str] = "success"
ACK_FAILED: Final[str] = "failed"


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Result of handling one command.

    Attributes:
        command_id: The command that was processed.
        status: ``success`` or ``failed``.
        error: Failure reason, ``None`` on success.
        applied: ``True`` when local state actually changed.
        unlock_until: New unlock deadline when the command extended one.
        trigger_sync: ``True`` when the caller should sync immediately.
    """

    command_id: str
    status: str
    error: str | None = None
    applied: bool = False
    unlock_until: datetime | None = None
    trigger_sync: bool = False

    @property
    def ok(self) -> bool:
        """``True`` when the command executed successfully."""
        return self.status == ACK_SUCCESS


class CommandHandler:
    """Turns delivered commands into local state changes plus acks.

    Args:
        settings: Settings repository (unlock deadline / pause flag).
        events: Event repository used to record an audit trail.
        outbox: Outbox repository receiving the ``command_ack`` rows.
        clock: Injected clock (``FakeClock`` in tests).
        usage: Usage tracker used to zero local daily usage on ``RESET_USAGE``.
        on_unlock: Callback invoked with the new unlock deadline so the runtime
            engine can refresh its cached grace window.
        on_enforcement: Callback invoked with the new ``enforcement_enabled``
            value (``False`` = paused).
        on_sync_now: Callback that triggers an immediate sync.
        on_reset_usage: Callback invoked after a ``RESET_USAGE`` is applied,
            typically to push the acknowledgement to the server right away.
        on_lock_style: Callback invoked after a ``SET_LOCK_STYLE`` is applied,
            passing ``(style, allow_child_switch)`` so the runtime engine can
            refresh the lock-screen rendering (mirrors ``on_reset_usage``).
    """

    def __init__(
        self,
        settings: SettingsRepository,
        events: EventRepository,
        outbox: OutboxRepository,
        clock: Clock,
        usage: "UsageTracker | None" = None,
        on_unlock: Callable[[datetime], None] | None = None,
        on_enforcement: Callable[[bool], None] | None = None,
        on_sync_now: Callable[[], None] | None = None,
        on_reset_usage: Callable[[], None] | None = None,
        on_lock_style: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._settings = settings
        self._events = events
        self._outbox = outbox
        self._clock = clock
        self._usage = usage
        self._on_unlock = on_unlock
        self._on_enforcement = on_enforcement
        self._on_sync_now = on_sync_now
        self._on_reset_usage = on_reset_usage
        self._on_lock_style = on_lock_style
        self._handled: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def handle_all(self, commands: list[CommandDeliverItem]) -> list[CommandOutcome]:
        """Handle a batch of delivered commands in order.

        Args:
            commands: Commands from :attr:`SyncResponse.commands`.

        Returns:
            One :class:`CommandOutcome` per command, in the same order.
        """
        outcomes: list[CommandOutcome] = []
        for command in commands:
            outcomes.append(self.handle(command))
        return outcomes

    def handle(self, command: CommandDeliverItem) -> CommandOutcome:
        """Execute a single command and enqueue its acknowledgement.

        Never raises: an unexpected error is converted into a ``failed`` ack so
        the backend does not keep redelivering the command forever.

        Args:
            command: The delivered command.

        Returns:
            The :class:`CommandOutcome` describing what happened.
        """
        now = self._clock.now_utc()

        if command.command_id in self._handled:
            outcome = CommandOutcome(command.command_id, ACK_SUCCESS, applied=False)
            self._enqueue_ack(outcome, now)
            return outcome

        if command.expires_at <= now:
            outcome = CommandOutcome(
                command.command_id, ACK_FAILED, error="指令已过期", applied=False
            )
            self._handled.add(command.command_id)
            self._enqueue_ack(outcome, now)
            logger.info("Command %s expired before execution", command.command_id)
            return outcome

        try:
            outcome = self._dispatch(command, now)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Command %s failed", command.command_id)
            outcome = CommandOutcome(command.command_id, ACK_FAILED, error=str(exc)[:255])

        self._handled.add(command.command_id)
        self._enqueue_ack(outcome, now)
        return outcome

    def active_unlock_until(self) -> datetime | None:
        """Return the current unlock deadline, or ``None`` when not unlocked."""
        raw = self._settings.get(SETTING_UNLOCK_UNTIL)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed <= self._clock.now_utc():
            return None
        return parsed

    def enforcement_paused(self) -> bool:
        """Return ``True`` when a ``PAUSE_ENFORCEMENT`` command is in effect."""
        return self._settings.get_bool(SETTING_ENFORCEMENT_PAUSED, False)

    # ------------------------------------------------------------------
    # Per-type handlers
    # ------------------------------------------------------------------
    def _dispatch(self, command: CommandDeliverItem, now: datetime) -> CommandOutcome:
        """Route ``command`` to the appropriate handler."""
        kind = command.type
        if kind == CommandType.UNLOCK_TEMP.value:
            return self._handle_unlock_temp(command, now)
        if kind == CommandType.PAUSE_ENFORCEMENT.value:
            return self._handle_pause(command, now)
        if kind == CommandType.RESUME_ENFORCEMENT.value:
            return self._handle_resume(command, now)
        if kind == CommandType.SYNC_NOW.value:
            return self._handle_sync_now(command)
        if kind == CommandType.RESET_USAGE.value:
            return self._handle_reset_usage(command, now)
        if kind == CommandType.SET_LOCK_STYLE.value:
            return self._handle_set_lock_style(command, now)
        if kind == CommandType.DEDUCT_USAGE.value:
            return self._handle_deduct_usage(command, now)
        return CommandOutcome(
            command.command_id, ACK_FAILED, error=f"未知指令类型：{kind}", applied=False
        )

    def _handle_unlock_temp(self, command: CommandDeliverItem, now: datetime) -> CommandOutcome:
        """Grant (or extend) a temporary unlock window.

        A15: when an unlock is already active, the resulting deadline is
        ``max(existing, now + minutes)`` -- a new, shorter unlock never cuts an
        existing longer one short.
        """
        raw_minutes = command.payload.get("minutes", UNLOCK_TEMP_DEFAULT_MINUTES)
        try:
            minutes = int(raw_minutes)
        except (TypeError, ValueError):
            minutes = UNLOCK_TEMP_DEFAULT_MINUTES
        minutes = max(UNLOCK_TEMP_MIN_MINUTES, min(UNLOCK_TEMP_MAX_MINUTES, minutes))

        candidate = now + timedelta(minutes=minutes)
        existing = self.active_unlock_until()
        deadline = max(existing, candidate) if existing else candidate

        self._settings.set(SETTING_UNLOCK_UNTIL, deadline.isoformat())
        if self._on_unlock is not None:
            self._on_unlock(deadline)

        self._events.append(
            EventType.COMMAND_EXECUTED.value,
            EventSeverity.INFO.value,
            {
                "command_id": command.command_id,
                "type": CommandType.UNLOCK_TEMP.value,
                "minutes": minutes,
                "unlock_until": to_utc_iso(deadline),
                "extended": bool(existing),
            },
            occurred_at=now,
        )
        logger.info(
            "UNLOCK_TEMP granted for %s minutes (until %s)", minutes, deadline.isoformat()
        )
        return CommandOutcome(
            command.command_id, ACK_SUCCESS, applied=True, unlock_until=deadline
        )

    def _handle_pause(self, command: CommandDeliverItem, now: datetime) -> CommandOutcome:
        """Pause enforcement until an explicit resume."""
        already = self.enforcement_paused()
        self._settings.set_bool(SETTING_ENFORCEMENT_PAUSED, True)
        if self._on_enforcement is not None:
            self._on_enforcement(False)
        self._events.append(
            EventType.COMMAND_EXECUTED.value,
            EventSeverity.WARNING.value,
            {
                "command_id": command.command_id,
                "type": CommandType.PAUSE_ENFORCEMENT.value,
                "already_paused": already,
            },
            occurred_at=now,
        )
        logger.info("Enforcement paused by command %s", command.command_id)
        return CommandOutcome(command.command_id, ACK_SUCCESS, applied=not already)

    def _handle_resume(self, command: CommandDeliverItem, now: datetime) -> CommandOutcome:
        """Resume enforcement and clear any pending temporary unlock."""
        was_paused = self.enforcement_paused()
        self._settings.set_bool(SETTING_ENFORCEMENT_PAUSED, False)
        self._settings.delete(SETTING_UNLOCK_UNTIL)
        if self._on_enforcement is not None:
            self._on_enforcement(True)
        if self._on_unlock is not None:
            self._on_unlock(now)
        self._events.append(
            EventType.COMMAND_EXECUTED.value,
            EventSeverity.INFO.value,
            {
                "command_id": command.command_id,
                "type": CommandType.RESUME_ENFORCEMENT.value,
                "was_paused": was_paused,
            },
            occurred_at=now,
        )
        logger.info("Enforcement resumed by command %s", command.command_id)
        return CommandOutcome(command.command_id, ACK_SUCCESS, applied=was_paused)

    def _handle_sync_now(self, command: CommandDeliverItem) -> CommandOutcome:
        """Ask the sync engine to run another round trip right away."""
        if self._on_sync_now is not None:
            self._on_sync_now()
        return CommandOutcome(
            command.command_id, ACK_SUCCESS, applied=True, trigger_sync=True
        )

    def _handle_reset_usage(
        self, command: CommandDeliverItem, now: datetime
    ) -> CommandOutcome:
        """Zero local daily usage per a ``RESET_USAGE`` command (方案 A).

        The backend only issues the command once the admin confirms; this handler
        performs the actual local reset immediately and acks ``success``. The
        payload shape is ``{"target_date": "YYYY-MM-DD", "include_bonus": bool}``;
        ``include_bonus`` defaults to ``False`` (bonus preserved).
        """
        payload = dict(command.payload or {})
        include_bonus = bool(payload.get("include_bonus", False))

        # 本地重置一律以客户端「当前生效日」为准，防御服务端下发的 target_date
        # （设备时区）与客户端实际业务日因时区/跨零点错位而不一致：否则
        # usage_tracker.reset_today 因 usage_date != target_date 不会清零内存中
        # 当日用量，表现为「明明点了重置却没生效」。服务端在收到请求时已立即清零
        # 服务端用量，此处只负责让孩子屏幕上的当日用量立即归零。
        target_date = (
            self._usage.current.usage_date
            if self._usage is not None and self._usage.current is not None
            else self._clock.now_utc().date()
        )

        applied = False
        if self._usage is not None:
            self._usage.reset_today(target_date, include_bonus=include_bonus)
            applied = True

        if self._on_reset_usage is not None:
            self._on_reset_usage()

        self._events.append(
            EventType.COMMAND_EXECUTED.value,
            EventSeverity.INFO.value,
            {
                "command_id": command.command_id,
                "type": CommandType.RESET_USAGE.value,
                "target_date": target_date.isoformat(),
                "include_bonus": include_bonus,
            },
            occurred_at=now,
        )
        logger.info(
            "RESET_USAGE applied for %s (include_bonus=%s)",
            target_date.isoformat(),
            include_bonus,
        )
        return CommandOutcome(command.command_id, ACK_SUCCESS, applied=applied)

    def _handle_set_lock_style(
        self, command: CommandDeliverItem, now: datetime
    ) -> CommandOutcome:
        """应用服务端下发的 ``SET_LOCK_STYLE``（权威覆盖，方案镜像 RESET_USAGE）。

        payload = ``{"style": "default"|"eyecare"|"eyecare2", "allow_child_switch": bool}``。
        服务端值为权威，覆盖孩子本地临时切换（FR-10）。
        """
        payload = dict(command.payload or {})
        raw_style = str(payload.get("style") or "default")
        style = raw_style if raw_style in LOCK_STYLE_ORDER else "default"
        allow = bool(payload.get("allow_child_switch", False))
        # 持久化两个新设置键（覆盖本地临时值）。
        self._settings.set(SETTING_LOCK_STYLE, style)
        self._settings.set_bool(SETTING_LOCK_ALLOW_CHILD_SWITCH, allow)
        if self._on_lock_style is not None:
            self._on_lock_style(style, allow)
        self._events.append(
            EventType.COMMAND_EXECUTED.value,
            EventSeverity.INFO.value,
            {
                "command_id": command.command_id,
                "type": CommandType.SET_LOCK_STYLE.value,
                "style": style,
                "allow_child_switch": allow,
            },
            occurred_at=now,
        )
        logger.info(
            "SET_LOCK_STYLE applied: style=%s allow_child_switch=%s", style, allow
        )
        return CommandOutcome(command.command_id, ACK_SUCCESS, applied=True)

    def _handle_deduct_usage(
        self, command: CommandDeliverItem, now: datetime
    ) -> CommandOutcome:
        """本地镜像服务端的 `DEDUCT_USAGE` 扣减（V7 §3.5，最小客户端改动）。

        payload = ``{"target_date": "YYYY-MM-DD", "minutes": int, "reason": str}``。
        服务端在 ``deduct()`` 内已立即把 ``daily_usage.used_minutes += applied``
        作为唯一真相源；此处仅把本地 ``used_seconds += minutes*60`` 同步，让孩子
        屏幕上的剩余时间立即下降。ack 上报 ``success`` 后服务端**不再**二次写库
        （与 RESET_USAGE 的 ack 门控清零相反，避免双倍扣减）。
        """
        payload = dict(command.payload or {})
        try:
            minutes = int(payload.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        target_date = (
            self._usage.current.usage_date
            if self._usage is not None and self._usage.current is not None
            else self._clock.now_utc().date()
        )

        if minutes <= 0:
            # 服务端仅在 applied>0 时才下发；防御性处理非法载荷按失败回执。
            return CommandOutcome(
                command.command_id, ACK_FAILED, error="扣减分钟数非法", applied=False
            )

        applied = False
        if self._usage is not None:
            self._usage.deduct_today(minutes)
            applied = True

        self._events.append(
            EventType.COMMAND_EXECUTED.value,
            EventSeverity.INFO.value,
            {
                "command_id": command.command_id,
                "type": CommandType.DEDUCT_USAGE.value,
                "minutes": minutes,
                "target_date": target_date.isoformat(),
            },
            occurred_at=now,
        )
        logger.info(
            "DEDUCT_USAGE applied: minutes=%s target_date=%s",
            minutes,
            target_date.isoformat(),
        )
        return CommandOutcome(command.command_id, ACK_SUCCESS, applied=applied)

    # ------------------------------------------------------------------
    # Acknowledgement plumbing
    # ------------------------------------------------------------------
    def _enqueue_ack(self, outcome: CommandOutcome, now: datetime) -> None:
        """Write the acknowledgement to the outbox (best effort)."""
        ack = CommandAck(
            command_id=outcome.command_id,
            status=outcome.status,
            executed_at=now,
            error=outcome.error,
        )
        try:
            # The command id doubles as the outbox idempotency key: re-handling
            # the same command can never produce a duplicate ack row.
            self._outbox.enqueue(
                outcome.command_id, OutboxKind.COMMAND_ACK.value, ack.to_json()
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to enqueue command ack for %s", outcome.command_id)


__all__ = ["ACK_FAILED", "ACK_SUCCESS", "CommandHandler", "CommandOutcome"]
