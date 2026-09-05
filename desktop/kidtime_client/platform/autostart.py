"""Autostart registration via ``HKCU\\...\\Run`` (ARCHITECTURE.md §5.21).

``HKEY_CURRENT_USER`` is used deliberately: it needs no administrator rights and
scopes the guard to the account it was configured for. Writing to
``HKEY_LOCAL_MACHINE`` would require elevation and affect every user on the PC,
which conflicts with the "family agreement, not surveillance" principle.

On non-Windows hosts all functions are safe no-ops returning ``False``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: Registry value name (also the visible entry name in Task Manager > Startup).
AUTOSTART_VALUE_NAME: Final[str] = "KidTimeClient"

#: Registry key holding per-user startup commands.
RUN_KEY_PATH: Final[str] = r"Software\Microsoft\Windows\CurrentVersion\Run"

_IS_WINDOWS: Final[bool] = sys.platform.startswith("win")


def _launch_command() -> str:
    """Build the command line Windows should run at logon.

    Returns:
        A quoted command line: the frozen executable when running under
        PyInstaller, otherwise ``"<python.exe>" -m kidtime_client``.
    """
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):  # PyInstaller one-file / one-dir
        return f'"{executable}"'
    # Prefer pythonw.exe so a development autostart does not flash a console.
    windowless = executable.with_name("pythonw.exe")
    interpreter = windowless if windowless.exists() else executable
    return f'"{interpreter}" -m kidtime_client'


def is_autostart_enabled(value_name: str = AUTOSTART_VALUE_NAME) -> bool:
    """Check whether the autostart entry exists.

    Args:
        value_name: Registry value name.

    Returns:
        ``True`` when the entry is present (always ``False`` off Windows).
    """
    if not _IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, value_name)
            return bool(str(value).strip())
    except FileNotFoundError:
        return False
    except OSError:  # pragma: no cover - registry unavailable
        logger.debug("Reading the autostart entry failed", exc_info=True)
        return False


def enable_autostart(value_name: str = AUTOSTART_VALUE_NAME) -> bool:
    """Create or refresh the autostart entry.

    Args:
        value_name: Registry value name.

    Returns:
        ``True`` on success.
    """
    if not _IS_WINDOWS:
        logger.debug("Autostart is a no-op on this platform")
        return False
    command = _launch_command()
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
    except OSError:
        logger.exception("Could not enable autostart")
        return False
    logger.info("Autostart enabled: %s", command)
    return True


def disable_autostart(value_name: str = AUTOSTART_VALUE_NAME) -> bool:
    """Remove the autostart entry.

    Args:
        value_name: Registry value name.

    Returns:
        ``True`` when the entry is gone afterwards (including "was not there").
    """
    if not _IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        return True
    except OSError:
        logger.exception("Could not disable autostart")
        return False
    logger.info("Autostart disabled")
    return True


def set_autostart(enabled: bool, value_name: str = AUTOSTART_VALUE_NAME) -> bool:
    """Enable or disable autostart in one call.

    Args:
        enabled: Desired state.
        value_name: Registry value name.

    Returns:
        ``True`` when the requested state was reached.
    """
    return enable_autostart(value_name) if enabled else disable_autostart(value_name)


__all__ = [
    "AUTOSTART_VALUE_NAME",
    "RUN_KEY_PATH",
    "disable_autostart",
    "enable_autostart",
    "is_autostart_enabled",
    "set_autostart",
]
