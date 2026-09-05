"""规则 Schema（API.md §5）。"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ApiModel, UtcTimestamp

_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _validate_hhmm(value: str) -> str:
    """校验 `HH:MM` 24 小时制时刻。"""
    text = (value or "").strip()
    if not _HHMM_RE.match(text):
        raise ValueError("时刻格式必须为 HH:MM（24 小时制），例如 07:00")
    return text


class RuleProfileOut(ApiModel):
    """规则出参（API.md §5.1）。"""

    device_id: str = Field(..., description="设备 id")
    weekday_quota_minutes: int = Field(..., description="工作日配额分钟")
    weekend_quota_minutes: int = Field(..., description="周末配额分钟")
    allowed_start: str = Field(..., description="允许时段开始 HH:MM")
    allowed_end: str = Field(..., description="允许时段结束 HH:MM")
    continuous_limit_minutes: int = Field(..., description="连续使用上限分钟")
    break_minutes: int = Field(..., description="强制休息分钟")
    idle_minutes: int = Field(..., description="空闲判定分钟")
    reminder_points: list[int] = Field(..., description="提醒节点，去重降序")
    parent_mode_timeout_minutes: int = Field(..., description="家长模式超时分钟")
    sync_interval_seconds: int = Field(..., description="同步间隔秒")
    enforcement_enabled: bool = Field(..., description="是否启用管控")
    version: int = Field(..., description="规则版本，单调递增")
    template_id: int | None = Field(None, description="规则模板溯源 id（信息性）")
    updated_at: UtcTimestamp = Field(..., description="更新时间 UTC")
    updated_by: int | None = Field(None, description="更新人用户 id")
    updated_by_name: str | None = Field(
        None, description="更新人姓名快照（V5 §2.2）：删账号时写入，历史可追溯"
    )


class RuleProfileIn(BaseModel):
    """规则入参（API.md §5.2），全量替换语义，除只读字段外全部必填。"""

    weekday_quota_minutes: int = Field(..., ge=0, le=1440, description="工作日配额分钟 0-1440")
    weekend_quota_minutes: int = Field(..., ge=0, le=1440, description="周末配额分钟 0-1440")
    allowed_start: str = Field(..., description="允许时段开始 HH:MM")
    allowed_end: str = Field(..., description="允许时段结束 HH:MM")
    continuous_limit_minutes: int = Field(
        ..., ge=10, le=240, description="连续使用上限分钟 10-240"
    )
    break_minutes: int = Field(..., ge=1, le=60, description="强制休息分钟 1-60")
    idle_minutes: int = Field(..., ge=1, le=60, description="空闲判定分钟 1-60")
    reminder_points: list[int] = Field(
        ..., max_length=5, description="提醒节点，最多 5 个节点，每项取值 1-60"
    )
    parent_mode_timeout_minutes: int = Field(
        ..., ge=5, le=120, description="家长模式超时分钟 5-120"
    )
    sync_interval_seconds: int = Field(..., ge=15, le=300, description="必须在 15-300 之间")
    enforcement_enabled: bool = Field(..., description="是否启用管控")

    @field_validator("allowed_start", "allowed_end")
    @classmethod
    def _check_time(cls, value: str) -> str:
        """校验时刻格式。"""
        return _validate_hhmm(value)

    @field_validator("reminder_points")
    @classmethod
    def _check_points(cls, value: list[int]) -> list[int]:
        """校验提醒节点取值范围（服务端稍后去重降序存储，D26）。"""
        if any((not isinstance(item, int)) or item < 1 or item > 60 for item in value):
            raise ValueError("最多 5 个节点，每项取值 1-60")
        return value
