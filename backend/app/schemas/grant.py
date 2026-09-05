"""下发时长 Schema（§3.3）。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.core.constants import GrantStatus
from app.schemas.common import ApiModel, UtcTimestamp


class GrantCreateIn(BaseModel):
    """`POST /grants` 入参（管理员）。"""

    device_id: str = Field(..., description="目标设备 id")
    minutes: int = Field(..., ge=1, le=240, description="下发分钟数 1-240（与延时申请一致）")
    reason: str = Field("", max_length=500, description="下发事由（可选）")
    target_date: date | None = Field(None, description="目标业务日期，缺省为今日")


class TimeGrantOut(ApiModel):
    """下发记录出参。"""

    id: str = Field(..., description="grant UUID")
    device_id: str = Field(..., description="设备 id")
    device_name: str = Field(..., description="设备名称（冗余避免前端二次查询）")
    target_date: date = Field(..., description="目标业务日期 YYYY-MM-DD")
    minutes: int = Field(..., description="下发分钟数")
    reason: str = Field(..., description="事由")
    status: GrantStatus = Field(..., description="状态")
    granted_by: int | None = Field(None, description="下发人用户 id")
    granted_by_name: str | None = Field(None, description="下发人姓名快照（V5 §2.2）")
    granted_at: UtcTimestamp = Field(..., description="下发时间 UTC")
    credited_at: UtcTimestamp | None = Field(None, description="客户端入账确认时间 UTC")


class GrantListQuery(BaseModel):
    """`GET /grants` 查询参数。"""

    device_id: str | None = Field(None, description="按设备过滤")
    status: str | None = Field(None, description="按状态过滤（逗号分隔多值）")
