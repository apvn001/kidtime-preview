"""规则快照与时段计算（ARCHITECTURE.md §5.7）。

:class:`RuleSnapshot` 是**不可变**的规则副本：离线时客户端完全依赖它做判定
（D45）。字段名与后端 `RuleProfileOut` 保持一致，便于直接 `from_dict`。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from kidtime_client.constants import (
    DEFAULT_ALLOWED_END,
    DEFAULT_ALLOWED_START,
    DEFAULT_BREAK_MINUTES,
    DEFAULT_CONTINUOUS_LIMIT_MINUTES,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_PARENT_MODE_TIMEOUT_MINUTES,
    DEFAULT_REMINDER_POINTS,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    DEFAULT_WEEKDAY_QUOTA_MINUTES,
    DEFAULT_WEEKEND_QUOTA_MINUTES,
)

_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str, fallback: str) -> time:
    """把 ``HH:MM`` 解析成 `datetime.time`，非法时用回退值。

    Args:
        value: 待解析字符串。
        fallback: 回退的 ``HH:MM``（必须合法）。

    Returns:
        解析结果。
    """
    text = (value or "").strip()
    if not _HHMM_RE.match(text):
        text = fallback
    hour, minute = text.split(":")
    return time(hour=int(hour), minute=int(minute))


@dataclass(frozen=True)
class RuleSnapshot:
    """一份完整的规则快照（离线判定的唯一依据）。"""

    weekday_quota_minutes: int = DEFAULT_WEEKDAY_QUOTA_MINUTES
    weekend_quota_minutes: int = DEFAULT_WEEKEND_QUOTA_MINUTES
    allowed_start: str = DEFAULT_ALLOWED_START
    allowed_end: str = DEFAULT_ALLOWED_END
    continuous_limit_minutes: int = DEFAULT_CONTINUOUS_LIMIT_MINUTES
    break_minutes: int = DEFAULT_BREAK_MINUTES
    idle_minutes: int = DEFAULT_IDLE_MINUTES
    reminder_points: tuple[int, ...] = field(default=DEFAULT_REMINDER_POINTS)
    parent_mode_timeout_minutes: int = DEFAULT_PARENT_MODE_TIMEOUT_MINUTES
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS
    enforcement_enabled: bool = True
    version: int = 1

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RuleSnapshot":
        """从后端下发的规则字典构造（未知字段忽略，缺失字段用默认值）。

        Args:
            d: `RuleProfileOut` 的 JSON 字典；``None`` 返回全默认快照。

        Returns:
            规则快照。
        """
        if not d:
            return cls()
        defaults = cls()

        def _int(key: str, fallback: int) -> int:
            raw = d.get(key, fallback)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return fallback

        def _str(key: str, fallback: str) -> str:
            raw = d.get(key, fallback)
            text = str(raw).strip() if raw is not None else ""
            return text if _HHMM_RE.match(text) else fallback

        points_raw = d.get("reminder_points", defaults.reminder_points)
        try:
            points = tuple(sorted({int(item) for item in points_raw}, reverse=True))
        except (TypeError, ValueError):
            points = defaults.reminder_points
        if not points:
            points = defaults.reminder_points

        return cls(
            weekday_quota_minutes=_int("weekday_quota_minutes", defaults.weekday_quota_minutes),
            weekend_quota_minutes=_int("weekend_quota_minutes", defaults.weekend_quota_minutes),
            allowed_start=_str("allowed_start", defaults.allowed_start),
            allowed_end=_str("allowed_end", defaults.allowed_end),
            continuous_limit_minutes=_int(
                "continuous_limit_minutes", defaults.continuous_limit_minutes
            ),
            break_minutes=_int("break_minutes", defaults.break_minutes),
            idle_minutes=_int("idle_minutes", defaults.idle_minutes),
            reminder_points=points,
            parent_mode_timeout_minutes=_int(
                "parent_mode_timeout_minutes", defaults.parent_mode_timeout_minutes
            ),
            sync_interval_seconds=_int("sync_interval_seconds", defaults.sync_interval_seconds),
            enforcement_enabled=bool(d.get("enforcement_enabled", defaults.enforcement_enabled)),
            version=_int("version", defaults.version),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典（``reminder_points`` 转 list）。"""
        data = asdict(self)
        data["reminder_points"] = list(self.reminder_points)
        return data


def base_quota_for(rules: RuleSnapshot, local_day: date) -> int:
    """返回指定本地日期的基础配额分钟（D13）。

    Args:
        rules: 规则快照。
        local_day: 本地业务日期。

    Returns:
        周一至周五返回 ``weekday_quota_minutes``，周六日返回 ``weekend_quota_minutes``。
    """
    return (
        rules.weekday_quota_minutes
        if local_day.weekday() < 5
        else rules.weekend_quota_minutes
    )


def is_within_allowed_window(rules: RuleSnapshot, local_time: time) -> bool:
    """判断本地时刻是否落在允许时段 ``[start, end)`` 内（D14）。

    ``end <= start`` 视为跨零点，等价于 ``[start, 24:00) ∪ [00:00, end)``。
    ``end == start`` 表示全天允许。

    Args:
        rules: 规则快照。
        local_time: 本地时刻。

    Returns:
        在允许时段内返回 ``True``。
    """
    start = parse_hhmm(rules.allowed_start, DEFAULT_ALLOWED_START)
    end = parse_hhmm(rules.allowed_end, DEFAULT_ALLOWED_END)
    if start == end:
        return True
    if start < end:
        return start <= local_time < end
    # 跨零点
    return local_time >= start or local_time < end


def next_allowed_datetime(rules: RuleSnapshot, local_now: datetime) -> datetime:
    """返回下一次进入允许时段的本地时刻（遮罩上展示"X 点可再用"）。

    若当前已在允许时段内，返回下一个周期的开始时刻。

    Args:
        rules: 规则快照。
        local_now: 本地当前时间（可为 naive 或 aware，返回值保持同种类）。

    Returns:
        下一次允许使用的本地 datetime。
    """
    start = parse_hhmm(rules.allowed_start, DEFAULT_ALLOWED_START)
    end = parse_hhmm(rules.allowed_end, DEFAULT_ALLOWED_END)
    if start == end:
        # 全天允许，退化为下一个整日开始
        return datetime.combine(
            local_now.date() + timedelta(days=1), time(0, 0), tzinfo=local_now.tzinfo
        )

    today_start = datetime.combine(local_now.date(), start, tzinfo=local_now.tzinfo)
    if local_now < today_start:
        return today_start
    if start < end:
        # 同日时段：已过今日 start（可能在窗口内或窗口后）→ 明日 start
        return today_start + timedelta(days=1)
    # 跨零点时段：now >= start 说明已在窗口内 → 明日 start
    return today_start + timedelta(days=1)
