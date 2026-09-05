"""延时申请端点（API.md §8.1–§8.3）。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession, IdempotencyKeyDep, PageDep
from app.schemas.common import Page
from app.schemas.extension import ApproveIn, ExtensionRequestOut, RejectIn
from app.services import extension_service

router = APIRouter(prefix="/extension-requests", tags=["延时申请"])


@router.get("", response_model=Page[ExtensionRequestOut], summary="申请列表")
def list_extension_requests(
    db: DbSession,
    _user: CurrentUser,
    pagination: PageDep,
    device_id: str | None = Query(None, description="按设备过滤"),
    status_filter: str | None = Query(
        None, alias="status", description="逗号分隔多值，如 pending,approved"
    ),
    date_from: date | None = Query(None, description="target_date >="),
    date_to: date | None = Query(None, description="target_date <="),
) -> Page[ExtensionRequestOut]:
    """分页查询申请，`pending` 置顶。查询前惰性触发过期扫描。"""
    extension_service.expire_stale(db)
    items, total = extension_service.list_requests(
        db,
        device_id=device_id,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        page=pagination.page,
        size=pagination.size,
    )
    return Page.build(items, pagination.page, pagination.size, total)


@router.post(
    "/{request_id}/approve", response_model=ExtensionRequestOut, summary="批准申请"
)
def approve_request(
    payload: ApproveIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
    request_id: str = Path(..., description="申请 UUID"),
) -> ExtensionRequestOut:
    """批准申请，`approved_minutes` 可不同于 `requested_minutes`（D29）。"""
    row = extension_service.approve(db, request_id, payload.approved_minutes, user)
    return extension_service.to_out(db, row)


@router.post(
    "/{request_id}/reject", response_model=ExtensionRequestOut, summary="拒绝申请"
)
def reject_request(
    payload: RejectIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
    request_id: str = Path(..., description="申请 UUID"),
) -> ExtensionRequestOut:
    """拒绝申请，`approved_minutes` 置 0（D29）。"""
    row = extension_service.reject(db, request_id, payload.reason, user)
    return extension_service.to_out(db, row)
