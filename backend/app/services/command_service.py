"""远程指令服务：批量创建（逐设备 command_id）、下发、回执、过期扫描（D18/D48/D49）。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    ADMIN_ONLY_COMMAND_TYPES,
    CommandStatus,
    CommandType,
    DeviceStatus,
    EventType,
    LOCK_STYLE_VALUES,
    UserRole,
)
from app.core.errors import ErrorCode, ForbiddenError, NotFoundError
from app.core.utils import parse_csv_filter
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.command import RemoteCommand
from app.models.device import Device
from app.models.user import User
from app.core.security import new_uuid
from app.schemas.client_sync import CommandDeliverItem
from app.schemas.command import (
    CommandAckItem,
    CommandCreateFailure,
    CommandCreateIn,
    CommandOut,
)
from app.services import event_service, rule_service, usage_service


@retry_on_busy
def create_batch(
    db: Session,
    device_ids: list[str],
    command_type: str,
    payload: dict[str, Any] | None,
    user: User,
    *,
    now: datetime | None = None,
) -> tuple[str, list[RemoteCommand], list[CommandCreateFailure]]:
    """批量创建指令，🔴 逐设备生成独立 `command_id`，共享 `batch_id`（D48/A18）。

    `command_type` 直接传字符串（不经由 `CommandCreateIn` 的 schema 收窄），
    以便管理专属端点（如 `POST /usage/reset`）下发 `RESET_USAGE`。

    `PAUSE_ENFORCEMENT` / `RESUME_ENFORCEMENT` 在**同一事务内**改写
    `rule_profiles.enforcement_enabled` 并 `version += 1`（D18）。

    Returns:
        ``(batch_id, 已创建指令列表, 失败设备列表)``。

    Raises:
        NotFoundError: 全部 device_id 都不存在。
        ForbiddenError: `command_type` 属于 admin-only 而 `user` 非管理员（R2 兜底）。
    """
    settings = get_settings()
    moment = now or utcnow()
    # R2 越权面兜底：管理专属指令类型只允许管理员经内部服务层下发；
    # 公开端点 `POST /commands`（CurrentUser）schema 已收窄，无法传入即 422。
    if command_type in ADMIN_ONLY_COMMAND_TYPES and user.role != UserRole.ADMIN.value:
        raise ForbiddenError(
            "该指令类型仅管理员可下发", code=ErrorCode.ADMIN_REQUIRED
        )
    batch_id = new_uuid()
    created: list[RemoteCommand] = []
    failed: list[CommandCreateFailure] = []
    found_any = False

    for device_id in device_ids:
        device = db.get(Device, device_id)
        if device is None:
            failed.append(
                CommandCreateFailure(
                    device_id=device_id,
                    code=ErrorCode.DEVICE_NOT_FOUND,
                    message="设备不存在",
                )
            )
            continue
        found_any = True
        if device.status != DeviceStatus.ACTIVE.value:
            failed.append(
                CommandCreateFailure(
                    device_id=device_id,
                    code=ErrorCode.DEVICE_RETIRED,
                    message="设备已停用",
                )
            )
            continue

        command = RemoteCommand(
            id=new_uuid(),
            device_id=device.id,
            type=command_type,
            payload_json=json.dumps(build_payload(command_type, payload), ensure_ascii=False),
            status=CommandStatus.PENDING.value,
            batch_id=batch_id,
            created_by=user.id,
            created_by_name=user.username,
            created_at=moment,
            expires_at=moment + timedelta(minutes=settings.COMMAND_EXPIRE_MINUTES),
        )
        db.add(command)
        db.flush()
        created.append(command)

        if command_type == CommandType.PAUSE_ENFORCEMENT.value:
            rule_service.set_enforcement(db, device.id, False, user, now=moment)
        elif command_type == CommandType.RESUME_ENFORCEMENT.value:
            rule_service.set_enforcement(db, device.id, True, user, now=moment)

    if not found_any:
        raise NotFoundError("设备不存在", code=ErrorCode.DEVICE_NOT_FOUND)
    return batch_id, created, failed


def expire_stale(
    db: Session, device_id: str | None = None, *, now: datetime | None = None
) -> int:
    """把 `pending AND expires_at <= now` 的指令置为 `expired`（D49）。"""
    moment = now or utcnow()
    stmt = update(RemoteCommand).where(
        RemoteCommand.status == CommandStatus.PENDING.value,
        RemoteCommand.expires_at <= moment,
    )
    if device_id is not None:
        stmt = stmt.where(RemoteCommand.device_id == device_id)
    result = db.execute(stmt.values(status=CommandStatus.EXPIRED.value))
    return int(result.rowcount or 0)


def deliver_pending(
    db: Session, device_id: str, *, now: datetime | None = None
) -> list[CommandDeliverItem]:
    """同步第 10 步：过期清理后取出待执行指令并置为 `delivered`。"""
    moment = now or utcnow()
    expire_stale(db, device_id, now=moment)
    rows = (
        db.execute(
            select(RemoteCommand)
            .where(
                RemoteCommand.device_id == device_id,
                RemoteCommand.status == CommandStatus.PENDING.value,
                RemoteCommand.expires_at > moment,
            )
            .order_by(RemoteCommand.created_at.asc())
        )
        .scalars()
        .all()
    )
    items: list[CommandDeliverItem] = []
    for row in rows:
        row.status = CommandStatus.DELIVERED.value
        row.delivered_at = moment
        items.append(
            CommandDeliverItem(
                command_id=row.id,
                type=row.type,  # type: ignore[arg-type]
                payload=row.payload,
                created_at=row.created_at,
                expires_at=row.expires_at,
            )
        )
    if items:
        db.flush()
    return items


def ack(
    db: Session,
    device_id: str,
    acks: Sequence[CommandAckItem],
    *,
    now: datetime | None = None,
) -> tuple[list[str], list[str]]:
    """同步第 7 步：处理指令回执，仅 `pending`/`delivered` 可 ack。

    Returns:
        ``(acked_ids, ignored_ids)``。
    """
    moment = now or utcnow()
    acked: list[str] = []
    ignored: list[str] = []
    for item in acks:
        row = db.get(RemoteCommand, item.command_id)
        if row is None or row.device_id != device_id:
            ignored.append(item.command_id)
            continue
        if row.status not in (CommandStatus.PENDING.value, CommandStatus.DELIVERED.value):
            ignored.append(item.command_id)
            continue
        row.status = CommandStatus.ACKED.value
        row.acked_at = moment
        row.result_json = json.dumps(
            {
                "status": item.status,
                "error": item.error,
                "executed_at": item.executed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            ensure_ascii=False,
        )
        # 🔴 ack 门控清零（方案 A）：仅 RESET_USAGE 且执行成功时才清零当日已用量。
        # 失败态（旧客户端不识别指令）保留原值，前端据此提示升级客户端。
        if row.type == CommandType.RESET_USAGE.value and item.status == "success":
            _apply_reset_on_ack(db, device_id, row, moment)
        acked.append(item.command_id)
        event_service.record(
            db,
            EventType.COMMAND_EXECUTED,
            device_id=device_id,
            payload={
                "command_id": row.id,
                "type": row.type,
                "status": item.status,
                "error": item.error,
            },
            occurred_at=item.executed_at,
            now=moment,
        )
    if acked:
        db.flush()
    return acked, ignored


def _apply_reset_on_ack(
    db: Session, device_id: str, row: RemoteCommand, moment: datetime
) -> None:
    """`RESET_USAGE` 指令被客户端成功 ack 后，清零该设备当日已用量（方案 A）。

    仅当 payload 含合法 `target_date` 才执行；清零 `used/parent/break`，
    默认保留 `bonus`（除非 `include_bonus=True`）。随后写 `USAGE_RESET` 审计事件。
    """
    payload = row.payload or {}
    target_raw = payload.get("target_date")
    if not target_raw:
        return
    try:
        target_date = date.fromisoformat(str(target_raw))
    except (TypeError, ValueError):
        return
    include_bonus = bool(payload.get("include_bonus", False))
    usage_service.reset_daily(
        db, [device_id], target_date, include_bonus=include_bonus, now=moment
    )
    event_service.record(
        db,
        EventType.USAGE_RESET,
        device_id=device_id,
        user_id=row.created_by,
        actor_name=row.created_by_name,
        payload={
            "command_id": row.id,
            "target_date": target_date.isoformat(),
            "include_bonus": include_bonus,
            "scope": "device",
        },
        occurred_at=moment,
        now=moment,
    )


def list_commands(
    db: Session,
    *,
    device_id: str | None = None,
    status_filter: str | None = None,
    type_filter: str | None = None,
    batch_id: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[CommandOut], int]:
    """分页查询指令，按 `created_at DESC`。"""
    stmt = select(RemoteCommand)
    if device_id:
        stmt = stmt.where(RemoteCommand.device_id == device_id)
    if status_filter:
        statuses = parse_csv_filter(status_filter)
        if statuses:
            stmt = stmt.where(RemoteCommand.status.in_(statuses))
    if type_filter:
        types = parse_csv_filter(type_filter)
        if types:
            stmt = stmt.where(RemoteCommand.type.in_(types))
    if batch_id:
        stmt = stmt.where(RemoteCommand.batch_id == batch_id)

    total = int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one() or 0
    )
    rows = (
        db.execute(
            stmt.order_by(RemoteCommand.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return [to_out(db, row) for row in rows], total


def to_out(db: Session, row: RemoteCommand) -> CommandOut:
    """把 ORM 指令转成出参（补齐 device_name / created_by_username / 快照名）。

    ⚠️ 账号被删后 `created_by` 为 NULL（V5 SET NULL），此时 `db.get(User, None)`
    会触发 SAWarning；前端按 `formatOperator(created_by, created_by_name)`
    渲染「已删除账号（原 X）」。
    """
    device = db.get(Device, row.device_id)
    created_by_username: str | None = None
    if row.created_by:
        user = db.get(User, row.created_by)
        created_by_username = user.username if user is not None else None
    return CommandOut(
        command_id=row.id,
        device_id=row.device_id,
        device_name=device.name if device is not None else "",
        type=row.type,  # type: ignore[arg-type]
        payload=row.payload,
        status=row.status,  # type: ignore[arg-type]
        result=row.result,
        batch_id=row.batch_id,
        created_by=row.created_by,
        created_by_username=created_by_username,
        created_by_name=row.created_by_name,
        created_at=row.created_at,
        delivered_at=row.delivered_at,
        acked_at=row.acked_at,
        expires_at=row.expires_at,
    )


def count_outstanding(db: Session) -> int:
    """统计 `pending`/`delivered` 指令数（仪表盘 KPI）。"""
    stmt = (
        select(func.count())
        .select_from(RemoteCommand)
        .where(
            RemoteCommand.status.in_(
                [CommandStatus.PENDING.value, CommandStatus.DELIVERED.value]
            )
        )
    )
    return int(db.execute(stmt).scalar_one() or 0)


def build_payload(command_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """规范化指令参数，仅保留该类型关心的字段。

    - ``UNLOCK_TEMP``：保留 ``{minutes}``
    - ``RESET_USAGE``：保留 ``{target_date, include_bonus}``
    - ``DEDUCT_USAGE``：保留 ``{target_date, minutes, reason}``
    - 其余：空对象
    """
    if command_type == CommandType.UNLOCK_TEMP.value:
        return payload or {}
    if command_type == CommandType.RESET_USAGE.value:
        p = payload or {}
        target = p.get("target_date")
        if not target:
            return {}
        return {
            "target_date": target,
            "include_bonus": bool(p.get("include_bonus", False)),
        }
    if command_type == CommandType.SET_LOCK_STYLE.value:
        p = payload or {}
        style = p.get("style", "default")
        if style not in LOCK_STYLE_VALUES:
            style = "default"
        return {
            "style": style,
            "allow_child_switch": bool(p.get("allow_child_switch", False)),
        }
    if command_type == CommandType.DEDUCT_USAGE.value:
        p = payload or {}
        target = p.get("target_date")
        if not target:
            return {}
        return {
            "target_date": target,
            "minutes": int(p.get("minutes") or 0),
            "reason": str(p.get("reason") or ""),
        }
    return {}
