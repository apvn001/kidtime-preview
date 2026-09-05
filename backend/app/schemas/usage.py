"""用量 Schema（API.md §6 / V7 §2.3）。"""

from __future__ import annotations

from datetime import date as DateType
from typing import Literal

from pydantic import BaseModel, Field

from app.core.constants import DEDUCT_MINUTES_MAX
from app.schemas.command import CommandCreateFailure, CommandOut
from app.schemas.common import ApiModel, UtcTimestamp


class DailyUsageOut(ApiModel):
    """每日用量出参（API.md §6.1）。"""

    usage_date: DateType = Field(..., description="业务日期 YYYY-MM-DD")
    used_minutes: int = Field(..., description="孩子已用分钟")
    bonus_minutes: int = Field(..., description="当日额外额度分钟")
    parent_minutes: int = Field(..., description="家长模式分钟，审计用不占配额")
    break_count: int = Field(..., description="强制休息次数")
    base_quota_minutes: int = Field(..., description="当日基础配额快照")
    effective_quota_minutes: int = Field(..., description="base + bonus")
    remaining_minutes: int = Field(..., description="max(0, effective - used)")
    updated_at: UtcTimestamp = Field(..., description="更新时间 UTC")


class DeviceUsageSummary(BaseModel):
    """单设备用量汇总项（API.md §6.2）。"""

    device_id: str = Field(..., description="设备 id")
    device_name: str = Field(..., description="设备名称")
    usage_date: DateType = Field(..., description="业务日期")
    used_minutes: int = Field(..., description="已用分钟")
    bonus_minutes: int = Field(..., description="额外额度分钟")
    parent_minutes: int = Field(..., description="家长模式分钟")
    base_quota_minutes: int = Field(..., description="基础配额分钟")
    effective_quota_minutes: int = Field(..., description="有效配额分钟")
    remaining_minutes: int = Field(..., description="剩余分钟")


class UsageSummaryOut(BaseModel):
    """跨设备用量汇总（API.md §6.2）。"""

    date: DateType | None = Field(None, description="回显查询日期；缺省时为各设备本地今日")
    total_used_minutes: int = Field(..., description="全部设备合计已用")
    total_quota_minutes: int = Field(..., description="全部设备合计有效配额")
    devices: list[DeviceUsageSummary] = Field(
        default_factory=list, description="各设备明细"
    )


class ResetUsageIn(BaseModel):
    """`POST /usage/reset` 入参（V7 §2.3）。"""

    scope: Literal["device", "global"] = Field(..., description="重置范围：device 或 global（本版不含 user）")
    device_id: str | None = Field(None, description="scope=device 时必填")
    target_date: DateType | None = Field(None, description="目标日期；缺省为各设备时区今日")
    include_bonus: bool = Field(
        False, description="高危开关：是否一并清零已批准的延时额度（bonus_minutes），默认 false"
    )
    confirm_text: str | None = Field(
        None,
        description=(
            "scope=global 时必填，须严格等于 \"RESET\"（L3 高危逐字确认）。"
            "前端逐字输入是第一道闸门，服务端在端点层二次校验防绕过。"
        ),
    )


class ResetUsageOut(BaseModel):
    """重置当日用量出参（V7 修正：服务端立即清零，客户端 ack 作为冗余通知）。

    扁平结构与前端 ``ResetUsageOut`` 类型对齐（frontend/src/types/api.ts）。
    """

    scope: Literal["device", "global"] = Field(..., description="重置范围")
    target_date: DateType = Field(..., description="目标业务日期 YYYY-MM-DD")
    total_devices: int = Field(..., description="成功下发的设备数")
    command_ids: list[str] = Field(
        default_factory=list, description="已下发的 RESET_USAGE 指令 id 列表"
    )
    status: Literal["ok"] = Field(
        ..., description="恒为 ok：服务端已立即清零，客户端下次同步会再次执行本地清零"
    )
    message: str = Field(..., description="人类可读提示文案")


class DeductUsageIn(BaseModel):
    """`POST /usage/deduct` 入参（V7 功能三：扣减用量）。

    对指定设备当天已用/剩余用量做主动扣减：服务端立即 ``used_minutes += applied``
    （剩余随之下降），并下发 ``DEDUCT_USAGE`` 指令让客户端本地同步。
    """

    device_id: str = Field(..., description="目标设备 id")
    minutes: int = Field(
        ..., ge=1, le=DEDUCT_MINUTES_MAX, description=f"扣减分钟数 1-{DEDUCT_MINUTES_MAX}"
    )
    reason: str = Field("", max_length=500, description="扣减事由（可选）")


class DeductUsageOut(BaseModel):
    """`POST /usage/deduct` 出参（扁平结构对齐前端）。

    ``applied_minutes`` 可能小于 ``requested_minutes``（越界钳制，``clamped=True``）；
    ``applied_minutes=0`` 时表示剩余已为 0，未产生扣减、未下发指令。
    """

    device_id: str = Field(..., description="目标设备 id")
    target_date: DateType = Field(..., description="目标业务日期 YYYY-MM-DD")
    requested_minutes: int = Field(..., description="请求扣减分钟数")
    applied_minutes: int = Field(..., description="实际扣减分钟数（已钳制）")
    clamped: bool = Field(..., description="是否因越界被钳制")
    command_id: str | None = Field(None, description="下发的 DEDUCT_USAGE 指令 id（applied=0 时为 null）")
    remaining_minutes: int = Field(..., description="扣减后的剩余分钟")
    status: Literal["ok"] = Field(..., description="恒为 ok：服务端已立即生效")
    message: str = Field(..., description="人类可读提示文案")
