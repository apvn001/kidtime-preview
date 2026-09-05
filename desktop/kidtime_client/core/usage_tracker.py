"""当日用量累计与节流落盘（ARCHITECTURE.md §5.9，D34）。

计时以**秒**为单位在内存累加，每 30 秒（或强制）落盘一次，避免每秒写 SQLite。
上报给服务端时再向下取整成分钟。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from kidtime_client.core.clock import Clock

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免循环导入
    from kidtime_client.storage.repositories import UsageRepository

logger = logging.getLogger(__name__)


@dataclass
class DailyUsageRecord:
    """某一天的用量记录（内存表示，秒级精度）。

    Attributes:
        usage_date: 业务日期。
        used_seconds: 孩子已用秒数。
        bonus_minutes: 当日额外额度分钟。
        parent_seconds: 家长模式秒数。
        break_count: 强制休息次数。
        base_quota_minutes: 当日基础配额快照。
    """

    usage_date: date
    used_seconds: float = 0.0
    bonus_minutes: int = 0
    parent_seconds: float = 0.0
    break_count: int = 0
    base_quota_minutes: int = 0

    @property
    def used_minutes(self) -> int:
        """孩子已用分钟（向下取整）。"""
        return int(self.used_seconds // 60)

    @property
    def parent_minutes(self) -> int:
        """家长模式分钟（向下取整）。"""
        return int(self.parent_seconds // 60)

    @property
    def effective_quota_minutes(self) -> int:
        """有效配额 = 基础配额 + 额度。"""
        return self.base_quota_minutes + self.bonus_minutes

    @property
    def remaining_minutes(self) -> float:
        """剩余分钟（可能带小数，用于精确提醒判定）。"""
        return max(0.0, self.effective_quota_minutes - self.used_seconds / 60.0)


class UsageTracker:
    """当日用量累加器。

    Attributes:
        flush_interval_seconds: 节流落盘间隔（默认 30s，D34）。
    """

    def __init__(
        self,
        repo: "UsageRepository",
        clock: Clock,
        flush_interval_seconds: int = 30,
    ) -> None:
        """初始化。

        Args:
            repo: 用量仓储。
            clock: 时钟。
            flush_interval_seconds: 落盘节流间隔秒。
        """
        self._repo = repo
        self._clock = clock
        self.flush_interval_seconds: int = int(flush_interval_seconds)
        self._current: DailyUsageRecord = DailyUsageRecord(usage_date=date.min)
        self._last_flush_mono: float = clock.monotonic()
        self._dirty: bool = False

    @property
    def current(self) -> DailyUsageRecord:
        """当前生效的当日记录。"""
        return self._current

    def load(self, usage_date: date, base_quota: int) -> DailyUsageRecord:
        """读取或创建指定日期的记录并设为当前记录。

        Args:
            usage_date: 业务日期。
            base_quota: 该日基础配额分钟（新建时写入；已存在时按最新规则刷新）。

        Returns:
            当前记录。
        """
        record = self._repo.get(usage_date)
        if record is None:
            record = DailyUsageRecord(usage_date=usage_date, base_quota_minutes=base_quota)
            self._repo.upsert(record)
        elif record.base_quota_minutes != base_quota:
            # 家长改了规则 → 当日基础配额跟随最新规则（D13）
            record.base_quota_minutes = base_quota
            self._repo.upsert(record)
        self._current = record
        self._last_flush_mono = self._clock.monotonic()
        self._dirty = False
        return record

    def add_child_seconds(self, seconds: float) -> None:
        """累加孩子使用秒数。

        Args:
            seconds: 增量秒数（<=0 忽略）。
        """
        if seconds <= 0:
            return
        self._current.used_seconds += float(seconds)
        self._dirty = True

    def add_parent_seconds(self, seconds: float) -> None:
        """累加家长模式秒数。

        Args:
            seconds: 增量秒数（<=0 忽略）。
        """
        if seconds <= 0:
            return
        self._current.parent_seconds += float(seconds)
        self._dirty = True

    def add_bonus_minutes(self, minutes: int) -> None:
        """累加当日额度（仅供 `CreditService` 在事务内回调，勿直接调用）。

        Args:
            minutes: 批准分钟数。
        """
        if minutes <= 0:
            return
        self._current.bonus_minutes += int(minutes)
        self._dirty = True

    def deduct_today(self, minutes: int) -> None:
        """按 DEDUCT_USAGE 指令扣减当日用量（V7 功能三，服务端已立即生效）。

        扣减的本质是「把已用视为增加 minutes」：本地 ``used_seconds += minutes*60``，
        ``remaining`` 随之下降。服务端在 ``deduct()`` 内已把 ``used_minutes += applied``
        作为唯一真相源，此处仅为本地冗余同步；ack 后服务端不再二次写库（避免双倍）。

        Args:
            minutes: 扣减分钟数（服务端已钳制，>0 才会下发指令；<=0 忽略）。
        """
        if minutes <= 0:
            return
        self._current.used_seconds += float(minutes) * 60
        self._dirty = True

    def increment_break_count(self) -> None:
        """强制休息次数 +1。"""
        self._current.break_count += 1
        self._dirty = True

    def maybe_flush(self, *, force: bool = False) -> bool:
        """距上次落盘满间隔或 ``force`` 时写库。

        Args:
            force: 强制落盘（退出、日切、同步前调用）。

        Returns:
            是否实际写库。
        """
        if self._current.usage_date == date.min:
            return False
        now_mono = self._clock.monotonic()
        due = (now_mono - self._last_flush_mono) >= self.flush_interval_seconds
        if not force and (not due or not self._dirty):
            return False
        if not force and not self._dirty:
            return False
        self._repo.upsert(self._current)
        self._last_flush_mono = now_mono
        self._dirty = False
        return True

    def rollover(self, new_date: date, base_quota: int) -> DailyUsageRecord:
        """日切：强制落盘昨日 → 载入/新建新日记录（D17）。

        Args:
            new_date: 新的业务日期。
            base_quota: 新日期的基础配额分钟。

        Returns:
            新的当日记录。
        """
        self.maybe_flush(force=True)
        logger.info("业务日切：%s → %s", self._current.usage_date, new_date)
        return self.load(new_date, base_quota)

    def refresh_bonus_from_db(self) -> None:
        """重新从库里读取当日 `bonus_minutes`（`CreditService` 在事务里改过库）。"""
        if self._current.usage_date == date.min:
            return
        latest = self._repo.get(self._current.usage_date)
        if latest is not None:
            self._current.bonus_minutes = latest.bonus_minutes

    def reset_today(self, target_date: date, *, include_bonus: bool = False) -> None:
        """按 RESET_USAGE 指令清零指定日期的用量（方案 A：本地清零在收到指令时执行）。

        Args:
            target_date: 指令指定的业务日期（通常为今日）。
            include_bonus: 是否同时清零已批准额度（默认 ``False``，保留额度）。
        """
        self._repo.reset(target_date, include_bonus=include_bonus)
        if self._current.usage_date == target_date:
            self._current.used_seconds = 0.0
            self._current.parent_seconds = 0.0
            self._current.break_count = 0
            if include_bonus:
                self._current.bonus_minutes = 0
            self._dirty = True

    def pending_upload(self, limit: int = 7) -> list[DailyUsageRecord]:
        """返回最近 N 天中需要上报的记录。

        Args:
            limit: 最多返回条数（服务端上限 7，API.md §7.2.1）。

        Returns:
            待上报记录列表，按日期升序。
        """
        return self._repo.pending_upload(limit=limit)

    def mark_uploaded(self, dates: list[date], at: datetime) -> None:
        """标记指定日期已同步。

        Args:
            dates: 已被服务端接收的日期。
            at: 同步时刻。
        """
        if dates:
            self._repo.mark_synced(dates, at)
