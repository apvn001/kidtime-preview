"""仪表盘服务：概览 KPI 与 7/30 天趋势聚合（P0-29 / P0-30）。"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_TIMEZONE, DeviceStatus, OnlineStatus
from app.core.errors import ErrorCode, UnprocessableEntityError
from app.db.base import utcnow
from app.models.device import Device
from app.schemas.dashboard import DashboardOverviewOut, TrendPoint, TrendsOut
from app.services import (
    command_service,
    device_service,
    event_service,
    extension_service,
    rule_service,
    usage_service,
)

ALLOWED_TREND_DAYS = (7, 30)


def overview(db: Session, *, now: datetime | None = None) -> DashboardOverviewOut:
    """首页 5 张 KPI 卡片 + active 设备速览（API.md §9.1）。"""
    moment = now or utcnow()
    devices = device_service.list_devices(db, status=DeviceStatus.ACTIVE.value)
    device_outs = device_service.build_device_out_list(db, devices, now=moment)

    total_used = sum(item.today_used_minutes for item in device_outs)
    total_quota = sum(item.today_effective_quota_minutes for item in device_outs)
    online = sum(1 for item in device_outs if item.online_status == OnlineStatus.ONLINE.value)
    stale = sum(1 for item in device_outs if item.online_status == OnlineStatus.STALE.value)
    offline = sum(
        1 for item in device_outs if item.online_status == OnlineStatus.OFFLINE.value
    )

    return DashboardOverviewOut(
        today_used_minutes=total_used,
        today_quota_minutes=total_quota,
        today_remaining_minutes=max(0, total_quota - total_used),
        total_devices=len(device_outs),
        online_devices=online,
        stale_devices=stale,
        offline_devices=offline,
        pending_approvals=extension_service.count_pending_all(db),
        abnormal_events_24h=event_service.count_abnormal_since(
            db, event_service.hours_ago(24, moment)
        ),
        pending_commands=command_service.count_outstanding(db),
        devices=device_outs,
        server_time=moment,
    )


def _reference_today(
    db: Session, devices: list[Device], device_id: str | None, moment: datetime
) -> date:
    """确定趋势图的参考"今日"。

    指定设备时用该设备时区；否则取全部 active 设备中最常见的时区，
    没有任何设备时回落到 `Asia/Shanghai`（S13 的合理延伸）。
    """
    if device_id is not None and devices:
        return usage_service.device_today(devices[0], moment)
    if devices:
        common = Counter(device.timezone for device in devices).most_common(1)[0][0]
        return moment.astimezone(event_service.resolve_timezone(common)).date()
    return moment.astimezone(event_service.resolve_timezone(DEFAULT_TIMEZONE)).date()


def trends(
    db: Session,
    days: int = 7,
    device_id: str | None = None,
    *,
    now: datetime | None = None,
) -> TrendsOut:
    """🔴 连续日期趋势，缺失日补 0（API.md §9.2）。

    `quota_minutes` 优先取 `daily_usage.base_quota_minutes` 快照；
    无记录时按 D13 用当前规则现算。

    Raises:
        UnprocessableEntityError: `days` 不是 7 或 30。
    """
    if days not in ALLOWED_TREND_DAYS:
        raise UnprocessableEntityError(
            "days 仅接受 7 或 30", code=ErrorCode.VALIDATION_ERROR
        )
    moment = now or utcnow()
    if device_id is not None:
        device = device_service.get_device_or_404(db, device_id)
        devices = [device]
    else:
        devices = device_service.list_devices(db, status=DeviceStatus.ACTIVE.value)

    today = _reference_today(db, devices, device_id, moment)
    start = today - timedelta(days=days - 1)
    dates = [start + timedelta(days=offset) for offset in range(days)]

    used_map: dict[date, int] = {day: 0 for day in dates}
    quota_map: dict[date, int] = {day: 0 for day in dates}
    bonus_map: dict[date, int] = {day: 0 for day in dates}
    parent_map: dict[date, int] = {day: 0 for day in dates}

    for device in devices:
        rule = rule_service.get_rule(db, device.id)
        rows = {
            row.usage_date: row
            for row in usage_service.list_usage(db, device.id, start, today)
        }
        for day in dates:
            row = rows.get(day)
            if row is not None:
                used_map[day] += row.used_minutes
                bonus_map[day] += row.bonus_minutes
                parent_map[day] += row.parent_minutes
                quota_map[day] += row.base_quota_minutes + row.bonus_minutes
            else:
                quota_map[day] += rule_service.base_quota_for(rule, day) if rule else 0

    points = [
        TrendPoint(
            date=day,
            used_minutes=used_map[day],
            quota_minutes=quota_map[day],
            bonus_minutes=bonus_map[day],
            parent_minutes=parent_map[day],
        )
        for day in dates
    ]
    return TrendsOut(days=days, device_id=device_id, points=points)
