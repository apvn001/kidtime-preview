"""远程指令端点（API.md §8.4 / §8.5）。"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession, IdempotencyKeyDep, PageDep
from app.schemas.command import CommandBatchOut, CommandCreateIn, CommandOut
from app.schemas.common import Page
from app.services import command_service

router = APIRouter(prefix="/commands", tags=["远程指令"])


@router.post(
    "",
    response_model=CommandBatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="批量下发远程指令",
)
def create_commands(
    payload: CommandCreateIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
) -> CommandBatchOut:
    """🔴 逐设备生成独立 `command_id`，共享 `batch_id`（D48/A18）。"""
    batch_id, created, failed = command_service.create_batch(
        db, payload.device_ids, payload.type, payload.payload, user
    )
    return CommandBatchOut(
        batch_id=batch_id,
        commands=[command_service.to_out(db, row) for row in created],
        failed=failed,
    )


@router.get("", response_model=Page[CommandOut], summary="指令列表")
def list_commands(
    db: DbSession,
    _user: CurrentUser,
    pagination: PageDep,
    device_id: str | None = Query(None, description="按设备过滤"),
    status_filter: str | None = Query(None, alias="status", description="逗号分隔多值"),
    type_filter: str | None = Query(None, alias="type", description="按指令类型过滤"),
    batch_id: str | None = Query(None, description="按批次过滤"),
) -> Page[CommandOut]:
    """分页查询指令，按 `created_at DESC`。查询前惰性触发过期扫描。"""
    command_service.expire_stale(db)
    items, total = command_service.list_commands(
        db,
        device_id=device_id,
        status_filter=status_filter,
        type_filter=type_filter,
        batch_id=batch_id,
        page=pagination.page,
        size=pagination.size,
    )
    return Page.build(items, pagination.page, pagination.size, total)
