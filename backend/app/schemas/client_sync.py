"""🔴 `/client/sync` 请求与响应模型（API.md §7.2，字段名一字不差）。

系统心脏。任何字段改名都会同时打断桌面客户端与后端的契约。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.core.constants import (
    SYNC_MAX_COMMAND_ACK_ITEMS,
    SYNC_MAX_CREDIT_CONFIRM_ITEMS,
    SYNC_MAX_EVENT_ITEMS,
    SYNC_MAX_EXTENSION_ITEMS,
    SYNC_MAX_USAGE_ITEMS,
)
from app.schemas.command import CommandAckItem
from app.schemas.common import UtcTimestamp
from app.schemas.event import EventUploadItem
from app.schemas.rule import RuleProfileOut

ClientStateLiteral = Literal[
    "DISABLED",
    "GRACE",
    "PARENT",
    "LOCKED_CURFEW",
    "LOCKED_QUOTA",
    "BREAK",
    "IDLE_PAUSED",
    "ACTIVE",
]


class UsageUploadItem(BaseModel):
    """上行用量项（API.md §7.2.1）。"""

    usage_date: date = Field(..., description="业务日期 YYYY-MM-DD")
    used_minutes: int = Field(..., ge=0, description="孩子已用分钟")
    bonus_minutes: int = Field(..., ge=0, description="当日额外额度分钟")
    parent_minutes: int = Field(..., ge=0, description="家长模式分钟")
    break_count: int = Field(..., ge=0, description="强制休息次数")
    base_quota_minutes: int = Field(..., ge=0, description="当日基础配额快照")


class ExtensionUploadItem(BaseModel):
    """上行延时申请项（API.md §7.2.1）。"""

    id: str = Field(..., max_length=36, description="客户端生成的 UUIDv4，即 request_id")
    target_date: date = Field(..., description="目标日期 YYYY-MM-DD")
    requested_minutes: int = Field(..., ge=1, le=240, description="申请分钟数 1-240")
    reason: str = Field("", max_length=500, description="申请理由，可为空串")
    client_created_at: UtcTimestamp = Field(..., description="客户端提交时刻 UTC")


class SyncRequest(BaseModel):
    """`POST /client/sync` 请求体（API.md §7.2.1）。"""

    client_version: str = Field(..., max_length=32, description="客户端版本")
    timezone: str = Field(..., max_length=64, description="IANA 时区")
    os_info: str | None = Field(None, max_length=128, description="操作系统信息")
    device_time: UtcTimestamp = Field(..., description="客户端墙钟 UTC")
    effective_date: date = Field(..., description="客户端算出的生效日（D11，服务端不重算）")
    state: ClientStateLiteral = Field(..., description="当前状态，8 枚举之一")
    state_changed_at: UtcTimestamp = Field(..., description="进入当前状态的 UTC 时刻")
    rule_version: int = Field(..., ge=0, description="本地规则版本")
    usage: list[UsageUploadItem] = Field(
        default_factory=list, max_length=SYNC_MAX_USAGE_ITEMS, description="用量，最多 7 条"
    )
    events: list[EventUploadItem] = Field(
        default_factory=list, max_length=SYNC_MAX_EVENT_ITEMS, description="事件，最多 200 条"
    )
    extension_requests: list[ExtensionUploadItem] = Field(
        default_factory=list,
        max_length=SYNC_MAX_EXTENSION_ITEMS,
        description="延时申请，最多 20 条",
    )
    command_acks: list[CommandAckItem] = Field(
        default_factory=list,
        max_length=SYNC_MAX_COMMAND_ACK_ITEMS,
        description="指令回执，最多 50 条",
    )
    credit_confirms: list[str] = Field(
        default_factory=list,
        max_length=SYNC_MAX_CREDIT_CONFIRM_ITEMS,
        description="已入账的 request_id 数组，最多 50 条",
    )


class DeviceSyncInfo(BaseModel):
    """下行设备信息（API.md §7.2.2）。"""

    id: str = Field(..., description="设备 id")
    name: str = Field(..., description="设备名，家长可能已改名")
    status: Literal["active", "retired"] = Field(..., description="设备状态")
    timezone: str = Field(..., description="设备时区")


class CommandDeliverItem(BaseModel):
    """下行指令项（API.md §7.2.2）。"""

    command_id: str = Field(..., description="指令 UUID")
    type: Literal[
        "UNLOCK_TEMP",
        "PAUSE_ENFORCEMENT",
        "RESUME_ENFORCEMENT",
        "SYNC_NOW",
        "RESET_USAGE",
    ] = Field(..., description="指令类型")
    payload: dict = Field(default_factory=dict, description="指令参数")
    created_at: UtcTimestamp = Field(..., description="创建时间 UTC")
    expires_at: UtcTimestamp = Field(..., description="过期时间 UTC")


class CreditItem(BaseModel):
    """下行待入账额度（API.md §7.2.2）。"""

    request_id: str = Field(..., description="= extension_requests.id")
    target_date: date = Field(..., description="目标日期，客户端须校验等于 effective_date")
    approved_minutes: int = Field(..., description="批准分钟数 1-240")
    requested_minutes: int = Field(..., description="原申请分钟数")
    decided_at: UtcTimestamp = Field(..., description="审批时刻 UTC")


class ExtensionResultItem(BaseModel):
    """下行申请状态回执（API.md §7.2.2）。"""

    id: str = Field(..., description="申请 id")
    status: Literal["pending", "approved", "rejected", "expired", "credited"] = Field(
        ..., description="申请状态"
    )
    approved_minutes: int | None = Field(None, description="批准分钟数")
    decided_at: UtcTimestamp | None = Field(None, description="审批时刻 UTC")
    reject_reason: str | None = Field(
        None, description="PENDING_LIMIT | PARENT_REJECTED | null"
    )


class AcceptedCounts(BaseModel):
    """各类上行数据实际接受条数（API.md §7.2.2）。"""

    usage: int = Field(0, description="成功 upsert 条数")
    events: int = Field(0, description="实际新增条数（去重后）")
    extension_requests: int = Field(0, description="实际新增条数")
    command_acks: int = Field(0, description="实际更新条数")
    credit_confirms: int = Field(0, description="实际置为 credited 的条数")


class SyncResponse(BaseModel):
    """`POST /client/sync` 响应体（API.md §7.2.2）。"""

    server_time: UtcTimestamp = Field(..., description="服务端 UTC 时间")
    device: DeviceSyncInfo = Field(..., description="设备信息")
    rules: RuleProfileOut | None = Field(
        None, description="null 表示规则未变，客户端沿用本地快照"
    )
    commands: list[CommandDeliverItem] = Field(
        default_factory=list, description="本次下发的待执行指令"
    )
    credits: list[CreditItem] = Field(default_factory=list, description="待入账额度")
    extension_results: list[ExtensionResultItem] = Field(
        default_factory=list, description="本设备近期申请的最新状态"
    )
    accepted: AcceptedCounts = Field(..., description="各类上行数据实际接受条数")
    next_sync_seconds: int = Field(..., description="建议下次同步间隔秒")
