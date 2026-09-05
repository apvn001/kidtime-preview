"""用量服务：🔴 MAX 防回退 Upsert、区间查询、跨设备汇总（D32/D33）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.constants import MAX_USAGE_RANGE_DAYS
from app.core.errors import ErrorCode, UnprocessableEntityError
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.device import Device
from app.models.usage import DailyUsage
from app.schemas.client_sync import UsageUploadItem
from app.schemas.usage import DailyUsageOut, DeviceUsageSummary, UsageSummaryOut
from app.services import rule_service
from app.services.event_service import resolve_timezone


def device_today(device: Device, now: datetime | None = None) -> date:
    """按设备时区计算"今日"（S13：服务端唯一允许算日期的场景）。"""
    moment = now or utcnow()
    return moment.astimezone(resolve_timezone(device.timezone)).date()


def upsert_daily_usage(
    db: Session,
    device_id: str,
    item: UsageUploadItem,
    *,
    now: datetime | None = None,
) -> None:
    """🔴 每日用量 Upsert，冲突时取 `MAX(旧, 新)` 防回退（D33 / P0-04）。

    注意 SQLite 的 ``MAX(a, b)`` 是**两参数标量函数**，`func.max(col, excluded)`
    正好映射到该形式。
    """
    moment = now or utcnow()
    stmt = sqlite_insert(DailyUsage).values(
        device_id=device_id,
        usage_date=item.usage_date,
        used_minutes=item.used_minutes,
        bonus_minutes=item.bonus_minutes,
        # 客户端上行从不携带 deducted_minutes（服务端专属审计列），插入时落 0；
        # 冲突更新时不覆盖服务端既有累加值（见下方 set_ 不含该列）。
        deducted_minutes=0,
        parent_minutes=item.parent_minutes,
        break_count=item.break_count,
        base_quota_minutes=item.base_quota_minutes,
        updated_at=moment,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[DailyUsage.device_id, DailyUsage.usage_date],
        set_={
            "used_minutes": func.max(DailyUsage.used_minutes, stmt.excluded.used_minutes),
            "bonus_minutes": func.max(DailyUsage.bonus_minutes, stmt.excluded.bonus_minutes),
            "parent_minutes": func.max(
                DailyUsage.parent_minutes, stmt.excluded.parent_minutes
            ),
            "break_count": func.max(DailyUsage.break_count, stmt.excluded.break_count),
            "base_quota_minutes": stmt.excluded.base_quota_minutes,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    db.execute(stmt)


@retry_on_busy
def upsert_many(
    db: Session,
    device_id: str,
    items: Sequence[UsageUploadItem],
    *,
    now: datetime | None = None,
) -> int:
    """批量 Upsert，返回处理条数。"""
    moment = now or utcnow()
    for item in items:
        upsert_daily_usage(db, device_id, item, now=moment)
    return len(items)


def get_daily(db: Session, device_id: str, day: date) -> DailyUsage | None:
    """查询某设备某天的用量行。"""
    return db.execute(
        select(DailyUsage).where(
            DailyUsage.device_id == device_id, DailyUsage.usage_date == day
        )
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class UsageFacts:
    """单设备某日用量事实（只读汇总，不落库）。"""

    used_minutes: int
    bonus_minutes: int
    parent_minutes: int
    base_quota_minutes: int
    effective_quota_minutes: int
    remaining_minutes: int


def compute_remaining(used: int, bonus: int, base: int) -> int:
    """单设备剩余用量 = ``max(0, base + bonus - used)``（B9 单一真相源）。"""
    return max(0, base + bonus - used)


def read_usage_facts(db: Session, device: Device, target: date, rule=None) -> UsageFacts:
    """读取单设备某日用量事实（无行时按规则基础配额回退，不写库）。

    供 ``summary`` / ``build_device_out`` 复用，消除「无行回退 base_quota_for」
    分支与剩余公式的重复（B9）。``rule`` 由调用方预取传入以避免重复查询。
    """
    if rule is None:
        rule = rule_service.get_rule(db, device.id)
    row = get_daily(db, device.id, target)
    if row is not None:
        base_quota = row.base_quota_minutes
        used = row.used_minutes
        bonus = row.bonus_minutes
        parent = row.parent_minutes
    else:
        base_quota = rule_service.base_quota_for(rule, target) if rule else 0
        used = 0
        bonus = 0
        parent = 0
    effective = base_quota + bonus
    return UsageFacts(
        used_minutes=used,
        bonus_minutes=bonus,
        parent_minutes=parent,
        base_quota_minutes=base_quota,
        effective_quota_minutes=effective,
        remaining_minutes=compute_remaining(used, bonus, base_quota),
    )


def ensure_daily_usage(
    db: Session, device: Device, target: date, *, now: datetime | None = None
) -> DailyUsage:
    """取/建设备某日用量行：无行时按规则基础配额建行（零值），立即 flush。

    供 grant_service / deduction_service 写入路径复用，消除两处逐字相同的
    9 字段 ``DailyUsage(...)`` 构造块（B5）。
    """
    moment = now or utcnow()
    row = get_daily(db, device.id, target)
    if row is None:
        rule = rule_service.get_rule(db, device.id)
        base_quota = rule_service.base_quota_for(rule, target) if rule else 0
        row = DailyUsage(
            device_id=device.id,
            usage_date=target,
            base_quota_minutes=base_quota,
            used_minutes=0,
            bonus_minutes=0,
            deducted_minutes=0,
            parent_minutes=0,
            break_count=0,
            updated_at=moment,
        )
        db.add(row)
        db.flush()
    return row


@retry_on_busy
def reset_daily(
    db: Session,
    device_ids: Sequence[str],
    target_date: date,
    *,
    include_bonus: bool = False,
    now: datetime | None = None,
) -> int:
    """清零指定设备指定业务日的当日用量（方案 A：ack 门控，仅客户端 ack 后调用）。

    清 ``used_minutes`` / ``parent_minutes`` / ``break_count``；默认保留
    ``bonus_minutes``，仅当 ``include_bonus=True`` 才一并清零。返回实际清零的
    设备数（即存在 daily_usage 行的设备数）。

    Args:
        db: 数据库会话。
        device_ids: 目标设备 id 列表（功能一按设备 / 全局重置都走这里）。
        target_date: 业务日期。
        include_bonus: 是否同时清零 bonus_minutes。
        now: 注入的当前时间。

    Returns:
        实际清零的设备数。
    """
    moment = now or utcnow()
    cleared = 0
    for device_id in device_ids:
        row = get_daily(db, device_id, target_date)
        if row is None:
            continue
        row.used_minutes = 0
        row.parent_minutes = 0
        row.break_count = 0
        if include_bonus:
            row.bonus_minutes = 0
        row.updated_at = moment
        cleared += 1
    if cleared:
        db.flush()
    return cleared


def list_usage(
    db: Session, device_id: str, date_from: date, date_to: date
) -> list[DailyUsage]:
    """按区间升序查询用量，缺失日期不补零。

    Raises:
        UnprocessableEntityError: 区间超过 366 天。
    """
    if (date_to - date_from).days + 1 > MAX_USAGE_RANGE_DAYS:
        raise UnprocessableEntityError(
            "查询区间过大，最多 366 天", code=ErrorCode.RANGE_TOO_LARGE
        )
    stmt = (
        select(DailyUsage)
        .where(
            DailyUsage.device_id == device_id,
            DailyUsage.usage_date >= date_from,
            DailyUsage.usage_date <= date_to,
        )
        .order_by(DailyUsage.usage_date.asc())
    )
    return list(db.execute(stmt).scalars().all())


def list_recent(db: Session, device_id: str, limit: int = 7) -> list[DailyUsage]:
    """查询最近 N 天用量，倒序。"""
    stmt = (
        select(DailyUsage)
        .where(DailyUsage.device_id == device_id)
        .order_by(DailyUsage.usage_date.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def to_out(row: DailyUsage) -> DailyUsageOut:
    """把 ORM 用量转成出参（含计算字段）。"""
    return DailyUsageOut(
        usage_date=row.usage_date,
        used_minutes=row.used_minutes,
        bonus_minutes=row.bonus_minutes,
        parent_minutes=row.parent_minutes,
        break_count=row.break_count,
        base_quota_minutes=row.base_quota_minutes,
        effective_quota_minutes=row.effective_quota_minutes,
        remaining_minutes=row.remaining_minutes,
        updated_at=row.updated_at,
    )


def default_range(device: Device, now: datetime | None = None) -> tuple[date, date]:
    """默认查询区间：设备今日往前 30 天（含今日）。"""
    today = device_today(device, now)
    return today - timedelta(days=29), today


def summary(
    db: Session,
    devices: Sequence[Device],
    day: date | None = None,
    *,
    now: datetime | None = None,
) -> UsageSummaryOut:
    """跨设备用量汇总（API.md §6.2）。

    Args:
        db: 数据库会话。
        devices: 目标设备列表。
        day: 指定日期；``None`` 时各设备取自己时区的今日。
        now: 注入的当前时间。
    """
    moment = now or utcnow()
    items: list[DeviceUsageSummary] = []
    total_used = 0
    total_quota = 0
    for device in devices:
        target = day or device_today(device, moment)
        facts = read_usage_facts(db, device, target)
        items.append(
            DeviceUsageSummary(
                device_id=device.id,
                device_name=device.name,
                usage_date=target,
                used_minutes=facts.used_minutes,
                bonus_minutes=facts.bonus_minutes,
                parent_minutes=facts.parent_minutes,
                base_quota_minutes=facts.base_quota_minutes,
                effective_quota_minutes=facts.effective_quota_minutes,
                remaining_minutes=facts.remaining_minutes,
            )
        )
        total_used += facts.used_minutes
        total_quota += facts.effective_quota_minutes
    return UsageSummaryOut(
        date=day,
        total_used_minutes=total_used,
        total_quota_minutes=total_quota,
        devices=items,
    )
