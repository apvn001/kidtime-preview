"""仪表盘 Schema（API.md §9.1 / §9.2）。"""

from __future__ import annotations

from datetime import date as DateType

from pydantic import BaseModel, Field

from app.schemas.common import UtcTimestamp
from app.schemas.device import DeviceOut


class DashboardOverviewOut(BaseModel):
    """首页 KPI 概览（API.md §9.1）。"""

    today_used_minutes: int = Field(..., description="全部 active 设备今日合计已用")
    today_quota_minutes: int = Field(..., description="全部 active 设备今日合计有效配额")
    today_remaining_minutes: int = Field(..., description="max(0, quota - used)")
    total_devices: int = Field(..., description="active 设备数")
    online_devices: int = Field(..., description="在线设备数")
    stale_devices: int = Field(..., description="失联设备数")
    offline_devices: int = Field(..., description="离线设备数")
    pending_approvals: int = Field(..., description="全部 pending 申请数")
    abnormal_events_24h: int = Field(..., description="近 24h warning/error 事件数")
    pending_commands: int = Field(..., description="pending/delivered 指令数")
    devices: list[DeviceOut] = Field(default_factory=list, description="全部 active 设备")
    server_time: UtcTimestamp = Field(..., description="服务端 UTC 时间")


class TrendPoint(BaseModel):
    """趋势图单点。"""

    date: DateType = Field(..., description="日期 YYYY-MM-DD")
    used_minutes: int = Field(..., description="已用分钟")
    quota_minutes: int = Field(..., description="有效配额分钟")
    bonus_minutes: int = Field(..., description="额外额度分钟")
    parent_minutes: int = Field(..., description="家长模式分钟")


class TrendsOut(BaseModel):
    """趋势响应（API.md §9.2）。"""

    days: int = Field(..., description="回显天数，7 或 30")
    device_id: str | None = Field(None, description="回显设备 id，null 表示全部设备合计")
    points: list[TrendPoint] = Field(
        default_factory=list, description="连续日期点，缺失日补 0"
    )
