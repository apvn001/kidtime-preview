"""键鼠空闲与会话锁定探测（ARCHITECTURE.md §5.11，P0-12）。

Windows 上用 ``GetLastInputInfo`` + ``GetTickCount64`` 计算空闲毫秒数；
会话锁定状态由 `platform/session_events.py` 的 WTS 通知注入，避免轮询。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class IdleSource(Protocol):
    """空闲信息来源协议。"""

    def idle_seconds(self) -> float:
        """返回距上次键鼠输入的秒数。"""
        ...

    def session_locked(self) -> bool:
        """返回 Windows 会话是否处于锁定 / 已切走状态。"""
        ...


class _LASTINPUTINFO(ctypes.Structure):
    """对应 Win32 ``LASTINPUTINFO`` 结构体。"""

    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class Win32IdleSource:
    """基于 Win32 API 的空闲探测。

    非 Windows 平台（或 API 调用失败）时退化为恒定返回 0 秒空闲，
    保证开发机上也能跑通逻辑。
    """

    def __init__(self) -> None:
        """初始化并探测 API 可用性。"""
        self._locked: bool = False
        self._available: bool = sys.platform == "win32"
        self._user32 = None
        self._kernel32 = None
        if self._available:
            try:
                self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                self._kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            except (AttributeError, OSError):  # pragma: no cover - 非 Windows
                logger.warning("无法加载 user32/kernel32，空闲探测退化为恒定 0 秒")
                self._available = False

    def idle_seconds(self) -> float:
        """返回空闲秒数。

        Returns:
            距上次键鼠输入的秒数；API 不可用时返回 ``0.0``。
        """
        if not self._available or self._user32 is None or self._kernel32 is None:
            return 0.0
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        try:
            if not self._user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            now_ms = int(self._kernel32.GetTickCount64())
        except OSError:  # pragma: no cover - 系统调用异常
            logger.debug("GetLastInputInfo 调用失败", exc_info=True)
            return 0.0
        # dwTime 是 32 位 tick，会在 ~49.7 天后回绕，取低 32 位对齐
        last_ms = int(info.dwTime)
        elapsed_ms = (now_ms & 0xFFFFFFFF) - last_ms
        if elapsed_ms < 0:
            elapsed_ms += 0x100000000
        return max(0.0, elapsed_ms / 1000.0)

    def session_locked(self) -> bool:
        """返回会话锁定状态（由 `set_session_locked` 注入）。"""
        return self._locked

    def set_session_locked(self, locked: bool) -> None:
        """更新会话锁定状态。

        Args:
            locked: ``True`` 表示已锁屏 / 会话已切走。
        """
        if self._locked != locked:
            logger.info("Windows 会话锁定状态变更：%s", "锁定" if locked else "解锁")
        self._locked = bool(locked)


class FakeIdleSource:
    """测试用空闲源，可直接设定值。"""

    def __init__(self, idle: float = 0.0, locked: bool = False) -> None:
        """初始化。

        Args:
            idle: 初始空闲秒数。
            locked: 初始会话锁定状态。
        """
        self._idle: float = float(idle)
        self._locked: bool = bool(locked)

    def idle_seconds(self) -> float:
        """返回设定的空闲秒数。"""
        return self._idle

    def session_locked(self) -> bool:
        """返回设定的会话锁定状态。"""
        return self._locked

    def set_idle(self, seconds: float) -> None:
        """设定空闲秒数。"""
        self._idle = float(seconds)

    def set_session_locked(self, locked: bool) -> None:
        """设定会话锁定状态。"""
        self._locked = bool(locked)
