"""时间守卫：单调钟增量钳制 + 改表检测 + 生效日不回退（ARCHITECTURE.md §5.6）。

🔴 三条不可动摇的规则：

1. **增量钳制**（D16）：单次 tick 的计时增量钳制在 ``[0, max_delta_seconds]``，
   休眠/卡顿导致的大跳变不会一次性吃掉孩子的配额。
2. **改表检测**（D15）：墙钟增量与单调钟增量之差超过 ±60s 视为篡改，
   记 `TIME_TAMPER` 事件。
3. **生效日不回退**：``effective_date = max(系统日期, 上次生效日)``，
   把系统时间调回昨天不会刷新今天的配额。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from kidtime_client.core.clock import Clock


class TamperKind(str, Enum):
    """时间篡改方向。"""

    BACKWARD = "BACKWARD"
    FORWARD = "FORWARD"


@dataclass(frozen=True)
class TimeReading:
    """一次时间读数。

    Attributes:
        wall_utc: 墙钟 UTC 时间（原始读数，仅供记录/日志）。
        trusted_now_utc: 经篡改拦截后的可信 ``now``。检测到跳变时只前进真实
            单调增量，不采信跳变后的墙钟；引擎一律用它判定休息到期与业务日切。
        monotonic: 单调钟当前值。
        monotonic_delta: 已钳制到 ``[0, max_delta_seconds]`` 的计时增量。
        raw_monotonic_delta: 未钳制的单调钟增量。
        wall_delta_seconds: 墙钟增量。
        drift_seconds: ``wall_delta - raw_monotonic_delta``。
        tamper: 篡改方向；未检测到为 ``None``。
        tamper_blocked: 本 tick 是否拦截了墙钟跳变（不采信跳变后的 ``now``）。
    """

    wall_utc: datetime
    trusted_now_utc: datetime
    monotonic: float
    monotonic_delta: float
    raw_monotonic_delta: float
    wall_delta_seconds: float
    drift_seconds: float
    tamper: TamperKind | None
    tamper_blocked: bool


class TimeGuard:
    """把系统时间转换成可信的计时增量与业务日期。

    Attributes:
        max_delta_seconds: 单次 tick 增量上限（默认 2.0s）。
        tamper_threshold_seconds: 判定改表的漂移阈值（默认 60s）。
    """

    def __init__(
        self,
        clock: Clock,
        max_delta_seconds: float = 2.0,
        tamper_threshold_seconds: float = 60.0,
    ) -> None:
        """初始化。

        Args:
            clock: 时钟。
            max_delta_seconds: 增量钳制上限。
            tamper_threshold_seconds: 改表判定阈值。
        """
        self._clock: Clock = clock
        self.max_delta_seconds: float = float(max_delta_seconds)
        self.tamper_threshold_seconds: float = float(tamper_threshold_seconds)
        self._last_mono: float | None = None
        self._last_wall: datetime | None = None

    def read(self) -> TimeReading:
        """读取一次时间并计算增量与篡改标记。

        首次调用（或 :meth:`reset` 之后的首次）增量恒为 0，且不会误报篡改。

        Returns:
            本次读数。
        """
        wall = self._clock.now_utc()
        mono = self._clock.monotonic()

        if self._last_mono is None or self._last_wall is None:
            self._last_mono = mono
            self._last_wall = wall
            return TimeReading(
                wall_utc=wall,
                trusted_now_utc=wall,
                monotonic=mono,
                monotonic_delta=0.0,
                raw_monotonic_delta=0.0,
                wall_delta_seconds=0.0,
                drift_seconds=0.0,
                tamper=None,
                tamper_blocked=False,
            )

        raw_delta = mono - self._last_mono
        wall_delta = (wall - self._last_wall).total_seconds()
        drift = wall_delta - raw_delta

        tamper: TamperKind | None = None
        if drift < -self.tamper_threshold_seconds:
            tamper = TamperKind.BACKWARD
        elif drift > self.tamper_threshold_seconds:
            tamper = TamperKind.FORWARD

        clamped = min(max(raw_delta, 0.0), self.max_delta_seconds)

        # 篡改拦截（D15 升级）：单调钟不可被改系统时间影响，故它是可信基准。
        # 检测到跳变时，**不采信**跳变后的墙钟——``trusted_now`` 只前进真实
        # 单调增量，并把基线重锚到清洗值（而非跳变后的墙钟），这样后续每 tick
        # 仍持续重判、持续阻断，直到钟被恢复；孩子无法用改表快进强制休息或
        # 刷新配额。无篡改时 ``trusted_now`` 即真实墙钟。
        if tamper is not None:
            trusted = self._last_wall + timedelta(seconds=raw_delta)
            tamper_blocked = True
        else:
            trusted = wall
            tamper_blocked = False

        self._last_mono = mono
        self._last_wall = trusted
        return TimeReading(
            wall_utc=wall,
            trusted_now_utc=trusted,
            monotonic=mono,
            monotonic_delta=clamped,
            raw_monotonic_delta=raw_delta,
            wall_delta_seconds=wall_delta,
            drift_seconds=drift,
            tamper=tamper,
            tamper_blocked=tamper_blocked,
        )

    def effective_date(self, tz: ZoneInfo, last_effective_date: date | None) -> date:
        """计算业务生效日，**强制不回退**（D15）。

        Args:
            tz: 设备本地时区。
            last_effective_date: 上次持久化的生效日；``None`` 表示首次运行。

        Returns:
            ``max(系统本地日期, last_effective_date)``。
        """
        system_date = self._clock.now_utc().astimezone(tz).date()
        if last_effective_date is None:
            return system_date
        return max(system_date, last_effective_date)

    def reset(self) -> None:
        """重置基线（休眠唤醒、会话切换后调用），下一 tick 增量归零。"""
        self._last_mono = None
        self._last_wall = None
