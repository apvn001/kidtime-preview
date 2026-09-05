"""延时申请服务：提交（3 条 pending 上限）、审批、拒绝、过期扫描、入账确认。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.core.constants import (
    MAX_PENDING_EXTENSIONS_PER_DAY,
    EventSeverity,
    EventType,
    ExtensionStatus,
)
from app.core.errors import ConflictError, ErrorCode, NotFoundError
from app.core.utils import parse_csv_filter
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.device import Device
from app.models.extension import ExtensionRequest
from app.models.user import User
from app.schemas.extension import ExtensionRequestOut
from app.services import credit_service, event_service, usage_service

REJECT_REASON_PENDING_LIMIT = "PENDING_LIMIT"
REJECT_REASON_PARENT = "PARENT_REJECTED"


def get_or_404(db: Session, request_id: str) -> ExtensionRequest:
    """按 id 查询申请。

    Raises:
        NotFoundError: 申请不存在。
    """
    row = db.get(ExtensionRequest, request_id)
    if row is None:
        raise NotFoundError("延时申请不存在", code=ErrorCode.EXTENSION_NOT_FOUND)
    return row


def count_pending(db: Session, device_id: str, target_date: date) -> int:
    """统计某设备某目标日期的 pending 申请数（D30）。"""
    stmt = (
        select(func.count())
        .select_from(ExtensionRequest)
        .where(
            ExtensionRequest.device_id == device_id,
            ExtensionRequest.target_date == target_date,
            ExtensionRequest.status == ExtensionStatus.PENDING.value,
        )
    )
    return int(db.execute(stmt).scalar_one() or 0)


def expire_stale(
    db: Session, device_id: str | None = None, today: date | None = None,
    *, now: datetime | None = None,
) -> int:
    """把 `target_date` 已过的 pending 申请置为 `expired`（D27）。

    Args:
        db: 数据库会话。
        device_id: 限定设备；``None`` 时扫描全部 active 设备。
        today: 设备本地今日；``None`` 时按每台设备的时区各自计算。
        now: 注入的当前时间（G8）。

    Returns:
        被置为 expired 的条数。
    """
    moment = now or utcnow()
    total = 0
    if device_id is not None:
        device = db.get(Device, device_id)
        if device is None:
            return 0
        target_today = today or usage_service.device_today(device, moment)
        stmt = (
            update(ExtensionRequest)
            .where(
                ExtensionRequest.device_id == device_id,
                ExtensionRequest.status == ExtensionStatus.PENDING.value,
                ExtensionRequest.target_date < target_today,
            )
            .values(status=ExtensionStatus.EXPIRED.value)
        )
        return int(db.execute(stmt).rowcount or 0)

    for device in db.execute(select(Device)).scalars().all():
        device_today = today or usage_service.device_today(device, moment)
        stmt = (
            update(ExtensionRequest)
            .where(
                ExtensionRequest.device_id == device.id,
                ExtensionRequest.status == ExtensionStatus.PENDING.value,
                ExtensionRequest.target_date < device_today,
            )
            .values(status=ExtensionStatus.EXPIRED.value)
        )
        total += int(db.execute(stmt).rowcount or 0)
    return total


def submit(
    db: Session,
    device: Device,
    request_id: str,
    target_date: date,
    requested_minutes: int,
    reason: str,
    client_created_at: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[ExtensionRequest | None, bool, str | None]:
    """提交（或补传）一条延时申请。

    Returns:
        ``(申请对象, 是否新增, 软拒绝原因)``。
        已存在时返回 ``(已有对象, False, None)``；
        超过 3 条 pending 上限时返回 ``(None, False, "PENDING_LIMIT")``（D30）。
    """
    moment = now or utcnow()
    existing = db.get(ExtensionRequest, request_id)
    if existing is not None:
        return existing, False, None

    if count_pending(db, device.id, target_date) >= MAX_PENDING_EXTENSIONS_PER_DAY:
        return None, False, REJECT_REASON_PENDING_LIMIT

    row = ExtensionRequest(
        id=request_id,
        device_id=device.id,
        target_date=target_date,
        requested_minutes=requested_minutes,
        reason=reason or "",
        status=ExtensionStatus.PENDING.value,
        approved_minutes=None,
        created_at=moment,
        client_created_at=client_created_at,
    )
    db.add(row)
    db.flush()
    event_service.record(
        db,
        EventType.EXTENSION_SUBMITTED,
        device_id=device.id,
        payload={
            "request_id": request_id,
            "target_date": target_date.isoformat(),
            "requested_minutes": requested_minutes,
        },
        occurred_at=client_created_at or moment,
        now=moment,
    )
    return row, True, None


@retry_on_busy
def approve(
    db: Session,
    request_id: str,
    approved_minutes: int,
    user: User,
    *,
    now: datetime | None = None,
) -> ExtensionRequest:
    """批准申请（D29：`approved_minutes` 独立于 `requested_minutes`）。

    Raises:
        ConflictError: 当前状态非 pending，或 `target_date` 已过。
    """
    row = get_or_404(db, request_id)
    moment = now or utcnow()
    if row.status != ExtensionStatus.PENDING.value:
        raise ConflictError("该申请已被处理", code=ErrorCode.EXTENSION_NOT_PENDING)

    device = db.get(Device, row.device_id)
    today = usage_service.device_today(device, moment) if device else moment.date()
    if row.target_date < today:
        row.status = ExtensionStatus.EXPIRED.value
        db.flush()
        raise ConflictError(
            "申请目标日期已过，无法审批", code=ErrorCode.EXTENSION_DATE_PASSED
        )

    row.status = ExtensionStatus.APPROVED.value
    row.approved_minutes = approved_minutes
    row.decided_by = user.id
    row.decided_at = moment
    db.flush()
    return row


@retry_on_busy
def reject(
    db: Session,
    request_id: str,
    reason: str | None,
    user: User,
    *,
    now: datetime | None = None,
) -> ExtensionRequest:
    """拒绝申请（D29：`approved_minutes = 0`）。

    Raises:
        ConflictError: 当前状态非 pending。
    """
    row = get_or_404(db, request_id)
    if row.status != ExtensionStatus.PENDING.value:
        raise ConflictError("该申请已被处理", code=ErrorCode.EXTENSION_NOT_PENDING)
    moment = now or utcnow()
    row.status = ExtensionStatus.REJECTED.value
    row.approved_minutes = 0
    row.decided_by = user.id
    row.decided_at = moment
    db.flush()
    if reason:
        event_service.record(
            db,
            EventType.EXTENSION_SUBMITTED,
            severity=EventSeverity.INFO,
            device_id=row.device_id,
            user_id=user.id,
            payload={"request_id": row.id, "action": "rejected", "reason": reason},
            now=moment,
        )
    return row


def confirm_credit(
    db: Session,
    device_id: str,
    request_ids: Sequence[str],
    *,
    now: datetime | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """确认额度已在客户端入账（D31 / A11，重复确认幂等成功）。

    V7 起经 ``credit_service.confirm`` 路由到 ``ExtensionRequest`` 与 ``TimeGrant``
    双表（复用既有 ``/client/confirm-credit`` 路径，客户端零改动）。

    Returns:
        ``(confirmed, already_credited, not_found)``。
    """
    return credit_service.confirm(db, device_id, request_ids, now=now)


def list_deliverable_credits(
    db: Session, device_id: str, today: date
) -> list[ExtensionRequest]:
    """同步第 11 步：查询待下发的已批准额度。

    条件：``status='approved' AND approved_minutes>0 AND target_date >= today-1``。
    已 `credited` 的天然被状态过滤排除。
    """
    from datetime import timedelta

    stmt = (
        select(ExtensionRequest)
        .where(
            ExtensionRequest.device_id == device_id,
            ExtensionRequest.status == ExtensionStatus.APPROVED.value,
            ExtensionRequest.approved_minutes.is_not(None),
            ExtensionRequest.approved_minutes > 0,
            ExtensionRequest.target_date >= today - timedelta(days=1),
        )
        .order_by(ExtensionRequest.decided_at.asc())
    )
    return list(db.execute(stmt).scalars().all())


def list_recent_for_device(
    db: Session, device_id: str, today: date, limit: int = 50
) -> list[ExtensionRequest]:
    """查询该设备近期（target_date >= today-1）的申请，用于 `extension_results`。"""
    from datetime import timedelta

    stmt = (
        select(ExtensionRequest)
        .where(
            ExtensionRequest.device_id == device_id,
            ExtensionRequest.target_date >= today - timedelta(days=1),
        )
        .order_by(ExtensionRequest.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def list_requests(
    db: Session,
    *,
    device_id: str | None = None,
    status_filter: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[ExtensionRequestOut], int]:
    """分页查询申请列表，`pending` 置顶、其次按 `created_at DESC`。"""
    stmt = select(ExtensionRequest)
    if device_id:
        stmt = stmt.where(ExtensionRequest.device_id == device_id)
    if status_filter:
        statuses = parse_csv_filter(status_filter)
        if statuses:
            stmt = stmt.where(ExtensionRequest.status.in_(statuses))
    if date_from is not None:
        stmt = stmt.where(ExtensionRequest.target_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(ExtensionRequest.target_date <= date_to)

    total = int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one() or 0
    )
    # `func.case(..., else_=1)` 在 SQLAlchemy 2.0 非法：`func.*` 是通用 SQL 函数
    # 代理，不接受 `else_` 关键字；必须用独立的 `case()` 构造（G14）。
    pending_first = case(
        (ExtensionRequest.status == ExtensionStatus.PENDING.value, 0), else_=1
    )
    rows = (
        db.execute(
            stmt.order_by(pending_first.asc(), ExtensionRequest.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return [to_out(db, row) for row in rows], total


def to_out(db: Session, row: ExtensionRequest) -> ExtensionRequestOut:
    """把 ORM 申请转成出参（补齐 device_name / decided_by_username / 快照名）。"""
    device = db.get(Device, row.device_id)
    decided_by_username: str | None = None
    if row.decided_by:
        user = db.get(User, row.decided_by)
        decided_by_username = user.username if user is not None else None
    return ExtensionRequestOut(
        id=row.id,
        device_id=row.device_id,
        device_name=device.name if device is not None else "",
        target_date=row.target_date,
        requested_minutes=row.requested_minutes,
        reason=row.reason,
        status=row.status,  # type: ignore[arg-type]
        approved_minutes=row.approved_minutes,
        decided_by=row.decided_by,
        decided_by_username=decided_by_username,
        decided_by_name=row.decided_by_name,
        decided_at=row.decided_at,
        credited_at=row.credited_at,
        created_at=row.created_at,
        client_created_at=row.client_created_at,
    )


def count_pending_all(db: Session) -> int:
    """统计全部 pending 申请数（仪表盘 KPI）。"""
    stmt = (
        select(func.count())
        .select_from(ExtensionRequest)
        .where(ExtensionRequest.status == ExtensionStatus.PENDING.value)
    )
    return int(db.execute(stmt).scalar_one() or 0)
