"""远程指令 Schema（API.md §8.4 / §8.5 / §7.4）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.core.constants import (
    UNLOCK_TEMP_DEFAULT_MINUTES,
    UNLOCK_TEMP_MAX_MINUTES,
    UNLOCK_TEMP_MIN_MINUTES,
)
from app.schemas.common import ApiModel, UtcTimestamp

CommandTypeLiteral = Literal[
    "UNLOCK_TEMP",
    "PAUSE_ENFORCEMENT",
    "RESUME_ENFORCEMENT",
    "SYNC_NOW",
    "RESET_USAGE",
    "SET_LOCK_STYLE",
]


class CommandCreateIn(BaseModel):
    """`POST /commands` 入参，支持批量。"""

    device_ids: list[str] = Field(
        ..., min_length=1, max_length=50, description="目标设备 id，1-50 个"
    )
    type: CommandTypeLiteral = Field(..., description="指令类型")
    payload: dict[str, Any] | None = Field(
        None, description="UNLOCK_TEMP 时必填 {\"minutes\": int}，范围 5-120"
    )

    @model_validator(mode="after")
    def _check_payload(self) -> "CommandCreateIn":
        """校验 `UNLOCK_TEMP` 的 minutes 取值（D19）；`RESET_USAGE` 透传 payload。"""
        if self.type == "UNLOCK_TEMP":
            payload = self.payload or {}
            minutes = payload.get("minutes", UNLOCK_TEMP_DEFAULT_MINUTES)
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                raise ValueError("UNLOCK_TEMP 需要整数 minutes 参数")
            if minutes < UNLOCK_TEMP_MIN_MINUTES or minutes > UNLOCK_TEMP_MAX_MINUTES:
                raise ValueError(
                    f"minutes 必须在 {UNLOCK_TEMP_MIN_MINUTES}-{UNLOCK_TEMP_MAX_MINUTES} 之间"
                )
            object.__setattr__(self, "payload", {"minutes": minutes})
            return self
        if self.type == "RESET_USAGE":
            # 仅管理员经内部服务层下发（R2）。公开端点走到这里会被 create_batch
            # 的 ADMIN_ONLY 校验拦截为 403；此处仅透传 target_date 等参数。
            object.__setattr__(self, "payload", self.payload or {})
            return self
        object.__setattr__(self, "payload", {})
        return self


class LockStyleIn(BaseModel):
    """`POST /devices/{id}/lock-style` 入参（v1.4.2 锁屏外观三样式，admin-only）。"""

    style: Literal["default", "eyecare", "eyecare2"] = Field(
        "default",
        description="锁屏外观样式：default 默认蓝黑 / eyecare 护眼浅色 / eyecare2 护眼2 暖橙米色",
    )
    allow_child_switch: bool = Field(
        False,
        description="是否允许孩子自主点击右下角浮窗轮换锁屏外观（家长未勾选时点击仅抖动反馈）",
    )


class CommandOut(ApiModel):
    """指令出参（API.md §8.4）。"""

    command_id: str = Field(..., description="指令 UUID，逐设备独立（D48）")
    device_id: str = Field(..., description="设备 id")
    device_name: str = Field(..., description="设备名称")
    type: CommandTypeLiteral = Field(..., description="指令类型")
    payload: dict[str, Any] = Field(default_factory=dict, description="指令参数")
    status: Literal["pending", "delivered", "acked", "expired"] = Field(
        ..., description="指令状态"
    )
    result: dict[str, Any] | None = Field(None, description="执行结果")
    batch_id: str | None = Field(None, description="批量分组 id")
    created_by: int | None = Field(
        None,
        description="下发人用户 id；账号被删后为 NULL（V5 §2.1 SET NULL）",
    )
    created_by_username: str | None = Field(
        None, description="下发人当前用户名；账号被删后为 NULL"
    )
    created_by_name: str | None = Field(
        None, description="下发人姓名快照（V5 §2.2）：删账号时写入，历史可追溯"
    )
    created_at: UtcTimestamp = Field(..., description="创建时间 UTC")
    delivered_at: UtcTimestamp | None = Field(None, description="下发时间 UTC")
    acked_at: UtcTimestamp | None = Field(None, description="回执时间 UTC")
    expires_at: UtcTimestamp = Field(..., description="过期时间 UTC，created + 30min")


class CommandCreateFailure(BaseModel):
    """批量创建中失败的设备项。"""

    device_id: str = Field(..., description="设备 id")
    code: str = Field(..., description="错误码")
    message: str = Field(..., description="中文错误描述")


class CommandBatchOut(BaseModel):
    """`POST /commands` 出参。"""

    batch_id: str = Field(..., description="本次批量的分组 UUID")
    commands: list[CommandOut] = Field(default_factory=list, description="成功创建的指令")
    failed: list[CommandCreateFailure] = Field(
        default_factory=list, description="未能创建的设备"
    )


class CommandAckItem(BaseModel):
    """单条指令回执（API.md §7.2.1）。"""

    command_id: str = Field(..., max_length=36, description="指令 UUID")
    status: Literal["success", "failed"] = Field(..., description="执行结果")
    error: str | None = Field(None, max_length=255, description="失败原因")
    executed_at: UtcTimestamp = Field(..., description="执行时刻 UTC")


class CommandAckIn(BaseModel):
    """`POST /client/command-ack` 入参。"""

    acks: list[CommandAckItem] = Field(
        ..., min_length=1, max_length=50, description="回执列表，1-50 项"
    )


class CommandAckOut(BaseModel):
    """`POST /client/command-ack` 出参。"""

    acked: list[str] = Field(default_factory=list, description="成功回执的指令 id")
    ignored: list[str] = Field(
        default_factory=list, description="已 acked / expired / 不属本设备"
    )
