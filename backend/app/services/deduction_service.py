"""扣减用量服务（V7 §2.2 / §3.5）：服务端立即生效 + 客户端冗余指令。

与「下发时长」相反，扣减用量是「让客户端本地 `remaining` 下降」：本质是
把 `used` 视为增加 `applied` 分钟。服务端在 ``deduct()`` 内**立即**把
``daily_usage.used_minutes += applied``（唯一真相源，网页后台实时可见），
同时下发 ``DEDUCT_USAGE`` 指令让客户端本地 ``used_seconds += applied*60``。

与 ``RESET_USAGE`` 的时序差异（ack 门控）：
  - ``RESET_USAGE``：服务端立即清零 + 客户端 ack 时再冗余清零（双写一致）。
  - ``DEDUCT_USAGE``：服务端在 ``deduct()`` 内已立即生效，客户端 ack 时**不再**
    二次写库（``command_service.ack`` 对 ``DEDUCT_USAGE`` 无特殊分支），避免双倍累加。

越界钳制：``applied = min(minutes, remaining)``，剩余下限 0；``applied=0`` 时
视为幂等 no-op（不写库、不下发指令），返回 ``clamped=True``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.core.constants import (
    DEDUCT_MINUTES_MAX,
    CommandType,
    DeviceStatus,
    EventType,
)
from app.core.errors import ConflictError, ErrorCode, NotFoundError, UnprocessableEntityError
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.device import Device
from app.models.user import User
from app.services import command_service, event_service, rule_service, usage_service


@dataclass(frozen=True, slots=True)
class DeductResult:
    """``deduct()`` 的结果。

    Attributes:
        device_id: 目标设备 id。
        target_date: 目标业务日期（设备时区今日，不信任前端日期）。
        requested_minutes: 请求扣减分钟数。
        applied_minutes: 实际扣减分钟数（越界时已被钳制）。
        clamped: 是否因越界被钳制（``applied < requested``）。
        command_id: 下发到客户端的 ``DEDUCT_USAGE`` 指令 id；``applied=0`` 时为 None。
        remaining_after: 扣减后的剩余分钟（``max(0, base+bonus-used)``）。
    """

    device_id: str
    target_date: date
    requested_minutes: int
    applied_minutes: int
    clamped: bool
    command_id: str | None
    remaining_after: int


@retry_on_busy
def deduct(
    db: Session,
    device_id: str,
    minutes: int,
    reason: str,
    user: User,
    *,
    now: datetime | None = None,
) -> DeductResult:
    """扣减设备当日用量并下发 ``DEDUCT_USAGE`` 指令（服务端立即生效）。

    Args:
        db: 数据库会话。
        device_id: 目标设备 id。
        minutes: 请求扣减分钟数（1-240；越界在 schema 层已收窄，此处双保险）。
        reason: 扣减事由（可选，写审计）。
        user: 操作管理员。
        now: 注入当前时间（G8）。

    Returns:
        :class:`DeductResult`。

    Raises:
        NotFoundError: 设备不存在。
        ConflictError: 设备已停用。
        UnprocessableEntityError: minutes 越界。
    """
    moment = now or utcnow()
    device = db.get(Device, device_id)
    if device is None:
        raise NotFoundError("设备不存在", code=ErrorCode.DEVICE_NOT_FOUND)
    if device.status != DeviceStatus.ACTIVE.value:
        raise ConflictError("设备已停用，无法扣减用量", code=ErrorCode.DEVICE_RETIRED)
    requested = int(minutes)
    if not (1 <= requested <= DEDUCT_MINUTES_MAX):
        raise UnprocessableEntityError(
            f"扣减分钟数需在 1-{DEDUCT_MINUTES_MAX} 之间",
            code=ErrorCode.VALIDATION_ERROR,
        )

    # 目标业务日期一律由**设备时区**决定（与 reset / grant 口径一致）。
    target = usage_service.device_today(device, moment)

    # 取/建当日用量行（对齐 grant_service.create，单一路径）。
    row = usage_service.ensure_daily_usage(db, device, target, now=moment)

    remaining = usage_service.compute_remaining(
        row.used_minutes, row.bonus_minutes, row.base_quota_minutes
    )
    applied = min(requested, remaining)
    clamped = applied < requested

    # 幂等 no-op：剩余已为 0（或请求非法为 0），不写库、不下发指令。
    if applied <= 0:
        return DeductResult(
            device_id=device.id,
            target_date=target,
            requested_minutes=requested,
            applied_minutes=0,
            clamped=True,
            command_id=None,
            remaining_after=remaining,
        )

    # 🔴 服务端立即生效（唯一真相源）：used 视为增加 applied → remaining 减 applied。
    row.used_minutes += applied
    row.deducted_minutes += applied
    row.updated_at = moment
    db.flush()

    # 下发 DEDUCT_USAGE 指令（ADMIN_ONLY 校验在 create_batch 内部；客户端仅冗余同步）。
    _, created, _failed = command_service.create_batch(
        db,
        [device.id],
        CommandType.DEDUCT_USAGE.value,
        {
            "target_date": target.isoformat(),
            "minutes": applied,
            "reason": reason or "",
        },
        user,
        now=moment,
    )
    command_id = created[0].id if created else None

    event_service.record(
        db,
        EventType.USAGE_DEDUCTED,
        device_id=device.id,
        user_id=user.id,
        actor_name=user.username,
        payload={
            "applied_minutes": applied,
            "requested_minutes": requested,
            "target_date": target.isoformat(),
            "reason": reason or "",
            "command_id": command_id,
        },
        now=moment,
    )

    remaining_after = usage_service.compute_remaining(
        row.used_minutes, row.bonus_minutes, row.base_quota_minutes
    )
    return DeductResult(
        device_id=device.id,
        target_date=target,
        requested_minutes=requested,
        applied_minutes=applied,
        clamped=clamped,
        command_id=command_id,
        remaining_after=remaining_after,
    )


__all__ = ["DeductResult", "deduct"]
