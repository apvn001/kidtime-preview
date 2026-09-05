"""事件服务：写入（含 `client_event_id` 去重）、筛选分页查询。"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_TIMEZONE, EventSeverity, EventType
from app.core.errors import ErrorCode, NotFoundError
from app.core.utils import parse_csv_filter
from app.db.base import utcnow
from app.models.device import Device
from app.models.event import EventLog
from app.models.user import User
from app.schemas.event import EventOut, EventUploadItem


def resolve_timezone(name: str | None) -> ZoneInfo:
    """把 IANA 时区名解析成 `ZoneInfo`，非法值回落到 `Asia/Shanghai`（D09）。"""
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def record(
    db: Session,
    event_type: EventType | str,
    *,
    severity: EventSeverity | str = EventSeverity.INFO,
    device_id: str | None = None,
    user_id: int | None = None,
    actor_name: str | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    client_event_id: str | None = None,
    now: datetime | None = None,
) -> EventLog:
    """写入一条服务端事件。

    Args:
        db: 数据库会话。
        event_type: 事件类型（27 种之一）。
        severity: 事件级别。
        device_id: 关联设备。⚠️ `DEVICE_DELETED` 必须传 ``None``（共享知识 2）。
        user_id: 关联用户（`event_logs.user_id` 为 ON DELETE SET NULL）。
        actor_name: 操作人姓名快照（V5）。账号被删后 `user_id` 会被置空，
            该列保证历史仍可溯源到「已删除账号（原 X）」。
        payload: 事件负载。
        occurred_at: 发生时刻，缺省取 `now`。
        client_event_id: 客户端事件 id，服务端产生的事件为 ``None``。
        now: 注入的当前时间（G8）。

    Returns:
        已加入 Session 的事件对象。
    """
    moment = now or utcnow()
    event = EventLog(
        client_event_id=client_event_id,
        device_id=device_id,
        user_id=user_id,
        actor_name=actor_name,
        event_type=str(getattr(event_type, "value", event_type)),
        severity=str(getattr(severity, "value", severity)),
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        occurred_at=occurred_at or moment,
        created_at=moment,
    )
    db.add(event)
    db.flush()
    return event


def bulk_insert(
    db: Session,
    device_id: str,
    items: Sequence[EventUploadItem],
    now: datetime | None = None,
) -> int:
    """批量写入客户端上传的事件，按 `client_event_id` 去重（S10 / A05）。

    Args:
        db: 数据库会话。
        device_id: 归属设备。
        items: 上传的事件列表。
        now: 注入的当前时间。

    Returns:
        实际新增条数（已存在的不计）。
    """
    if not items:
        return 0
    moment = now or utcnow()
    inserted = 0
    for item in items:
        stmt = (
            sqlite_insert(EventLog)
            .values(
                client_event_id=item.client_event_id,
                device_id=device_id,
                user_id=None,
                event_type=item.event_type,
                severity=item.severity,
                payload_json=json.dumps(item.payload or {}, ensure_ascii=False),
                occurred_at=item.occurred_at,
                created_at=moment,
            )
            .on_conflict_do_nothing(index_elements=["client_event_id"])
        )
        result = db.execute(stmt)
        inserted += int(result.rowcount or 0)
    return inserted


def _day_bounds(day: date, tz: ZoneInfo, end: bool = False) -> datetime:
    """把设备本地自然日边界转换成 UTC 时刻。"""
    if end:
        local = datetime.combine(day, time.max.replace(microsecond=999999), tzinfo=tz)
    else:
        local = datetime.combine(day, time.min, tzinfo=tz)
    return local.astimezone(timezone.utc)


def _build_query(
    device_id: str | None,
    types: Iterable[str],
    severities: Iterable[str],
    date_from: date | None,
    date_to: date | None,
    keyword: str | None,
    tz: ZoneInfo,
) -> Select[Any]:
    """构造事件筛选条件（不含排序与分页）。"""
    stmt = select(EventLog)
    if device_id:
        stmt = stmt.where(EventLog.device_id == device_id)
    type_list = list(types)
    if type_list:
        stmt = stmt.where(EventLog.event_type.in_(type_list))
    severity_list = list(severities)
    if severity_list:
        stmt = stmt.where(EventLog.severity.in_(severity_list))
    if date_from is not None:
        stmt = stmt.where(EventLog.occurred_at >= _day_bounds(date_from, tz))
    if date_to is not None:
        stmt = stmt.where(EventLog.occurred_at <= _day_bounds(date_to, tz, end=True))
    if keyword:
        stmt = stmt.where(EventLog.payload_json.like(f"%{keyword}%"))
    return stmt


def list_events(
    db: Session,
    *,
    device_id: str | None = None,
    type_filter: str | None = None,
    severity_filter: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    keyword: str | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[EventOut], int]:
    """分页查询事件。

    `date_from` / `date_to` 按设备本地自然日过滤；未指定 `device_id` 时，
    使用默认时区（`Asia/Shanghai`）换算边界。

    Returns:
        ``(当前页数据, 总条数)``。
    """
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    if device_id:
        device = db.get(Device, device_id)
        if device is not None:
            tz = resolve_timezone(device.timezone)

    types = parse_csv_filter(type_filter)
    severities = parse_csv_filter(severity_filter)
    base = _build_query(device_id, types, severities, date_from, date_to, keyword, tz)

    total = int(
        db.execute(select(func.count()).select_from(base.subquery())).scalar_one() or 0
    )
    rows = (
        db.execute(
            base.order_by(EventLog.occurred_at.desc(), EventLog.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return [to_out(db, row) for row in rows], total


def get_event(db: Session, event_id: int) -> EventOut:
    """按 id 查询单条事件。

    Raises:
        NotFoundError: 事件不存在。
    """
    row = db.get(EventLog, event_id)
    if row is None:
        raise NotFoundError("事件不存在", code=ErrorCode.EVENT_NOT_FOUND)
    return to_out(db, row)


def to_out(db: Session, row: EventLog) -> EventOut:
    """把 ORM 事件转成出参（补齐 device_name / username / actor_name）。

    `actor_name` 是 V5 新增的操作人姓名快照：账号被删除后 `user_id` 被置空，
    前端 `formatOperator(user_id, username ?? actor_name)` 据此渲染
    「已删除账号（原 X）」。
    """
    device_name: str | None = None
    if row.device_id:
        device = db.get(Device, row.device_id)
        device_name = device.name if device is not None else None
    username: str | None = None
    if row.user_id:
        user = db.get(User, row.user_id)
        username = user.username if user is not None else None
    return EventOut(
        id=row.id,
        client_event_id=row.client_event_id,
        device_id=row.device_id,
        device_name=device_name,
        user_id=row.user_id,
        username=username,
        actor_name=row.actor_name,
        event_type=row.event_type,
        severity=row.severity,  # type: ignore[arg-type]
        payload=row.payload,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


def count_abnormal_since(db: Session, since: datetime) -> int:
    """统计指定时刻之后的 warning/error 事件数（仪表盘 KPI）。"""
    stmt = select(func.count()).select_from(EventLog).where(
        EventLog.occurred_at >= since,
        EventLog.severity.in_([EventSeverity.WARNING.value, EventSeverity.ERROR.value]),
    )
    return int(db.execute(stmt).scalar_one() or 0)


def hours_ago(hours: int, now: datetime | None = None) -> datetime:
    """返回 `now - hours` 的 UTC 时刻。"""
    return (now or utcnow()) - timedelta(hours=hours)
