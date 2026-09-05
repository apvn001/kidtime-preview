"""重置用量服务（V7 §2.3 / §2.5）：建 RESET_USAGE 指令 + 服务端立即清零。

🔴 关键语义：本服务在创建指令的同时**立即清零服务端 daily_usage**，使网页后台
实时看到效果；同时仍向客户端下发 `RESET_USAGE` 指令，让客户端在下次同步时
也执行本地清零。旧客户端若不支持该指令，ack 会返回失败，但服务端状态已经
生效，不会出现「点了重置但用量不变」的情况。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    CommandStatus,
    CommandType,
    DeviceStatus,
    EventSeverity,
    EventType,
    RESET_SCOPES,
)
from app.core.errors import ErrorCode, UnprocessableEntityError
from app.core.security import new_uuid
from app.db.base import utcnow
from app.models.command import RemoteCommand
from app.models.device import Device
from app.models.user import User
from app.services import event_service, usage_service


def _reset_one(
    db: Session,
    device: Device,
    target_date: date,
    *,
    batch_id: str,
    user: User,
    moment: datetime,
    include_bonus: bool = False,
) -> RemoteCommand:
    """为单台设备创建一条 `RESET_USAGE` 待下发指令（不清零用量本身）。

    `include_bonus` 透传进指令 payload：客户端 ack 时回传给服务端，
    `_apply_reset_on_ack` 据此决定是否一并清零 `bonus_minutes`（L3 高危开关）。
    """
    command = RemoteCommand(
        id=new_uuid(),
        device_id=device.id,
        type=CommandType.RESET_USAGE.value,
        payload_json=json.dumps(
            {
                "target_date": target_date.isoformat(),
                "include_bonus": bool(include_bonus),
            },
            ensure_ascii=False,
        ),
        status=CommandStatus.PENDING.value,
        batch_id=batch_id,
        created_by=user.id,
        created_by_name=user.username,
        created_at=moment,
        expires_at=moment + timedelta(minutes=get_settings().COMMAND_EXPIRE_MINUTES),
    )
    db.add(command)
    db.flush()
    return command


def request_reset(
    db: Session,
    scope: str,
    *,
    device_id: str | None = None,
    target_date: date | None = None,
    include_bonus: bool = False,
    user: User,
    now: datetime | None = None,
) -> tuple[str, list[RemoteCommand], list[Any], list[dict[str, str]]]:
    """下发「重置当日用量」指令（按设备维度；不含 user）。

    Args:
        db: 数据库会话。
        scope: ``"device"`` 或 ``"global"``。
        device_id: ``scope=device`` 时必填。
        target_date: 目标日期；缺省为各设备时区今日。
        include_bonus: 是否一并清零已批准额度（L3 高危，默认 False）。
        user: 操作管理员。
        now: 注入当前时间（G8）。

    Returns:
        ``(batch_id, 已创建指令, 失败设备, 已下发列表)``。

    Raises:
        UnprocessableEntityError: scope 非法 / device 维度缺 device_id / 设备已停用。
    """
    moment = now or utcnow()
    if scope not in RESET_SCOPES:
        raise UnprocessableEntityError(
            "不支持的重置范围", code=ErrorCode.USER_SCOPE_NOT_SUPPORTED
        )

    if scope == "device":
        if not device_id:
            raise UnprocessableEntityError(
                "device 维度重置必须指定 device_id", code=ErrorCode.RESET_SCOPE_REQUIRED
            )
        device = db.get(Device, device_id)
        if device is None:
            from app.schemas.command import CommandCreateFailure

            failed = [
                CommandCreateFailure(
                    device_id=device_id, code=ErrorCode.DEVICE_NOT_FOUND, message="设备不存在"
                )
            ]
            return new_uuid(), [], failed, []
        if device.status != DeviceStatus.ACTIVE.value:
            from app.schemas.command import CommandCreateFailure

            failed = [
                CommandCreateFailure(
                    device_id=device.id,
                    code=ErrorCode.DEVICE_RETIRED,
                    message="设备已停用",
                )
            ]
            return new_uuid(), [], failed, []
        devices = [device]
    else:  # global：所有在管设备
        devices = list(
            db.execute(
                select(Device).where(Device.status == DeviceStatus.ACTIVE.value)
            ).scalars().all()
        )

    batch_id = new_uuid()
    created: list[RemoteCommand] = []
    failed: list[Any] = []
    dispatched: list[dict[str, str]] = []

    reset_target_dates: list[date] = []
    for device in devices:
        td = target_date or usage_service.device_today(device, moment)
        reset_target_dates.append(td)
        command = _reset_one(
            db,
            device,
            td,
            batch_id=batch_id,
            user=user,
            moment=moment,
            include_bonus=include_bonus,
        )
        created.append(command)
        dispatched.append({"device_id": device.id, "command_id": command.id})

    # 🔴 服务端立即清零：只要存在 daily_usage 行就清，不依赖客户端 ack。
    # 不同设备可能处于不同时区，但管理员传入的 target_date 是统一业务日；
    # 缺省情况下按各设备时区今日分别清零（逐个调用）。
    for device, td in zip(devices, reset_target_dates):
        usage_service.reset_daily(
            db, [device.id], td, include_bonus=include_bonus, now=moment
        )
        # 写「已清零」审计：服务端状态已生效，客户端 ack 仅作为冗余通知。
        event_service.record(
            db,
            EventType.USAGE_RESET,
            severity=EventSeverity.INFO if not include_bonus else EventSeverity.WARNING,
            device_id=device.id,
            user_id=user.id,
            actor_name=user.username,
            payload={
                "phase": "cleared",
                "scope": scope,
                "command_id": next(
                    (c.id for c in created if c.device_id == device.id), None
                ),
                "target_date": td.isoformat(),
                "include_bonus": include_bonus,
            },
            now=moment,
        )

    return batch_id, created, failed, dispatched
