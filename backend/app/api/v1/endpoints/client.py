"""🔴 客户端专用端点（API.md §7）：pair / sync / confirm-credit / command-ack。"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentDevice, DbSession, IdempotencyKeyDep
from app.db.base import utcnow
from app.schemas.client_sync import SyncRequest, SyncResponse
from app.schemas.command import CommandAckIn, CommandAckOut
from app.schemas.device import ClientPairIn, ClientPairOut
from app.schemas.extension import ConfirmCreditIn, ConfirmCreditOut
from app.services import command_service, device_service, rule_service, sync_service

router = APIRouter(prefix="/client", tags=["客户端"])


@router.post(
    "/pair",
    response_model=ClientPairOut,
    status_code=status.HTTP_201_CREATED,
    summary="设备配对",
)
def pair(
    payload: ClientPairIn,
    db: DbSession,
    _idempotency_key: IdempotencyKeyDep,
) -> ClientPairOut:
    """凭配对码完成设备配对，返回 `device_secret` 明文（仅此一次）与初始规则快照。"""
    device, secret, rule = device_service.pair_device(db, payload)
    return ClientPairOut(
        device_id=device.id,
        device_secret=secret,
        device_name=device.name,
        timezone=device.timezone,
        rules=rule_service.to_out(rule),
        server_time=utcnow(),
        paired_at=device.paired_at or utcnow(),
    )


@router.post("/sync", response_model=SyncResponse, summary="🔴 一次往返完成上下行同步")
def sync(
    payload: SyncRequest,
    db: DbSession,
    device: CurrentDevice,
    _idempotency_key: IdempotencyKeyDep,
) -> SyncResponse:
    """心跳 + 用量 + 事件 + 申请 + 回执 + 额度确认 → 规则 + 指令 + 额度下发。"""
    return sync_service.sync(db, device, payload)


@router.post(
    "/confirm-credit", response_model=ConfirmCreditOut, summary="额度入账确认"
)
def confirm_credit(
    payload: ConfirmCreditIn,
    db: DbSession,
    device: CurrentDevice,
    _idempotency_key: IdempotencyKeyDep,
) -> ConfirmCreditOut:
    """入账后立即确认，缩短重复发放窗口。重复确认返回 200 幂等成功（D31/A11）。

    V7：经 ``credit_service.confirm`` 路由到 ``ExtensionRequest`` 与 ``TimeGrant`` 双表。
    """
    from app.services import credit_service

    confirmed, already, missing = credit_service.confirm(
        db, device.id, payload.request_ids
    )
    return ConfirmCreditOut(
        confirmed=confirmed, already_credited=already, not_found=missing
    )


@router.post("/command-ack", response_model=CommandAckOut, summary="指令执行回执")
def command_ack(
    payload: CommandAckIn,
    db: DbSession,
    device: CurrentDevice,
    _idempotency_key: IdempotencyKeyDep,
) -> CommandAckOut:
    """执行后立即回执，正常路径也可走 `/client/sync` 的 `command_acks`。"""
    acked, ignored = command_service.ack(db, device.id, payload.acks)
    return CommandAckOut(acked=acked, ignored=ignored)
