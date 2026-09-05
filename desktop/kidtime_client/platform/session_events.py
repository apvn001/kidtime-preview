"""Windows session lock/unlock notifications (ARCHITECTURE.md §5.21).

Why this exists: the child locks the screen (Win+L) and walks away. Without a
session notification the client would keep counting until the 5-minute idle
threshold kicks in, silently eating quota. ``WM_WTSSESSION_CHANGE`` tells us the
instant it happens.

Implementation notes:

* A hidden ``QWidget`` supplies the ``HWND`` required by
  ``WTSRegisterSessionNotification``.
* Messages are intercepted with a ``QAbstractNativeEventFilter`` installed on the
  application, so no extra thread or message pump is needed.
* Everything is wrapped defensively: if registration fails the client keeps
  running and simply relies on the idle detector (graceful degradation, G1).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any, Final

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

_IS_WINDOWS: Final[bool] = sys.platform.startswith("win")

#: ``WM_WTSSESSION_CHANGE``
WM_WTSSESSION_CHANGE: Final[int] = 0x02B1
#: ``WM_POWERBROADCAST``
WM_POWERBROADCAST: Final[int] = 0x0218

# wParam values for WM_WTSSESSION_CHANGE
WTS_SESSION_LOCK: Final[int] = 0x7
WTS_SESSION_UNLOCK: Final[int] = 0x8
WTS_CONSOLE_DISCONNECT: Final[int] = 0x3
WTS_CONSOLE_CONNECT: Final[int] = 0x1
WTS_REMOTE_DISCONNECT: Final[int] = 0x5
WTS_REMOTE_CONNECT: Final[int] = 0x4

# wParam values for WM_POWERBROADCAST
PBT_APMSUSPEND: Final[int] = 0x4
PBT_APMRESUMEAUTOMATIC: Final[int] = 0x12
PBT_APMRESUMESUSPEND: Final[int] = 0x7

#: Only notify about the session this process runs in.
NOTIFY_FOR_THIS_SESSION: Final[int] = 0x0

_LOCK_REASONS: Final[frozenset[int]] = frozenset(
    {WTS_SESSION_LOCK, WTS_CONSOLE_DISCONNECT, WTS_REMOTE_DISCONNECT}
)
_UNLOCK_REASONS: Final[frozenset[int]] = frozenset(
    {WTS_SESSION_UNLOCK, WTS_CONSOLE_CONNECT, WTS_REMOTE_CONNECT}
)


class _MSG(ctypes.Structure):
    """Subset of the Win32 ``MSG`` structure we need to read."""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class _NativeFilter(QAbstractNativeEventFilter):
    """Translates raw Win32 messages into :class:`SessionEventListener` signals.

    Args:
        owner: The listener that owns this filter.
    """

    def __init__(self, owner: "SessionEventListener") -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(  # noqa: N802 - Qt naming
        self, event_type: Any, message: Any
    ) -> object:
        """Inspect one native event.

        Args:
            event_type: Qt's platform tag, ``b"windows_generic_MSG"`` on Win32.
            message: Pointer to the ``MSG`` structure.

        Returns:
            ``(False, 0)`` -- the event is always passed on to Qt.
        """
        try:
            tag = bytes(event_type)
        except Exception:  # pragma: no cover - unexpected Qt payload
            return False, 0
        if tag not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
        except Exception:  # pragma: no cover - defensive
            return False, 0

        if msg.message == WM_WTSSESSION_CHANGE:
            self._owner.handle_session_change(int(msg.wParam))
        elif msg.message == WM_POWERBROADCAST:
            self._owner.handle_power_broadcast(int(msg.wParam))
        return False, 0


class SessionEventListener(QObject):
    """Emits Qt signals for session lock/unlock and sleep/resume.

    Args:
        parent: Optional Qt parent.

    Attributes:
        sessionLocked: The workstation was locked or disconnected.
        sessionUnlocked: The workstation was unlocked or reconnected.
        systemSuspending: The machine is about to sleep/hibernate.
        systemResumed: The machine woke up (the engine must re-read the clock).
    """

    sessionLocked = Signal()
    sessionUnlocked = Signal()
    systemSuspending = Signal()
    systemResumed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window: QWidget | None = None
        self._filter: _NativeFilter | None = None
        self._registered = False
        self._hwnd: int | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def registered(self) -> bool:
        """``True`` when Win32 session notifications are active."""
        return self._registered

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, app: Any) -> bool:
        """Install the native filter and register for notifications.

        Args:
            app: The running ``QApplication``.

        Returns:
            ``True`` when notifications were registered. ``False`` means the
            client keeps working using idle detection only.
        """
        if self._registered:
            return True
        if not _IS_WINDOWS:
            logger.debug("Session notifications unavailable on this platform")
            return False
        try:
            self._window = QWidget()
            self._window.setWindowTitle("KidTimeSessionSink")
            self._window.resize(0, 0)
            # A native handle must exist before WTSRegisterSessionNotification.
            self._hwnd = int(self._window.winId())

            wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
            wtsapi32.WTSRegisterSessionNotification.argtypes = (
                ctypes.c_void_p,
                ctypes.c_uint,
            )
            wtsapi32.WTSRegisterSessionNotification.restype = ctypes.c_bool
            ok = bool(
                wtsapi32.WTSRegisterSessionNotification(
                    ctypes.c_void_p(self._hwnd), NOTIFY_FOR_THIS_SESSION
                )
            )
            if not ok:
                logger.warning(
                    "WTSRegisterSessionNotification failed (error=%d)",
                    ctypes.get_last_error(),
                )
                self._cleanup_window()
                return False

            self._filter = _NativeFilter(self)
            app.installNativeEventFilter(self._filter)
            self._registered = True
            logger.info("Windows session notifications registered")
            return True
        except Exception:  # pragma: no cover - depends on the host
            logger.exception("Could not register session notifications")
            self._cleanup_window()
            return False

    def stop(self, app: Any = None) -> None:
        """Unregister notifications and drop the hidden window.

        Args:
            app: The application the filter was installed on (optional).
        """
        if self._registered and _IS_WINDOWS:
            try:
                wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
                wtsapi32.WTSUnRegisterSessionNotification.argtypes = (ctypes.c_void_p,)
                wtsapi32.WTSUnRegisterSessionNotification(ctypes.c_void_p(self._hwnd))
            except Exception:  # pragma: no cover - best effort
                logger.debug("WTSUnRegisterSessionNotification failed", exc_info=True)
        if app is not None and self._filter is not None:
            try:
                app.removeNativeEventFilter(self._filter)
            except Exception:  # pragma: no cover - best effort
                logger.debug("removeNativeEventFilter failed", exc_info=True)
        self._filter = None
        self._registered = False
        self._cleanup_window()

    def _cleanup_window(self) -> None:
        """Destroy the hidden message-sink window."""
        if self._window is not None:
            self._window.deleteLater()
            self._window = None
        self._hwnd = None

    # ------------------------------------------------------------------
    # Message handling (also callable directly from tests)
    # ------------------------------------------------------------------
    def handle_session_change(self, reason: int) -> None:
        """Translate a ``WM_WTSSESSION_CHANGE`` ``wParam`` into a signal.

        Args:
            reason: One of the ``WTS_*`` constants.
        """
        if reason in _LOCK_REASONS:
            logger.info("Session locked (reason=%d)", reason)
            self.sessionLocked.emit()
        elif reason in _UNLOCK_REASONS:
            logger.info("Session unlocked (reason=%d)", reason)
            self.sessionUnlocked.emit()

    def handle_power_broadcast(self, event: int) -> None:
        """Translate a ``WM_POWERBROADCAST`` ``wParam`` into a signal.

        Args:
            event: One of the ``PBT_*`` constants.
        """
        if event == PBT_APMSUSPEND:
            logger.info("System suspending")
            self.systemSuspending.emit()
        elif event in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            logger.info("System resumed")
            self.systemResumed.emit()


__all__ = [
    "PBT_APMRESUMEAUTOMATIC",
    "PBT_APMSUSPEND",
    "SessionEventListener",
    "WM_POWERBROADCAST",
    "WM_WTSSESSION_CHANGE",
    "WTS_SESSION_LOCK",
    "WTS_SESSION_UNLOCK",
]
