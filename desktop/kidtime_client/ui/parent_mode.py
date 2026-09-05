"""Parent mode: local authentication, cooldown, and auto-exit.

Design rules implemented here (PRD section 5.3, ARCHITECTURE.md section 5.20):

* D43 -- 5 consecutive wrong passwords start a 60-second cooldown.
* D20/D43 -- 10 consecutive wrong passwords grant a 15-minute emergency GRACE
  window and record an ``EMERGENCY_GRACE`` event. The child is never locked out
  of the machine because a parent forgot the password.
* D40 -- the 20-character offline recovery code is reusable and is checked only
  after the password fails.
* Parent mode auto-exits after ``rules.parent_mode_timeout_minutes`` of
  inactivity, on session lock, on day rollover, and on restart.

The controller owns no widgets. The UI layer listens on :attr:`exited` and must
immediately close every parent panel when it fires.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Final

from PySide6.QtCore import QObject, Signal

from kidtime_client.constants import (
    EventSeverity,
    EventType,
    SETTING_PARENT_PASSWORD,
    SETTING_RECOVERY_CODE,
)
from kidtime_client.core.clock import Clock
from kidtime_client.security.passwords import (
    generate_recovery_code,
    hash_secret,
    normalize_recovery_code,
    validate_parent_password,
    verify_secret,
)
from kidtime_client.storage.repositories import (
    EventRepository,
    RuntimeStateRepository,
    SettingsRepository,
)

logger = logging.getLogger(__name__)

#: Reasons that can be passed to :meth:`ParentModeController.exit`.
EXIT_TIMEOUT: Final[str] = "timeout"
EXIT_SESSION_LOCK: Final[str] = "session_lock"
EXIT_RESTART: Final[str] = "restart"
EXIT_DAY_ROLL: Final[str] = "day_roll"
EXIT_MANUAL: Final[str] = "manual"


class EnterResult(str, Enum):
    """Outcome of a :meth:`ParentModeController.try_enter` attempt."""

    OK = "OK"
    """Password accepted; parent mode is now active."""

    WRONG = "WRONG"
    """Secret did not match; the failure counter was incremented."""

    COOLDOWN = "COOLDOWN"
    """Still inside the 60-second cooldown after 5 failures (D43)."""

    EMERGENCY_GRACE = "EMERGENCY_GRACE"
    """10 failures reached; a 15-minute GRACE window was granted (D20/D43)."""

    RECOVERY_USED = "RECOVERY_USED"
    """The offline recovery code was accepted (D40)."""


class ParentModeController(QObject):
    """Owns the parent-mode session and its authentication policy.

    Args:
        settings_repo: Settings repository holding the password/recovery hashes.
        runtime_repo: Runtime state repository persisting the failure counter
            and the cooldown deadline across restarts.
        events: Event repository for the audit trail.
        clock: Injected clock (``FakeClock`` in tests).
        timeout_provider: Callable returning the current
            ``rules.parent_mode_timeout_minutes``.
    """

    entered = Signal()
    exited = Signal(str)  # reason: timeout|session_lock|restart|day_roll|manual
    emergencyGrace = Signal(int)  # minutes = 15

    COOLDOWN_THRESHOLD: Final[int] = 5
    COOLDOWN_SECONDS: Final[int] = 60
    EMERGENCY_THRESHOLD: Final[int] = 10
    EMERGENCY_GRACE_MINUTES: Final[int] = 15

    def __init__(
        self,
        settings_repo: SettingsRepository,
        runtime_repo: RuntimeStateRepository,
        events: EventRepository,
        clock: Clock,
        timeout_provider: Callable[[], int],
    ) -> None:
        super().__init__()
        self._settings = settings_repo
        self._runtime = runtime_repo
        self._events = events
        self._clock = clock
        self._timeout_provider = timeout_provider

        state = self._runtime.load()
        self._active = False
        self._last_activity: datetime | None = None
        self._fail_count = int(state.parent_fail_count)
        self._cooldown_until = state.parent_cooldown_until

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        """``True`` while a parent-mode session is open."""
        return self._active

    @property
    def failed_attempts(self) -> int:
        """Number of consecutive failed attempts."""
        return self._fail_count

    @property
    def cooldown_until(self) -> datetime | None:
        """Deadline of the active cooldown, or ``None``."""
        if self._cooldown_until is None:
            return None
        if self._cooldown_until <= self._clock.now_utc():
            return None
        return self._cooldown_until

    def has_password(self) -> bool:
        """``True`` when a parent password has been configured."""
        return bool(self._settings.get(SETTING_PARENT_PASSWORD))

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def try_enter(self, secret: str) -> EnterResult:
        """Attempt to enter parent mode with ``secret``.

        The password hash is checked first; only when it fails is the recovery
        code hash considered (D40).

        Args:
            secret: Plaintext password or recovery code as typed by the parent.

        Returns:
            The :class:`EnterResult` describing what happened.
        """
        now = self._clock.now_utc()

        if self._cooldown_until is not None and now < self._cooldown_until:
            remaining = int((self._cooldown_until - now).total_seconds())
            logger.info("Parent login rejected: cooldown active for %ss", remaining)
            return EnterResult.COOLDOWN

        password_hash = self._settings.get(SETTING_PARENT_PASSWORD)
        if verify_secret(secret, password_hash):
            self._succeed(now)
            self._events.append(
                EventType.PARENT_MODE_ENTER.value,
                EventSeverity.INFO.value,
                {"method": "password"},
                now,
            )
            return EnterResult.OK

        recovery_hash = self._settings.get(SETTING_RECOVERY_CODE)
        if verify_secret(normalize_recovery_code(secret), recovery_hash):
            self._succeed(now)
            self._events.append(
                EventType.RECOVERY_CODE_USED.value,
                EventSeverity.WARNING.value,
                {"method": "recovery_code"},
                now,
            )
            self._events.append(
                EventType.PARENT_MODE_ENTER.value,
                EventSeverity.INFO.value,
                {"method": "recovery_code"},
                now,
            )
            return EnterResult.RECOVERY_USED

        return self._fail(now)

    def _succeed(self, now: datetime) -> None:
        """Open a parent-mode session and clear the failure counters."""
        self._active = True
        self._fail_count = 0
        self._cooldown_until = None
        self._last_activity = now
        self._runtime.patch(parent_fail_count=0, parent_cooldown_until=None)
        logger.info("Parent mode entered")
        self.entered.emit()

    def _fail(self, now: datetime) -> EnterResult:
        """Record a failed attempt and apply the escalation policy."""
        self._fail_count += 1
        self._events.append(
            EventType.PASSWORD_FAILED.value,
            EventSeverity.WARNING.value,
            {"fail_count": self._fail_count},
            now,
        )

        if self._fail_count >= self.EMERGENCY_THRESHOLD:
            # D20/D43: never lock the child out because the parent forgot.
            self._fail_count = 0
            self._cooldown_until = None
            self._runtime.patch(parent_fail_count=0, parent_cooldown_until=None)
            self._events.append(
                EventType.EMERGENCY_GRACE.value,
                EventSeverity.ERROR.value,
                {
                    "minutes": self.EMERGENCY_GRACE_MINUTES,
                    "trigger": "password_failures",
                },
                now,
            )
            logger.warning(
                "Emergency grace granted after %d failed parent logins",
                self.EMERGENCY_THRESHOLD,
            )
            self.emergencyGrace.emit(self.EMERGENCY_GRACE_MINUTES)
            return EnterResult.EMERGENCY_GRACE

        if self._fail_count >= self.COOLDOWN_THRESHOLD:
            self._cooldown_until = now + timedelta(seconds=self.COOLDOWN_SECONDS)
            self._runtime.patch(
                parent_fail_count=self._fail_count,
                parent_cooldown_until=self._cooldown_until,
            )
            logger.info("Parent login cooldown started (%ds)", self.COOLDOWN_SECONDS)
            return EnterResult.COOLDOWN

        self._runtime.patch(parent_fail_count=self._fail_count)
        return EnterResult.WRONG

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def notify_activity(self) -> None:
        """Reset the inactivity timer (called on any parent-panel interaction)."""
        if self._active:
            self._last_activity = self._clock.now_utc()

    def check_timeout(self, now_utc: datetime) -> bool:
        """Exit parent mode when it has been idle for too long.

        Args:
            now_utc: Current UTC time (supplied by the engine tick).

        Returns:
            ``True`` when this call caused an exit.
        """
        if not self._active or self._last_activity is None:
            return False
        try:
            timeout_minutes = max(1, int(self._timeout_provider()))
        except Exception:  # pragma: no cover - defensive
            timeout_minutes = 30
        if now_utc - self._last_activity >= timedelta(minutes=timeout_minutes):
            self.exit(EXIT_TIMEOUT)
            return True
        return False

    def exit(self, reason: str) -> None:
        """Close the parent-mode session.

        Args:
            reason: One of ``timeout``/``session_lock``/``restart``/
                ``day_roll``/``manual``. The UI must close all parent panels
                when :attr:`exited` fires.
        """
        if not self._active:
            return
        self._active = False
        self._last_activity = None
        self._events.append(
            EventType.PARENT_MODE_EXIT.value,
            EventSeverity.INFO.value,
            {"reason": reason},
            self._clock.now_utc(),
        )
        logger.info("Parent mode exited (%s)", reason)
        self.exited.emit(reason)

    # ------------------------------------------------------------------
    # Credential management
    # ------------------------------------------------------------------
    def set_parent_password(self, new_plain: str) -> None:
        """Validate and store a new parent password.

        Args:
            new_plain: The new plaintext password.

        Raises:
            WeakPasswordError: When the password fails the local policy.
        """
        validate_parent_password(new_plain)
        self._settings.set(SETTING_PARENT_PASSWORD, hash_secret(new_plain))
        self._fail_count = 0
        self._cooldown_until = None
        self._runtime.patch(parent_fail_count=0, parent_cooldown_until=None)
        logger.info("Parent password updated")

    def reset_recovery_code(self) -> tuple[str, str]:
        """Generate, store, and return a fresh recovery code.

        Returns:
            A ``(display, normalized)`` tuple. Only ``display`` should be shown
            to the parent, and only once -- the plaintext is never stored.
        """
        display, normalized = generate_recovery_code()
        self._settings.set(SETTING_RECOVERY_CODE, hash_secret(normalized))
        logger.info("Recovery code regenerated")
        return display, normalized


__all__ = [
    "EXIT_DAY_ROLL",
    "EXIT_MANUAL",
    "EXIT_RESTART",
    "EXIT_SESSION_LOCK",
    "EXIT_TIMEOUT",
    "EnterResult",
    "ParentModeController",
]
