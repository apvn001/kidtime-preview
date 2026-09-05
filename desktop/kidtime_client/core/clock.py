"""时钟抽象（ARCHITECTURE.md §5.5）。

计时逻辑同时需要**墙钟**（判断日期、宵禁、倒计时）和**单调钟**（累加真实流逝
时间，不受用户改系统时间影响）。两者都通过 :class:`Clock` 协议注入，测试用
:class:`FakeClock` 可让二者独立推进，从而在零真实等待下模拟时间篡改。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """时钟协议。"""

    def now_utc(self) -> datetime:
        """返回当前 UTC 时间（tz-aware）。"""
        ...

    def monotonic(self) -> float:
        """返回单调递增秒数（与墙钟无关）。"""
        ...


class SystemClock:
    """真实系统时钟。"""

    def now_utc(self) -> datetime:
        """返回 `datetime.now(timezone.utc)`。"""
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        """返回 `time.monotonic()`。"""
        return time.monotonic()


class FakeClock:
    """测试专用时钟：墙钟与单调钟可独立推进。

    Attributes:
        wall: 当前墙钟（UTC，tz-aware）。
        mono: 当前单调钟秒数。
    """

    def __init__(self, start_utc: datetime, start_mono: float = 0.0) -> None:
        """初始化。

        Args:
            start_utc: 起始墙钟；naive datetime 会被视作 UTC。
            start_mono: 起始单调钟。
        """
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)
        self.wall: datetime = start_utc.astimezone(timezone.utc)
        self.mono: float = float(start_mono)

    def now_utc(self) -> datetime:
        """返回当前墙钟。"""
        return self.wall

    def monotonic(self) -> float:
        """返回当前单调钟。"""
        return self.mono

    def advance(self, seconds: float, *, wall: float | None = None) -> None:
        """推进时钟。

        Args:
            seconds: 单调钟推进秒数。
            wall: 墙钟推进秒数；``None`` 时与 ``seconds`` 同步推进。
                传入不同的值即可制造漂移（模拟用户改表）。
        """
        self.mono += float(seconds)
        wall_delta = float(seconds) if wall is None else float(wall)
        self.wall = self.wall + timedelta(seconds=wall_delta)

    def set_wall(self, dt: datetime) -> None:
        """直接设定墙钟（单调钟不变，用于模拟跳变改表）。

        Args:
            dt: 目标时间；naive 视作 UTC。
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self.wall = dt.astimezone(timezone.utc)
