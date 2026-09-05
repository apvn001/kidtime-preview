"""事件 Schema（API.md §8.6 / §8.7 / §7.2.1）。"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.constants import EventType
from app.schemas.common import ApiModel, UtcTimestamp

_VALID_EVENT_TYPES = {item.value for item in EventType}
MAX_PAYLOAD_BYTES = 4096


class EventOut(ApiModel):
    """事件出参。"""

    id: int = Field(..., description="事件 id")
    client_event_id: str | None = Field(None, description="客户端事件 UUID")
    device_id: str | None = Field(None, description="设备 id")
    device_name: str | None = Field(None, description="设备名称")
    user_id: int | None = Field(None, description="用户 id")
    username: str | None = Field(None, description="用户名")
    actor_name: str | None = Field(
        None,
        description="操作人姓名快照（V5）。账号被删后 user_id 置空，此字段仍保留原用户名",
    )
    event_type: str = Field(..., description="事件类型，27 种之一")
    severity: Literal["info", "warning", "error"] = Field(..., description="事件级别")
    payload: dict[str, Any] = Field(default_factory=dict, description="事件负载")
    occurred_at: UtcTimestamp = Field(..., description="设备侧发生时刻 UTC")
    created_at: UtcTimestamp = Field(..., description="服务端落库时刻 UTC")


class EventUploadItem(BaseModel):
    """客户端上传的单条事件（API.md §7.2.1）。"""

    client_event_id: str = Field(..., max_length=36, description="客户端 UUIDv4，用于去重")
    event_type: str = Field(..., max_length=32, description="事件类型，27 种之一")
    severity: Literal["info", "warning", "error"] = Field(..., description="事件级别")
    payload: dict[str, Any] = Field(default_factory=dict, description="事件负载，≤4KB")
    occurred_at: UtcTimestamp = Field(..., description="设备侧发生时刻 UTC")

    @field_validator("event_type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        """校验事件类型在枚举内（V5 后共 27 个）。"""
        if value not in _VALID_EVENT_TYPES:
            raise ValueError("事件类型不合法，必须是 EventType 枚举之一")
        return value

    @field_validator("payload")
    @classmethod
    def _check_payload_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        """校验负载序列化后不超过 4KB。"""
        encoded = json.dumps(value or {}, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload 序列化后不得超过 4KB")
        return value
