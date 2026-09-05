"""延时申请 Schema（API.md §8.1–§8.3 / §7.3）。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel, UtcTimestamp


class ExtensionRequestOut(ApiModel):
    """延时申请出参（API.md §8.1）。"""

    id: str = Field(..., description="申请 UUID，由客户端生成")
    device_id: str = Field(..., description="设备 id")
    device_name: str = Field(..., description="设备名称，冗余避免前端二次查询")
    target_date: date = Field(..., description="目标日期 YYYY-MM-DD")
    requested_minutes: int = Field(..., description="申请分钟数")
    reason: str = Field(..., description="申请理由")
    status: Literal["pending", "approved", "rejected", "expired", "credited"] = Field(
        ..., description="申请状态"
    )
    approved_minutes: int | None = Field(None, description="批准分钟数")
    decided_by: int | None = Field(None, description="审批人用户 id")
    decided_by_username: str | None = Field(None, description="审批人当前用户名")
    decided_by_name: str | None = Field(
        None, description="审批人姓名快照（V5 §2.2）：删账号时写入，历史可追溯"
    )
    decided_at: UtcTimestamp | None = Field(None, description="审批时间 UTC")
    credited_at: UtcTimestamp | None = Field(None, description="入账确认时间 UTC")
    created_at: UtcTimestamp = Field(..., description="服务端接收时刻 UTC")
    client_created_at: UtcTimestamp | None = Field(None, description="客户端提交时刻 UTC")


class ApproveIn(BaseModel):
    """`POST /extension-requests/{id}/approve` 入参。"""

    approved_minutes: int = Field(
        ..., ge=1, le=240, description="批准分钟数 1-240，可不同于申请值（D29）"
    )


class RejectIn(BaseModel):
    """`POST /extension-requests/{id}/reject` 入参。"""

    reason: str | None = Field(None, max_length=200, description="拒绝理由")


class ConfirmCreditIn(BaseModel):
    """`POST /client/confirm-credit` 入参。"""

    request_ids: list[str] = Field(
        ..., min_length=1, max_length=50, description="待确认的申请 id，1-50 个"
    )


class ConfirmCreditOut(BaseModel):
    """`POST /client/confirm-credit` 出参。"""

    confirmed: list[str] = Field(
        default_factory=list, description="本次由 approved 变为 credited 的 id"
    )
    already_credited: list[str] = Field(
        default_factory=list, description="此前已是 credited（幂等成功）"
    )
    not_found: list[str] = Field(
        default_factory=list, description="不存在或不属于本设备"
    )
