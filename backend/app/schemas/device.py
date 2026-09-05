"""设备 Schema（API.md §4 / §7.1）。"""

from __future__ import annotations

from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ApiModel, UtcTimestamp
from app.schemas.rule import RuleProfileOut
from app.schemas.usage import DailyUsageOut


class PairingCodeIn(BaseModel):
    """`POST /devices/pairing-codes` 入参。"""

    device_id: str | None = Field(
        None, max_length=36, description="提供则为重新配对（D05），绑定到已有设备"
    )
    note: str | None = Field(None, max_length=64, description="备注，仅供家长自己识别")


class PairingCodeOut(ApiModel):
    """配对码出参，`code` 明文仅此一次返回。"""

    id: int = Field(..., description="配对码记录 id")
    code: str = Field(..., description="明文配对码，格式 XXXX-XXXX，仅此一次返回")
    expires_at: UtcTimestamp = Field(..., description="过期时间 UTC，created + 15min")
    device_id: str | None = Field(None, description="重新配对时回显目标设备 id")
    created_at: UtcTimestamp = Field(..., description="创建时间 UTC")


class DeviceOut(ApiModel):
    """设备列表核心 DTO（API.md §4.2）。"""

    id: str = Field(..., description="设备 UUID")
    name: str = Field(..., description="展示名")
    timezone: str = Field(..., description="IANA 时区")
    status: Literal["active", "retired"] = Field(..., description="设备状态")
    online_status: Literal["online", "stale", "offline"] = Field(
        ..., description="在线状态计算值（D08）"
    )
    client_version: str | None = Field(None, description="客户端版本")
    os_info: str | None = Field(None, description="操作系统信息")
    last_seen_at: UtcTimestamp | None = Field(None, description="最后心跳时间 UTC")
    last_state: str | None = Field(None, description="最后状态，8 状态之一")
    last_state_at: UtcTimestamp | None = Field(None, description="进入最后状态的时间 UTC")
    today_date: date = Field(..., description="设备时区今日")
    today_used_minutes: int = Field(..., description="今日已用分钟")
    today_bonus_minutes: int = Field(..., description="今日额外额度分钟")
    today_parent_minutes: int = Field(..., description="今日家长模式分钟")
    today_base_quota_minutes: int = Field(..., description="今日基础配额分钟（D13）")
    today_effective_quota_minutes: int = Field(..., description="base + bonus（D32）")
    today_remaining_minutes: int = Field(..., description="max(0, effective - used)")
    enforcement_enabled: bool = Field(..., description="是否启用管控")
    pending_extension_count: int = Field(..., description="该设备今日待审批数")
    paired_at: UtcTimestamp | None = Field(None, description="配对时间 UTC")
    created_at: UtcTimestamp = Field(..., description="创建时间 UTC")


class DeviceDetailOut(DeviceOut):
    """设备详情（API.md §4.3）。"""

    rules: RuleProfileOut = Field(..., description="当前规则")
    recent_usage: list[DailyUsageOut] = Field(
        default_factory=list, description="近 7 天用量，倒序"
    )
    active_credential_issued_at: UtcTimestamp | None = Field(
        None, description="当前有效凭证签发时间"
    )
    credential_count: int = Field(..., description="历史凭证总数")


class DeviceUnretireOut(DeviceOut):
    """`POST /devices/{device_id}/unretire` 出参（增量架构设计 §5.3）。

    `needs_repair` 恒为 `True` 的场景：停用时凭证已被吊销，恢复只还原后台的管理
    可见性，**不还原客户端连接能力**，必须重新配对。
    """

    needs_repair: bool = Field(
        ...,
        description="是否需要重新配对才能恢复同步（当前无有效凭证时为 true）",
    )


class DeviceUpdateIn(BaseModel):
    """`PATCH /devices/{device_id}` 入参（改名 D07 + 改时区，V5 §4.2）。"""

    name: str = Field(..., min_length=1, max_length=64, description="新的设备展示名")
    timezone: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="IANA 时区名；缺省表示不修改。改动下次同步后由客户端生效（共享知识 12）",
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """去首尾空白后必须非空。"""
        text = (value or "").strip()
        if not text:
            raise ValueError("设备名称不能为空")
        return text

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        """校验 IANA 时区名合法（非法直接 422，不静默回落）。"""
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("时区不能为空字符串")
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError("时区不是合法的 IANA 名称") from exc
        return text


class DeviceDeleteIn(BaseModel):
    """`DELETE /devices/{device_id}` 请求体（V5 §4.3）。

    L3 逐字确认的服务端二次校验载体：前端控制按钮可用性，后端再比一次，
    防止绕过前端直接调用 API。
    """

    confirm_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="必须与设备当前名称完全一致（含大小写与空格）",
    )


class DeviceDeleteImpactOut(BaseModel):
    """`GET /devices/{device_id}/delete-impact` 出参（V5 §4.3）。

    供 L3 弹窗展示**真实条数**，让管理员知道自己在删什么。
    """

    device_id: str = Field(..., description="设备 UUID")
    device_name: str = Field(..., description="设备展示名")
    status: Literal["active", "retired"] = Field(..., description="设备状态")
    usage_days: int = Field(..., description="daily_usage 条数（用量天数）")
    event_count: int = Field(..., description="event_logs 条数")
    extension_count: int = Field(..., description="extension_requests 条数")
    command_count: int = Field(..., description="remote_commands 条数")
    rule_profile_count: int = Field(..., description="rule_profiles 条数（0 或 1）")
    pairing_code_count: int = Field(..., description="pairing_codes 条数")
    credential_count: int = Field(..., description="device_credentials 条数")
    first_record_at: UtcTimestamp | None = Field(
        None, description="最早一条事件时间 UTC，无事件时回落到配对时间"
    )
    last_record_at: UtcTimestamp | None = Field(
        None, description="最晚一条事件时间 UTC，无事件时回落到最后心跳时间"
    )


class RevokeCredentialsIn(BaseModel):
    """`POST /devices/{device_id}/revoke-credentials` 入参。"""

    reason: str | None = Field(
        None, max_length=64, description="撤销原因，默认 manual_revoke"
    )


class RevokeCredentialsOut(BaseModel):
    """撤销凭证出参。"""

    device_id: str = Field(..., description="设备 id")
    revoked_count: int = Field(..., description="本次撤销的凭证条数")
    revoked_at: UtcTimestamp = Field(..., description="撤销时刻 UTC")


class ClientPairIn(BaseModel):
    """`POST /client/pair` 入参（API.md §7.1）。"""

    code: str = Field(..., min_length=1, max_length=32, description="配对码，接受带或不带连字符")
    device_name: str = Field(..., min_length=1, max_length=64, description="设备展示名")
    timezone: str = Field(..., min_length=1, max_length=64, description="IANA 时区")
    client_version: str = Field(..., min_length=1, max_length=32, description="客户端版本")
    os_info: str | None = Field(None, max_length=128, description="操作系统信息")


class ClientPairOut(BaseModel):
    """`POST /client/pair` 出参，`device_secret` 明文仅此一次返回。"""

    device_id: str = Field(..., description="设备 UUID，重新配对时返回原有 id")
    device_secret: str = Field(..., description="设备密钥明文，仅此一次返回")
    device_name: str = Field(..., description="设备展示名回显")
    timezone: str = Field(..., description="设备时区回显")
    rules: RuleProfileOut = Field(..., description="初始规则快照，客户端首次即可离线工作")
    server_time: UtcTimestamp = Field(..., description="服务端 UTC 时间")
    paired_at: UtcTimestamp = Field(..., description="配对时刻 UTC")
