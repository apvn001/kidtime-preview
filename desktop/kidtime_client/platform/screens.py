"""Screen enumeration helpers used by the overlay layer (ARCH §5.19).

``QScreen.name()`` is the stable key the :class:`OverlayManager` uses to map
windows onto monitors, so all lookups funnel through here to keep that choice in
one place.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication, QScreen

logger = logging.getLogger(__name__)


def screen_names(app: QGuiApplication | None = None) -> list[str]:
    """List the names of all attached screens.

    Args:
        app: Application instance; defaults to the running one.

    Returns:
        Screen names in Qt's enumeration order.
    """
    instance = app or QGuiApplication.instance()
    if instance is None:
        return []
    return [screen.name() for screen in instance.screens()]


def screen_by_name(name: str, app: QGuiApplication | None = None) -> QScreen | None:
    """Look up a screen by its Qt name.

    Args:
        name: The value previously returned by ``QScreen.name()``.
        app: Application instance; defaults to the running one.

    Returns:
        The matching screen, or ``None`` when it was unplugged.
    """
    instance = app or QGuiApplication.instance()
    if instance is None:
        return None
    for screen in instance.screens():
        if screen.name() == name:
            return screen
    return None


def primary_screen(app: QGuiApplication | None = None) -> QScreen | None:
    """Return the primary screen.

    Args:
        app: Application instance; defaults to the running one.

    Returns:
        The primary screen, falling back to the first one, or ``None``.
    """
    instance = app or QGuiApplication.instance()
    if instance is None:
        return None
    screen = instance.primaryScreen()
    if screen is not None:
        return screen
    screens = instance.screens()
    return screens[0] if screens else None


def virtual_geometry(app: QGuiApplication | None = None) -> QRect:
    """Return the union of every screen's geometry.

    Args:
        app: Application instance; defaults to the running one.

    Returns:
        The bounding rectangle of the whole desktop (empty when headless).
    """
    instance = app or QGuiApplication.instance()
    if instance is None:
        return QRect()
    rect = QRect()
    for screen in instance.screens():
        rect = rect.united(screen.geometry())
    return rect


__all__ = ["primary_screen", "screen_by_name", "screen_names", "virtual_geometry"]
