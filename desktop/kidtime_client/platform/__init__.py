"""Windows platform integration.

Every module in this package is **import-safe on any OS**: the Win32 specific
parts are guarded so the client (and its test-suite) can be imported and largely
exercised on Linux/macOS CI runners. On non-Windows hosts the helpers degrade to
no-ops that report failure instead of raising.

Note:
    ``kidtime_client.platform`` never shadows the standard-library ``platform``
    module: Python 3 resolves bare ``import platform`` absolutely.
"""

from __future__ import annotations

from kidtime_client.platform.autostart import (
    AUTOSTART_VALUE_NAME,
    disable_autostart,
    enable_autostart,
    is_autostart_enabled,
    set_autostart,
)
from kidtime_client.platform.screens import (
    primary_screen,
    screen_by_name,
    screen_names,
    virtual_geometry,
)
from kidtime_client.platform.session_events import SessionEventListener
from kidtime_client.platform.single_instance import (
    SingleInstanceGuard,
    already_running,
)

__all__ = [
    "AUTOSTART_VALUE_NAME",
    "SessionEventListener",
    "SingleInstanceGuard",
    "already_running",
    "disable_autostart",
    "enable_autostart",
    "is_autostart_enabled",
    "primary_screen",
    "screen_by_name",
    "screen_names",
    "set_autostart",
    "virtual_geometry",
]
