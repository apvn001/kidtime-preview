"""本地库仓储层（ARCHITECTURE.md §5.14）。

🔴 **Single-Writer**：本模块的所有写方法只允许在 GUI 主线程调用。
`SyncWorker` 只负责 HTTP，绝不引用任何 Repository 实例（§9.4 R2/R4）。

时间统一以 ISO-8601（UTC，tz-aware）字符串存储，读取时还原为 aware datetime。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Final, Sequence

from kidtime_client.constants import (
    OutboxKind,
    OutboxStatus,
    SETTING_RULES_JSON,
    SETTING_RULE_VERSION,
)
from kidtime_client.core.rules import RuleSnapshot
from kidtime_client.core.state_machine import State
from kidtime_client.core.usage_tracker import DailyUsageRecord
from kidtime_client.storage.database import LocalDatabase

logger = logging.getLogger(__name__)

OUTBOX_BACKOFF_SECONDS: Final[tuple[int, ...]] = (45, 90, 180, 300)
"""发件箱重试退避（D46）。"""

OUTBOX_MAX_RETRY: Final[int] = 20
"""事件条目超过该重试次数后丢弃，防止无限堆积（§7.6）。"""


# ================================================================== 工具
def _iso(value: datetime | None) -> str | None:
    """把 aware datetime 序列化成 ISO 字符串。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(raw: Any) -> datetime | None:
    """把 ISO 字符串还原成 aware datetime，非法值返回 ``None``。"""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date(raw: Any) -> date | None:
    """把 ``YYYY-MM-DD`` 还原成 date，非法值返回 ``None``。"""
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _loads(raw: Any, fallback: Any) -> Any:
    """安全的 JSON 反序列化。"""
    if raw in (None, ""):
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


# ================================================================== 数据结构
@dataclass
class RuntimeState:
    """`runtime_state` 单行的内存映射。"""

    last_effective_date: date | None = None
    last_state: State = State.ACTIVE
    state_changed_at: datetime | None = None
    grace_until: datetime | None = None
    break_end_at: datetime | None = None
    continuous_use_seconds: float = 0.0
    fired_reminders: frozenset[int] = field(default_factory=frozenset)
    parent_fail_count: int = 0
    parent_cooldown_until: datetime | None = None
    last_flush_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_sync_ok: bool = False
    clean_shutdown: bool = True


@dataclass(frozen=True)
class OutboxItem:
    """发件箱条目。"""

    id: str
    kind: str
    payload: dict
    status: str
    retry_count: int
    next_retry_at: datetime | None
    last_error: str | None
    created_at: datetime | None
    sent_at: datetime | None


@dataclass(frozen=True)
class LocalEvent:
    """本地事件流水条目。"""

    id: str
    event_type: str
    severity: str
    payload: dict
    occurred_at: datetime
    uploaded_at: datetime | None


# ================================================================== 设置
class SettingsRepository:
    """`local_settings` 键值仓储。"""

    def __init__(self, db: LocalDatabase) -> None:
        """初始化。

        Args:
            db: 本地数据库。
        """
        self._db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        """读取一个字符串设置。"""
        row = self._db.query_one("SELECT value FROM local_settings WHERE key = ?", (key,))
        return str(row["value"]) if row is not None else default

    def set(self, key: str, value: str) -> None:
        """写入一个字符串设置（upsert）。"""
        now = _iso(datetime.now(timezone.utc))
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO local_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, str(value), now),
            )

    def delete(self, key: str) -> None:
        """删除一个设置项。"""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM local_settings WHERE key = ?", (key,))

    def get_json(self, key: str, default: Any = None) -> Any:
        """读取一个 JSON 设置。"""
        return _loads(self.get(key), default)

    def set_json(self, key: str, value: Any) -> None:
        """写入一个 JSON 设置。"""
        self.set(key, json.dumps(value, ensure_ascii=False))

    def get_bool(self, key: str, default: bool = False) -> bool:
        """读取一个布尔设置（存储为 ``1`` / ``0``）。"""
        raw = self.get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def set_bool(self, key: str, value: bool) -> None:
        """写入一个布尔设置。"""
        self.set(key, "1" if value else "0")

    def get_rules(self) -> RuleSnapshot:
        """读取规则快照；无记录时返回全默认快照（D45 离线可用）。"""
        return RuleSnapshot.from_dict(self.get_json(SETTING_RULES_JSON, None))

    def save_rules(self, rules: RuleSnapshot) -> None:
        """持久化规则快照，并同步写 `rule_version`。"""
        self.set_json(SETTING_RULES_JSON, rules.to_dict())
        self.set(SETTING_RULE_VERSION, str(rules.version))

    def get_rule_version(self) -> int:
        """读取本地规则版本；无记录返回 0。"""
        raw = self.get(SETTING_RULE_VERSION, "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return 0


# ================================================================== 运行时状态
class RuntimeStateRepository:
    """`runtime_state` 单行仓储（``id`` 恒为 1）。"""

    _COLUMNS: Final[tuple[str, ...]] = (
        "last_effective_date",
        "last_state",
        "state_changed_at",
        "grace_until",
        "break_end_at",
        "continuous_use_seconds",
        "fired_reminders_json",
        "parent_fail_count",
        "parent_cooldown_until",
        "last_flush_at",
        "last_sync_at",
        "last_sync_ok",
        "clean_shutdown",
    )

    def __init__(self, db: LocalDatabase) -> None:
        """初始化。"""
        self._db = db

    def load(self) -> RuntimeState:
        """读取运行时状态。无行时返回默认值。"""
        row = self._db.query_one("SELECT * FROM runtime_state WHERE id = 1")
        if row is None:
            return RuntimeState()
        try:
            last_state = State(str(row["last_state"]))
        except ValueError:
            last_state = State.ACTIVE
        reminders = _loads(row["fired_reminders_json"], [])
        try:
            fired = frozenset(int(item) for item in reminders)
        except (TypeError, ValueError):
            fired = frozenset()
        return RuntimeState(
            last_effective_date=_parse_date(row["last_effective_date"]),
            last_state=last_state,
            state_changed_at=_parse_dt(row["state_changed_at"]),
            grace_until=_parse_dt(row["grace_until"]),
            break_end_at=_parse_dt(row["break_end_at"]),
            continuous_use_seconds=float(row["continuous_use_seconds"] or 0.0),
            fired_reminders=fired,
            parent_fail_count=int(row["parent_fail_count"] or 0),
            parent_cooldown_until=_parse_dt(row["parent_cooldown_until"]),
            last_flush_at=_parse_dt(row["last_flush_at"]),
            last_sync_at=_parse_dt(row["last_sync_at"]),
            last_sync_ok=bool(row["last_sync_ok"]),
            clean_shutdown=bool(row["clean_shutdown"]),
        )

    def save(self, st: RuntimeState) -> None:
        """整行覆盖写入。"""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE runtime_state SET "
                "last_effective_date = ?, last_state = ?, state_changed_at = ?, "
                "grace_until = ?, break_end_at = ?, continuous_use_seconds = ?, "
                "fired_reminders_json = ?, parent_fail_count = ?, "
                "parent_cooldown_until = ?, last_flush_at = ?, last_sync_at = ?, "
                "last_sync_ok = ?, clean_shutdown = ? WHERE id = 1",
                (
                    st.last_effective_date.isoformat() if st.last_effective_date else None,
                    st.last_state.value,
                    _iso(st.state_changed_at),
                    _iso(st.grace_until),
                    _iso(st.break_end_at),
                    float(st.continuous_use_seconds),
                    json.dumps(sorted(st.fired_reminders)),
                    int(st.parent_fail_count),
                    _iso(st.parent_cooldown_until),
                    _iso(st.last_flush_at),
                    _iso(st.last_sync_at),
                    1 if st.last_sync_ok else 0,
                    1 if st.clean_shutdown else 0,
                ),
            )

    def patch(self, **fields: Any) -> None:
        """局部更新若干列。

        Args:
            **fields: 列名到值的映射。支持 `RuntimeState` 的字段名
                （``fired_reminders`` 会自动映射到 ``fired_reminders_json``）。

        Raises:
            KeyError: 传入了未知列名。
        """
        if not fields:
            return
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            column = "fired_reminders_json" if key == "fired_reminders" else key
            if column not in self._COLUMNS:
                raise KeyError(f"runtime_state 不存在列 {column}")
            assignments.append(f"{column} = ?")
            params.append(self._encode(column, value))
        params.append(1)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE runtime_state SET {', '.join(assignments)} WHERE id = ?", params
            )

    @staticmethod
    def _encode(column: str, value: Any) -> Any:
        """把 Python 值编码成列存储格式。"""
        if value is None:
            return None
        if column == "last_effective_date":
            return value.isoformat() if isinstance(value, date) else str(value)
        if column == "last_state":
            return value.value if isinstance(value, State) else str(value)
        if column == "fired_reminders_json":
            if isinstance(value, (set, frozenset, list, tuple)):
                return json.dumps(sorted(int(item) for item in value))
            return str(value)
        if column in {"last_sync_ok", "clean_shutdown"}:
            return 1 if value else 0
        if column == "continuous_use_seconds":
            return float(value)
        if column == "parent_fail_count":
            return int(value)
        if isinstance(value, datetime):
            return _iso(value)
        return value

    def mark_started(self) -> bool:
        """启动标记：返回上次是否为异常退出，并把 ``clean_shutdown`` 置 0。

        Returns:
            ``True`` 表示上次是异常退出（`clean_shutdown == 0`）。
        """
        row = self._db.query_one("SELECT clean_shutdown FROM runtime_state WHERE id = 1")
        was_clean = bool(row["clean_shutdown"]) if row is not None else True
        with self._db.transaction() as conn:
            conn.execute("UPDATE runtime_state SET clean_shutdown = 0 WHERE id = 1")
        return not was_clean

    def mark_clean_exit(self) -> None:
        """正常退出标记。"""
        with self._db.transaction() as conn:
            conn.execute("UPDATE runtime_state SET clean_shutdown = 1 WHERE id = 1")


# ================================================================== 用量
class UsageRepository:
    """`daily_usage` 仓储。"""

    def __init__(self, db: LocalDatabase) -> None:
        """初始化。"""
        self._db = db

    @staticmethod
    def _to_record(row: sqlite3.Row) -> DailyUsageRecord:
        """行 → 记录。"""
        usage_date = _parse_date(row["usage_date"]) or date.min
        return DailyUsageRecord(
            usage_date=usage_date,
            used_seconds=float(row["used_seconds"] or 0.0),
            bonus_minutes=int(row["bonus_minutes"] or 0),
            parent_seconds=float(row["parent_seconds"] or 0.0),
            break_count=int(row["break_count"] or 0),
            base_quota_minutes=int(row["base_quota_minutes"] or 0),
        )

    def get(self, usage_date: date) -> DailyUsageRecord | None:
        """读取某日记录。"""
        row = self._db.query_one(
            "SELECT * FROM daily_usage WHERE usage_date = ?", (usage_date.isoformat(),)
        )
        return None if row is None else self._to_record(row)

    def upsert(self, rec: DailyUsageRecord) -> None:
        """写入/更新某日记录（``updated_at`` 自动刷新）。"""
        now = _iso(datetime.now(timezone.utc))
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO daily_usage (usage_date, used_seconds, bonus_minutes, "
                "parent_seconds, break_count, base_quota_minutes, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(usage_date) DO UPDATE SET "
                "used_seconds = excluded.used_seconds, "
                "bonus_minutes = excluded.bonus_minutes, "
                "parent_seconds = excluded.parent_seconds, "
                "break_count = excluded.break_count, "
                "base_quota_minutes = excluded.base_quota_minutes, "
                "updated_at = excluded.updated_at",
                (
                    rec.usage_date.isoformat(),
                    float(rec.used_seconds),
                    int(rec.bonus_minutes),
                    float(rec.parent_seconds),
                    int(rec.break_count),
                    int(rec.base_quota_minutes),
                    now,
                ),
            )

    def add_bonus_in_conn(
        self, conn: sqlite3.Connection, usage_date: date, minutes: int
    ) -> None:
        """在既有事务内累加额度（供 `CreditService` 调用）。

        Args:
            conn: 已处于事务中的连接。
            usage_date: 目标日期。
            minutes: 增量分钟。
        """
        conn.execute(
            "UPDATE daily_usage SET bonus_minutes = bonus_minutes + ?, updated_at = ? "
            "WHERE usage_date = ?",
            (int(minutes), _iso(datetime.now(timezone.utc)), usage_date.isoformat()),
        )

    def recent(self, days: int = 7) -> list[DailyUsageRecord]:
        """返回最近 N 天的记录（按日期降序）。"""
        rows = self._db.query_all(
            "SELECT * FROM daily_usage ORDER BY usage_date DESC LIMIT ?", (int(days),)
        )
        return [self._to_record(row) for row in rows]

    def pending_upload(self, limit: int = 7) -> list[DailyUsageRecord]:
        """返回需要上报的记录：``synced_at`` 为空或早于 ``updated_at``。

        Args:
            limit: 最多返回条数。

        Returns:
            按日期升序排列的记录列表。
        """
        rows = self._db.query_all(
            "SELECT * FROM daily_usage "
            "WHERE synced_at IS NULL OR synced_at < updated_at "
            "ORDER BY usage_date DESC LIMIT ?",
            (int(limit),),
        )
        records = [self._to_record(row) for row in rows]
        records.sort(key=lambda item: item.usage_date)
        return records

    def mark_synced(self, dates: Sequence[date], at: datetime) -> None:
        """标记若干日期已同步。"""
        if not dates:
            return
        stamp = _iso(at)
        with self._db.transaction() as conn:
            conn.executemany(
                "UPDATE daily_usage SET synced_at = ? WHERE usage_date = ?",
                [(stamp, item.isoformat()) for item in dates],
            )

    def reset(self, usage_date: date, *, include_bonus: bool = False) -> bool:
        """清零某日用量（RESET_USAGE 指令，方案 A）。

        Args:
            usage_date: 目标业务日期。
            include_bonus: 为 ``True`` 时连 ``bonus_minutes`` 一并清零；
                为 ``False`` 时保留已批准额度（默认行为）。

        Returns:
            该日记录是否存在并被更新（``True`` 表示确有可重置的数据）。
        """
        row = self._db.query_one(
            "SELECT 1 FROM daily_usage WHERE usage_date = ?", (usage_date.isoformat(),)
        )
        if row is None:
            return False
        fields = "used_seconds = 0, parent_seconds = 0, break_count = 0"
        if include_bonus:
            fields += ", bonus_minutes = 0"
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE daily_usage SET {fields}, updated_at = ? WHERE usage_date = ?",
                (_iso(datetime.now(timezone.utc)), usage_date.isoformat()),
            )
        return True


# ================================================================== 发件箱
class OutboxRepository:
    """`outbox` 仓储。"""

    def __init__(self, db: LocalDatabase) -> None:
        """初始化。"""
        self._db = db

    @staticmethod
    def _to_item(row: sqlite3.Row) -> OutboxItem:
        """行 → 条目。"""
        return OutboxItem(
            id=str(row["id"]),
            kind=str(row["kind"]),
            payload=_loads(row["payload_json"], {}),
            status=str(row["status"]),
            retry_count=int(row["retry_count"] or 0),
            next_retry_at=_parse_dt(row["next_retry_at"]),
            last_error=row["last_error"],
            created_at=_parse_dt(row["created_at"]),
            sent_at=_parse_dt(row["sent_at"]),
        )

    def enqueue(self, item_id: str, kind: str, payload: dict) -> None:
        """入队一条待发送记录（同 id 重复入队幂等）。

        Args:
            item_id: 条目 id（业务幂等键，如 request_id / command_id / event_id）。
            kind: `OutboxKind` 取值。
            payload: 负载字典。
        """
        now = _iso(datetime.now(timezone.utc))
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO outbox "
                "(id, kind, payload_json, status, retry_count, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (
                    item_id,
                    kind,
                    json.dumps(payload or {}, ensure_ascii=False),
                    OutboxStatus.PENDING.value,
                    now,
                ),
            )

    def enqueue_in_conn(
        self, conn: sqlite3.Connection, item_id: str, kind: str, payload: dict
    ) -> None:
        """在既有事务内入队（供 `EventRepository.append` 一次事务写两处）。"""
        conn.execute(
            "INSERT OR IGNORE INTO outbox "
            "(id, kind, payload_json, status, retry_count, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (
                item_id,
                kind,
                json.dumps(payload or {}, ensure_ascii=False),
                OutboxStatus.PENDING.value,
                _iso(datetime.now(timezone.utc)),
            ),
        )

    def due(self, now: datetime, limit: int = 100) -> list[OutboxItem]:
        """返回到期可发送的条目。

        Args:
            now: 当前时间。
            limit: 最多返回条数。

        Returns:
            按创建时间升序的条目列表。
        """
        rows = self._db.query_all(
            "SELECT * FROM outbox WHERE status != ? "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (OutboxStatus.SENT.value, _iso(now), int(limit)),
        )
        return [self._to_item(row) for row in rows]

    def due_by_kind(self, kind: str, now: datetime, limit: int = 100) -> list[OutboxItem]:
        """按 kind 过滤的到期条目。"""
        rows = self._db.query_all(
            "SELECT * FROM outbox WHERE kind = ? AND status != ? "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (kind, OutboxStatus.SENT.value, _iso(now), int(limit)),
        )
        return [self._to_item(row) for row in rows]

    def mark_sent(self, ids: Sequence[str], at: datetime) -> None:
        """标记若干条目已发送成功。"""
        if not ids:
            return
        stamp = _iso(at)
        with self._db.transaction() as conn:
            conn.executemany(
                "UPDATE outbox SET status = ?, sent_at = ?, last_error = NULL, "
                "next_retry_at = NULL WHERE id = ?",
                [(OutboxStatus.SENT.value, stamp, item) for item in ids],
            )

    def mark_failed(
        self, ids: Sequence[str], error: str, next_retry_at: datetime
    ) -> None:
        """标记失败并安排下次重试；事件类超过上限直接丢弃（§7.6）。"""
        if not ids:
            return
        stamp = _iso(next_retry_at)
        text = (error or "")[:500]
        with self._db.transaction() as conn:
            conn.executemany(
                "UPDATE outbox SET status = ?, retry_count = retry_count + 1, "
                "last_error = ?, next_retry_at = ? WHERE id = ?",
                [(OutboxStatus.FAILED.value, text, stamp, item) for item in ids],
            )
            conn.execute(
                "DELETE FROM outbox WHERE kind = ? AND retry_count > ?",
                (OutboxKind.EVENT.value, OUTBOX_MAX_RETRY),
            )

    def purge_sent(self, before: datetime) -> int:
        """清理已发送且早于指定时间的条目。

        Returns:
            实际删除行数。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM outbox WHERE status = ? AND sent_at IS NOT NULL AND sent_at < ?",
                (OutboxStatus.SENT.value, _iso(before)),
            )
            deleted = cursor.rowcount
            cursor.close()
        return max(0, int(deleted))

    @staticmethod
    def backoff_seconds(retry_count: int) -> int:
        """按重试次数返回退避秒数（D46：45 → 90 → 180 → 300）。"""
        index = min(max(retry_count, 0), len(OUTBOX_BACKOFF_SECONDS) - 1)
        return OUTBOX_BACKOFF_SECONDS[index]


# ================================================================== 入账台账
class LedgerRepository:
    """`credited_ledger` 仓储。"""

    def __init__(self, db: LocalDatabase) -> None:
        """初始化。"""
        self._db = db

    def insert_if_absent_in_conn(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        minutes: int,
        target_date: date,
        at: datetime,
    ) -> bool:
        """在既有事务内 ``INSERT OR IGNORE``。

        Args:
            conn: 已处于事务中的连接。
            request_id: 申请 id。
            minutes: 批准分钟数。
            target_date: 目标日期。
            at: 入账时刻。

        Returns:
            ``True`` 表示本次真实插入（可以加时）；``False`` 表示已存在。
        """
        cursor = conn.execute(
            "INSERT OR IGNORE INTO credited_ledger "
            "(request_id, minutes, target_date, credited_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (request_id, int(minutes), target_date.isoformat(), _iso(at)),
        )
        inserted = cursor.rowcount == 1
        cursor.close()
        return inserted

    def unconfirmed(self) -> list[str]:
        """返回尚未确认的 `request_id`（崩溃恢复只补发确认）。"""
        rows = self._db.query_all(
            "SELECT request_id FROM credited_ledger WHERE confirmed_at IS NULL "
            "ORDER BY credited_at ASC"
        )
        return [str(row["request_id"]) for row in rows]

    def mark_confirmed(self, request_ids: Sequence[str], at: datetime) -> None:
        """回写 ``confirmed_at``。"""
        if not request_ids:
            return
        stamp = _iso(at)
        with self._db.transaction() as conn:
            conn.executemany(
                "UPDATE credited_ledger SET confirmed_at = ? WHERE request_id = ?",
                [(stamp, item) for item in request_ids],
            )

    def exists(self, request_id: str) -> bool:
        """判断某 `request_id` 是否已入账。"""
        row = self._db.query_one(
            "SELECT 1 FROM credited_ledger WHERE request_id = ?", (request_id,)
        )
        return row is not None


# ================================================================== 事件
class EventRepository:
    """`event_logs` 仓储。写入时**同一事务**内同步入队到 `outbox`（§7.6）。"""

    def __init__(self, db: LocalDatabase) -> None:
        """初始化。"""
        self._db = db

    @staticmethod
    def _to_event(row: sqlite3.Row) -> LocalEvent:
        """行 → 事件。"""
        return LocalEvent(
            id=str(row["id"]),
            event_type=str(row["event_type"]),
            severity=str(row["severity"]),
            payload=_loads(row["payload_json"], {}),
            occurred_at=_parse_dt(row["occurred_at"]) or datetime.now(timezone.utc),
            uploaded_at=_parse_dt(row["uploaded_at"]),
        )

    def append(
        self, event_type: str, severity: str, payload: dict, occurred_at: datetime
    ) -> str:
        """追加一条事件，同时入队到 `outbox(kind='event')`。

        Args:
            event_type: `EventType` 取值。
            severity: `EventSeverity` 取值。
            payload: 事件负载。
            occurred_at: 发生时刻（UTC）。

        Returns:
            生成的 `client_event_id`（UUIDv4）。
        """
        event_id = str(uuid.uuid4())
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        stamp = _iso(occurred_at)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO event_logs "
                "(id, event_type, severity, payload_json, occurred_at, uploaded_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (event_id, event_type, severity, payload_json, stamp),
            )
            conn.execute(
                "INSERT OR IGNORE INTO outbox "
                "(id, kind, payload_json, status, retry_count, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (
                    event_id,
                    OutboxKind.EVENT.value,
                    payload_json,
                    OutboxStatus.PENDING.value,
                    stamp,
                ),
            )
        logger.debug("事件已记录：%s(%s) %s", event_type, severity, payload)
        return event_id

    def pending_upload(self, limit: int = 200) -> list[LocalEvent]:
        """返回尚未上传的事件（按发生时间升序）。"""
        rows = self._db.query_all(
            "SELECT * FROM event_logs WHERE uploaded_at IS NULL "
            "ORDER BY occurred_at ASC LIMIT ?",
            (int(limit),),
        )
        return [self._to_event(row) for row in rows]

    def mark_uploaded(self, ids: Sequence[str], at: datetime) -> None:
        """标记若干事件已上传，同时把对应 outbox 条目置为已发送。"""
        if not ids:
            return
        stamp = _iso(at)
        with self._db.transaction() as conn:
            conn.executemany(
                "UPDATE event_logs SET uploaded_at = ? WHERE id = ?",
                [(stamp, item) for item in ids],
            )
            conn.executemany(
                "UPDATE outbox SET status = ?, sent_at = ?, next_retry_at = NULL, "
                "last_error = NULL WHERE id = ?",
                [(OutboxStatus.SENT.value, stamp, item) for item in ids],
            )

    def recent(self, limit: int = 100) -> list[LocalEvent]:
        """返回最近的事件（家长面板用，按发生时间降序）。"""
        rows = self._db.query_all(
            "SELECT * FROM event_logs ORDER BY occurred_at DESC LIMIT ?", (int(limit),)
        )
        return [self._to_event(row) for row in rows]

    def purge_uploaded(self, before: datetime) -> int:
        """清理早于指定时间且已上传的事件（保留 7 天）。

        Returns:
            实际删除行数。
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM event_logs WHERE uploaded_at IS NOT NULL AND occurred_at < ?",
                (_iso(before),),
            )
            deleted = cursor.rowcount
            cursor.close()
        return max(0, int(deleted))


# ================================================================== 聚合
@dataclass(frozen=True)
class Repositories:
    """一次性持有全部仓储，方便注入 `RuntimeEngine`。"""

    settings: SettingsRepository
    runtime: RuntimeStateRepository
    usage: UsageRepository
    outbox: OutboxRepository
    ledger: LedgerRepository
    events: EventRepository
    #: 底层数据库句柄。所有仓储共享同一实例；写事务必须发生在 GUI 主线程
    #: （Single-Writer 约束，见模块 docstring）。
    db: LocalDatabase = field(compare=False, repr=False)

    @classmethod
    def create(cls, db: LocalDatabase) -> "Repositories":
        """按同一个数据库句柄构造全部仓储。"""
        return cls(
            settings=SettingsRepository(db),
            runtime=RuntimeStateRepository(db),
            usage=UsageRepository(db),
            outbox=OutboxRepository(db),
            ledger=LedgerRepository(db),
            events=EventRepository(db),
            db=db,
        )

    def seal_old_device_data(self, at: datetime) -> dict[str, int]:
        """🔴 Q-A7（§C.4.3）：把旧设备的待上传数据一次性推进终态。

        重配对换服务器前调用。旧 ``device_id`` 下堆积的待上传数据，换到新
        凭据后必然被新服务器 4xx 拒绝；留着只会无限重试、污染日志、拖慢同步。
        但**不能 DELETE**：家长面板的本地历史（``events.recent()`` /
        ``usage.recent()``）依赖这些行，``credited_ledger`` 未确认行更是
        「加时幂等」的唯一依据。

        因此这里做的是「封存」：四个来源、**单事务**，全部复用既有的状态列
        （``uploaded_at`` / ``synced_at`` / ``status`` / ``confirmed_at``），
        零 DDL、零 DELETE。SQLite 保证要么全应用、要么全不应用。

        Args:
            at: 封存时刻（UTC，tz-aware）。

        Returns:
            ``{"events": n1, "usage": n2, "outbox": n3, "credits": n4}``，
            供分界事件 payload 事后审计。
        """
        stamp = _iso(at)
        with self.db.transaction() as conn:
            # 来源 1+2：event_logs（含其影子 outbox 行）——同一事务双写。
            event_ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM event_logs WHERE uploaded_at IS NULL"
                )
            ]
            conn.executemany(
                "UPDATE event_logs SET uploaded_at = ? WHERE id = ?",
                [(stamp, item) for item in event_ids],
            )
            conn.executemany(
                "UPDATE outbox SET status = ?, sent_at = ?, next_retry_at = NULL, "
                "last_error = NULL WHERE id = ?",
                [(OutboxStatus.SENT.value, stamp, item) for item in event_ids],
            )

            # 来源 3：daily_usage —— 未同步（synced_at 为空或早于 updated_at）。
            usage_dates = [
                str(row["usage_date"])
                for row in conn.execute(
                    "SELECT usage_date FROM daily_usage "
                    "WHERE synced_at IS NULL OR synced_at < updated_at"
                )
            ]
            conn.executemany(
                "UPDATE daily_usage SET synced_at = ? WHERE usage_date = ?",
                [(stamp, item) for item in usage_dates],
            )

            # 来源 4：outbox 的 extension_request / command_ack / credit_confirm。
            outbox_rows = conn.execute(
                "SELECT id, kind FROM outbox WHERE status != ? "
                "AND kind IN (?, ?, ?)",
                (
                    OutboxStatus.SENT.value,
                    OutboxKind.EXTENSION_REQUEST.value,
                    OutboxKind.COMMAND_ACK.value,
                    OutboxKind.CREDIT_CONFIRM.value,
                ),
            ).fetchall()
            outbox_ids = [str(row["id"]) for row in outbox_rows]
            conn.executemany(
                "UPDATE outbox SET status = ?, sent_at = ?, next_retry_at = NULL, "
                "last_error = NULL WHERE id = ?",
                [(OutboxStatus.SENT.value, stamp, item) for item in outbox_ids],
            )

            # 来源 5：credited_ledger —— 未确认行置 confirmed_at。
            credit_ids = [
                str(row["request_id"])
                for row in conn.execute(
                    "SELECT request_id FROM credited_ledger "
                    "WHERE confirmed_at IS NULL"
                )
            ]
            conn.executemany(
                "UPDATE credited_ledger SET confirmed_at = ? WHERE request_id = ?",
                [(stamp, item) for item in credit_ids],
            )

        counts = {
            "events": len(event_ids),
            "usage": len(usage_dates),
            "outbox": len(outbox_ids),
            "credits": len(credit_ids),
        }
        logger.info("封存旧设备待上传数据：%s", counts)
        return counts
