"""下发时长服务：创建 / 列表 / 撤回 / 惰性过期（§3.3 / §5 R4）。

``time_grants`` 表记录管理员向某设备下发的「当日额外配额」。每条 grant 由服务端
生成 UUID，经 sync 第 11 步并入既有 credits 下行数组推送给客户端，客户端凭既有
``credited_ledger`` 精确一次去重入账；``/client/confirm-credit`` 路由到本表。

V7 修正：创建 grant 的同时**立即在服务端 daily_usage.bonus_minutes 上累加分钟数**，
使网页后台立即看到「剩余时长」增加；客户端随后同步时再次应用同一额度（幂等，
 credited_ledger 去重），并回执确认把 grant 状态推进到 ``credited``。

状态机：``granted`` → ``credited``（客户端确认入账）/ ``revoked``（管理员撤回）/
``expired``（目标日已过期，惰性置位，不再下发）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.constants import (
    GRANT_MINUTES_MAX,
    GRANT_MINUTES_MIN,
    MAX_GRANTS_PER_DEVICE_PER_DAY,
    MAX_GRANT_TOTAL_MINUTES_PER_DAY,
    EventType,
    GrantStatus,
)
from app.core.errors import (
    ConflictError,
    ErrorCode,
    NotFoundError,
    UnprocessableEntityError,
)
from app.core.security import new_uuid
from app.core.utils import parse_csv_filter
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.device import Device
from app.models.grant import TimeGrant
from app.models.user import User
from app.schemas.grant import TimeGrantOut
from app.services import event_service, rule_service, usage_service


@retry_on_busy
def create(
    db: Session,
    device_id: str,
    minutes: int,
    reason: str,
    user: User,
    *,
    target_date: date | None = None,
    now: datetime | None = None,
) -> TimeGrant:
    """创建一条 time_grant（状态=granted），并写 TIME_GRANTED 审计事件。

    Raises:
        NotFoundError: 设备不存在。
        ConflictError: 设备已停用 / 仅 granted 可撤回之外（此处为状态校验占位）。
        UnprocessableEntityError: minutes 越界。
        ConflictError(GRANT_DAILY_LIMIT_EXCEEDED): 超过单设备单日下发上限。
    """
    moment = now or utcnow()
    device = db.get(Device, device_id)
    if device is None:
        raise NotFoundError("设备不存在", code=ErrorCode.DEVICE_NOT_FOUND)
    if device.status != "active":
        raise ConflictError("设备已停用，无法下发时长", code=ErrorCode.DEVICE_RETIRED)
    if not (GRANT_MINUTES_MIN <= int(minutes) <= GRANT_MINUTES_MAX):
        raise UnprocessableEntityError(
            f"时长需在 {GRANT_MINUTES_MIN}-{GRANT_MINUTES_MAX} 分钟之间",
            code=ErrorCode.VALIDATION_ERROR,
        )

    target = target_date or usage_service.device_today(device, moment)
    issued = count_granted(db, device_id, target, now=moment)
    if issued >= MAX_GRANTS_PER_DEVICE_PER_DAY:
        raise ConflictError(
            f"该设备当日（{target.isoformat()}）下发次数已达上限 "
            f"{MAX_GRANTS_PER_DEVICE_PER_DAY}",
            code=ErrorCode.GRANT_DAILY_LIMIT_EXCEEDED,
        )
    # R4：单设备单日下发总额上限（防「5 次 × 240」无限堆额度）。
    granted_total = count_granted_minutes(db, device_id, target, now=moment)
    if granted_total + int(minutes) > MAX_GRANT_TOTAL_MINUTES_PER_DAY:
        raise ConflictError(
            f"该设备当日（{target.isoformat()}）下发总额将达 "
            f"{granted_total + int(minutes)} 分钟，超过上限 "
            f"{MAX_GRANT_TOTAL_MINUTES_PER_DAY}",
            code=ErrorCode.GRANT_DAILY_LIMIT_EXCEEDED,
        )

    grant = TimeGrant(
        id=new_uuid(),
        device_id=device.id,
        target_date=target,
        minutes=int(minutes),
        reason=reason or "",
        status=GrantStatus.GRANTED.value,
        granted_by=user.id,
        granted_by_name=user.username,
        granted_at=moment,
    )
    db.add(grant)
    db.flush()

    # 🔴 服务端立即生效：把 grant 分钟数累加到 daily_usage.bonus_minutes，
    # 网页后台立刻看到「今日剩余」增加；客户端后续同步会再次幂等应用。
    usage_row = usage_service.ensure_daily_usage(db, device, target, now=moment)
    usage_row.bonus_minutes += int(minutes)
    usage_row.updated_at = moment
    db.flush()

    event_service.record(
        db,
        EventType.TIME_GRANTED,
        device_id=device.id,
        user_id=user.id,
        actor_name=user.username,
        payload={
            "grant_id": grant.id,
            "minutes": grant.minutes,
            "target_date": grant.target_date.isoformat(),
            "reason": reason or "",
            "action": GrantStatus.GRANTED.value,
        },
        now=moment,
    )
    return grant


def count_granted(
    db: Session, device_id: str, target_date: date, *, now: datetime | None = None
) -> int:
    """统计某设备某目标日期 granted 状态的条数（R4 频控）。"""
    _moment = now or utcnow()
    stmt = (
        select(func.count())
        .select_from(TimeGrant)
        .where(
            TimeGrant.device_id == device_id,
            TimeGrant.target_date == target_date,
            TimeGrant.status == GrantStatus.GRANTED.value,
        )
    )
    return int(db.execute(stmt).scalar_one() or 0)


def count_granted_minutes(
    db: Session, device_id: str, target_date: date, *, now: datetime | None = None
) -> int:
    """统计某设备某目标日期 granted 状态的总分钟数（R4 总额上限）。"""
    _moment = now or utcnow()
    stmt = (
        select(func.coalesce(func.sum(TimeGrant.minutes), 0))
        .select_from(TimeGrant)
        .where(
            TimeGrant.device_id == device_id,
            TimeGrant.target_date == target_date,
            TimeGrant.status == GrantStatus.GRANTED.value,
        )
    )
    return int(db.execute(stmt).scalar_one() or 0)


def list_grants(
    db: Session,
    *,
    device_id: str | None = None,
    status: str | None = None,
    target_date: date | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[TimeGrant], int]:
    """分页查询 time_grants，按 granted_at DESC。"""
    stmt = select(TimeGrant)
    if device_id:
        stmt = stmt.where(TimeGrant.device_id == device_id)
    if status:
        statuses = parse_csv_filter(status)
        if statuses:
            stmt = stmt.where(TimeGrant.status.in_(statuses))
    if target_date is not None:
        stmt = stmt.where(TimeGrant.target_date == target_date)

    total = int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one() or 0
    )
    rows = (
        db.execute(
            stmt.order_by(TimeGrant.granted_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return list(rows), total


@retry_on_busy
def revoke(db: Session, grant_id: str, user: User, *, now: datetime | None = None) -> TimeGrant:
    """撤回一条 granted 的 grant（R6 幂等：非 granted 状态直接返回，等同 200 成功）。

    已 credited / expired / revoked 的 grant 不可再撤，但按 R6 应视为幂等成功
    （重复撤回不产生副作用，也不报错），仅 granted 状态会真正改写并写审计。
    """
    moment = now or utcnow()
    grant = db.get(TimeGrant, grant_id)
    if grant is None:
        raise NotFoundError("下发记录不存在", code=ErrorCode.NOT_FOUND)
    if grant.status != GrantStatus.GRANTED.value:
        # R6 幂等：重复撤回 / 撤回已生效记录 → 视为成功，原样返回（不写审计）。
        return grant

    # 🔴 服务端立即回滚：撤回 granted 状态的 grant 时，把之前累加的 bonus_minutes
    # 扣回（不低于 0），保证网页后台的「今日剩余」立即反映撤回。
    usage_row = usage_service.get_daily(db, grant.device_id, grant.target_date)
    if usage_row is not None:
        usage_row.bonus_minutes = max(0, usage_row.bonus_minutes - grant.minutes)
        usage_row.updated_at = moment

    grant.status = GrantStatus.REVOKED.value
    db.flush()
    event_service.record(
        db,
        EventType.TIME_GRANTED,
        device_id=grant.device_id,
        user_id=user.id,
        actor_name=user.username,
        payload={
            "grant_id": grant.id,
            "minutes": grant.minutes,
            "target_date": grant.target_date.isoformat(),
            "action": GrantStatus.REVOKED.value,
        },
        now=moment,
    )
    return grant


def expire_stale(
    db: Session, today: date | None = None, *, now: datetime | None = None
) -> int:
    """把 ``target_date < today-1`` 的 granted 置为 expired（不再下行）。"""
    moment = now or utcnow()
    today = today or moment.date()
    cutoff = today - timedelta(days=1)
    stmt = (
        update(TimeGrant)
        .where(TimeGrant.status == GrantStatus.GRANTED.value, TimeGrant.target_date < cutoff)
        .values(status=GrantStatus.EXPIRED.value)
    )
    return int(db.execute(stmt).rowcount or 0)


def list_deliverable(db: Session, device_id: str, today: date) -> list[TimeGrant]:
    """同步第 11 步：待下发的已批准时长（status='granted' AND target_date >= today-1）。"""
    cutoff = today - timedelta(days=1)
    stmt = (
        select(TimeGrant)
        .where(
            TimeGrant.device_id == device_id,
            TimeGrant.status == GrantStatus.GRANTED.value,
            TimeGrant.target_date >= cutoff,
        )
        .order_by(TimeGrant.granted_at.asc())
    )
    return list(db.execute(stmt).scalars().all())


def to_out(db: Session, row: TimeGrant) -> TimeGrantOut:
    """把 ORM 下发记录转成出参（补齐 device_name / granted_by_username / 快照名）。"""
    device = db.get(Device, row.device_id)
    granted_by_username: str | None = None
    if row.granted_by:
        user = db.get(User, row.granted_by)
        granted_by_username = user.username if user is not None else None
    return TimeGrantOut(
        id=row.id,
        device_id=row.device_id,
        device_name=device.name if device is not None else "",
        target_date=row.target_date,
        minutes=row.minutes,
        reason=row.reason,
        status=row.status,  # type: ignore[arg-type]
        granted_by=row.granted_by,
        granted_by_name=row.granted_by_name or granted_by_username,
        granted_at=row.granted_at,
        credited_at=row.credited_at,
    )
