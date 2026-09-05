"""Single-instance guard backed by a named Win32 mutex (ARCH §5.21).

Two concurrent clients on the same machine would double-count usage and fight
over the same SQLite file, so the second launch must exit immediately.

A named kernel mutex is used rather than a lock file because Windows releases it
automatically when the process dies -- a hard kill or BSOD cannot leave a stale
lock that blocks every future launch.

On non-Windows hosts the guard falls back to an exclusive lock file so tests can
still exercise the surrounding logic.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Final

logger = logging.getLogger(__name__)

#: Global namespace so the guard also covers multiple RDP/user sessions.
MUTEX_NAME: Final[str] = "Global\\KidTimeClientSingleInstance"

#: ``GetLastError`` code returned when the mutex already exists.
_ERROR_ALREADY_EXISTS: Final[int] = 183

_IS_WINDOWS: Final[bool] = sys.platform.startswith("win")


class SingleInstanceGuard:
    """Acquire a machine-wide lock for the lifetime of the process.

    Args:
        name: Mutex name (defaults to :data:`MUTEX_NAME`).
        fallback_dir: Directory for the non-Windows lock file.

    Example:
        >>> guard = SingleInstanceGuard()
        >>> if not guard.acquire():
        ...     raise SystemExit(0)
    """

    def __init__(self, name: str = MUTEX_NAME, fallback_dir: Path | None = None) -> None:
        self._name = name
        self._fallback_dir = fallback_dir or Path(tempfile.gettempdir())
        self._handle: int | None = None
        self._lock_file: Path | None = None
        self._lock_fd: int | None = None
        self._acquired = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def acquired(self) -> bool:
        """``True`` while this process owns the lock."""
        return self._acquired

    @property
    def name(self) -> str:
        """The mutex / lock-file name in use."""
        return self._name

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------
    def acquire(self) -> bool:
        """Try to become the one and only running instance.

        Returns:
            ``True`` when this process owns the lock, ``False`` when another
            instance already holds it.
        """
        if self._acquired:
            return True
        acquired = self._acquire_windows() if _IS_WINDOWS else self._acquire_posix()
        self._acquired = acquired
        if acquired:
            logger.debug("Single-instance lock acquired (%s)", self._name)
        else:
            logger.warning("Another KidTime client instance is already running")
        return acquired

    def release(self) -> None:
        """Release the lock (idempotent)."""
        if _IS_WINDOWS:
            self._release_windows()
        else:
            self._release_posix()
        self._acquired = False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    def __enter__(self) -> "SingleInstanceGuard":
        """Acquire on entry."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release on exit."""
        self.release()

    # ------------------------------------------------------------------
    # Windows implementation
    # ------------------------------------------------------------------
    def _acquire_windows(self) -> bool:
        """Create the named mutex; report whether we are the first owner."""
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (
                wintypes.LPVOID,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            )
            kernel32.CreateMutexW.restype = wintypes.HANDLE

            handle = kernel32.CreateMutexW(None, True, self._name)
            last_error = ctypes.get_last_error()
            if not handle:
                logger.error("CreateMutexW failed (error=%d)", last_error)
                # Fail open: a broken mutex must not stop the guard from running.
                return True
            if last_error == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = int(handle)
            return True
        except Exception:  # pragma: no cover - depends on the host
            logger.exception("Single-instance mutex unavailable; continuing")
            return True

    def _release_windows(self) -> None:
        """Close the mutex handle."""
        if self._handle is None:
            return
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex(ctypes.c_void_p(self._handle))
            kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        except Exception:  # pragma: no cover - best effort
            logger.debug("Releasing the mutex failed", exc_info=True)
        finally:
            self._handle = None

    # ------------------------------------------------------------------
    # POSIX fallback (development / CI only)
    # ------------------------------------------------------------------
    def _acquire_posix(self) -> bool:
        """Use an ``O_EXCL`` lock file containing the owning PID."""
        safe_name = self._name.replace("\\", "_").replace("/", "_")
        path = self._fallback_dir / f"{safe_name}.lock"
        self._lock_file = path
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if self._steal_stale_lock(path):
                return self._acquire_posix()
            return False
        except OSError:  # pragma: no cover - unwritable temp dir
            logger.exception("Could not create the lock file; continuing")
            return True
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._lock_fd = fd
        return True

    @staticmethod
    def _steal_stale_lock(path: Path) -> bool:
        """Remove a lock file whose owning process no longer exists.

        Args:
            path: The lock file to inspect.

        Returns:
            ``True`` when the stale lock was removed.
        """
        try:
            pid = int(path.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass  # stale
            except PermissionError:
                return False  # alive, owned by someone else
            else:
                return False  # alive
        try:
            path.unlink()
        except OSError:  # pragma: no cover - race with another launcher
            return False
        return True

    def _release_posix(self) -> None:
        """Close and remove the lock file."""
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:  # pragma: no cover - best effort
                pass
            self._lock_fd = None
        if self._lock_file is not None:
            try:
                self._lock_file.unlink()
            except OSError:  # pragma: no cover - already gone
                pass
            self._lock_file = None


def already_running(name: str = MUTEX_NAME) -> bool:
    """Convenience probe used by the entry point.

    Args:
        name: Mutex name.

    Returns:
        ``True`` when another instance already holds the lock. The probe
        releases its own handle immediately, so the caller must still create a
        long-lived :class:`SingleInstanceGuard`.
    """
    guard = SingleInstanceGuard(name)
    acquired = guard.acquire()
    guard.release()
    return not acquired


__all__ = ["MUTEX_NAME", "SingleInstanceGuard", "already_running"]
