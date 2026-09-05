"""🔴 额度入账服务：幂等的唯一保证（ARCHITECTURE.md §5.10 / §7.5，D31/C5）。

**顺序不可调换**：

1. ``BEGIN IMMEDIATE`` —— 一次拿到写锁；
2. ``INSERT OR IGNORE credited_ledger`` —— ``rowcount == 0`` 说明已入过账，
   立刻 ``ROLLBACK`` 返回 ``ALREADY``，**绝不重复加时**；
3. ``UPDATE daily_usage.bonus_minutes``；
4. ``COMMIT`` —— 原子边界到此结束；
5. **提交之后**才产生"待确认"（事件 + outbox），确认失败不影响已入账事实。

崩溃恢复只会重发确认（`unconfirmed_request_ids`），永远不会重复加时。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Sequence

from kidtime_client.constants import EventSeverity, EventType, OutboxKind
from kidtime_client.core.clock import Clock

if TYPE_CHECKING:  # pragma: no cover
    from kidtime_client.storage.database import LocalDatabase
    from kidtime_client.storage.repositories import (
        EventRepository,
        LedgerRepository,
        OutboxRepository,
        UsageRepository,
    )

logger = logging.getLogger(__name__)


class CreditResult(str, Enum):
    """入账结果。"""

    CREDITED = "CREDITED"
    """本次真实入账。"""

    ALREADY = "ALREADY"
    """`request_id` 已存在，未重复加时。"""

    DATE_MISMATCH = "DATE_MISMATCH"
    """`target_date` 与当前生效日不符，按 D27 丢弃。"""


class CreditService:
    """把服务端下发的批准额度安全地写进本地当日配额。"""

    def __init__(
        self,
        db: "LocalDatabase",
        ledger: "LedgerRepository",
        usage: "UsageRepository",
        clock: Clock,
        events: "EventRepository | None" = None,
        outbox: "OutboxRepository | None" = None,
    ) -> None:
        """初始化。

        Args:
            db: 本地数据库。
            ledger: 入账台账仓储。
            usage: 用量仓储。
            clock: 时钟。
            events: 事件仓储（可选；缺省时不落事件，便于纯单测）。
            outbox: 发件箱仓储（可选；缺省时不排队确认）。
        """
        self._db = db
        self._ledger = ledger
        self._usage = usage
        self._clock = clock
        self._events = events
        self._outbox = outbox

    def credit(
        self,
        request_id: str,
        minutes: int,
        target_date: date,
        effective_date: date,
    ) -> CreditResult:
        """执行一次入账。

        Args:
            request_id: 申请 id（= 幂等键）。
            minutes: 批准分钟数。
            target_date: 服务端指定的目标日期。
            effective_date: 客户端当前业务日期。

        Returns:
            入账结果。

        Raises:
            sqlite3.Error: 数据库层异常（调用方需捕获并记 error 事件）。
        """
        # ① 跨天作废（D27，放宽至「昨日」宽限期）
        # 后端 grant_service.list_deliverable 仅下发 target_date >= today-1 的额度，
        # 故客户端至多见到「今日」与「昨日」两条。昨日额度仍应入账至今日
        # （跨零点、管理员浏览器时区与设备时区不一致、隔日才同步等场景），
        # 否则会静默丢弃，表现为「下发时长不生效」。仅当额度早于昨日才作废。
        if target_date < effective_date - timedelta(days=1) or target_date > effective_date:
            logger.warning(
                "额度跨天作废：request_id=%s target_date=%s effective_date=%s",
                request_id,
                target_date,
                effective_date,
            )
            self._append_event(
                EventSeverity.WARNING.value,
                {
                    "request_id": request_id,
                    "result": CreditResult.DATE_MISMATCH.value,
                    "target_date": target_date.isoformat(),
                    "effective_date": effective_date.isoformat(),
                },
            )
            return CreditResult.DATE_MISMATCH

        conn = self._db.connect()
        now = self._clock.now_utc()
        now_iso = now.isoformat()

        conn.execute("BEGIN IMMEDIATE")  # 🔴 写锁一次拿到
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO credited_ledger"
                " (request_id, minutes, target_date, credited_at, confirmed_at)"
                " VALUES (?, ?, ?, ?, NULL)",
                (request_id, int(minutes), target_date.isoformat(), now_iso),
            )
            already = cursor.rowcount == 0
            cursor.close()
            if already:
                # 🔴 已入过账：立刻回滚，绝不重复加时（C5）
                conn.execute("ROLLBACK")
                logger.info("额度重复下发，已忽略：request_id=%s", request_id)
                return CreditResult.ALREADY

            conn.execute(
                "UPDATE daily_usage SET bonus_minutes = bonus_minutes + ?, updated_at = ?"
                " WHERE usage_date = ?",
                (int(minutes), now_iso, effective_date.isoformat()),
            )
            conn.execute("COMMIT")  # 🔴 原子边界到此结束
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                logger.exception("额度入账回滚失败：request_id=%s", request_id)
            raise

        # ② 提交成功之后才产生"待确认"
        logger.info("额度已入账：request_id=%s minutes=%d", request_id, minutes)
        self._append_event(
            EventSeverity.INFO.value,
            {"request_id": request_id, "minutes": int(minutes)},
        )
        if self._outbox is not None:
            self._outbox.enqueue(
                request_id, OutboxKind.CREDIT_CONFIRM.value, {"request_id": request_id}
            )
        return CreditResult.CREDITED

    def unconfirmed_request_ids(self) -> list[str]:
        """返回已入账但尚未被服务端确认的 `request_id`。

        启动时与每次构造 `SyncRequest` 前调用（C5/D31）。崩溃恢复路径：
        事务已提交但 ``confirmed_at`` 仍为 NULL → **只补发确认**。

        Returns:
            `request_id` 列表。
        """
        return self._ledger.unconfirmed()

    def mark_confirmed(self, request_ids: Sequence[str]) -> None:
        """服务端确认成功后回写 ``confirmed_at``。

        Args:
            request_ids: 已确认的申请 id。
        """
        if not request_ids:
            return
        self._ledger.mark_confirmed(request_ids, self._clock.now_utc())

    # ------------------------------------------------------------ 内部
    def _append_event(self, severity: str, payload: dict) -> None:
        """写一条 `EXTENSION_CREDITED` 事件（事件仓储缺省时静默跳过）。"""
        if self._events is None:
            return
        self._events.append(
            EventType.EXTENSION_CREDITED.value,
            severity,
            payload,
            self._clock.now_utc(),
        )
