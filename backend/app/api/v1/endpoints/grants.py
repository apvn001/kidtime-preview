"""下发时长端点（V7 §3.3 / §5 R4）。

- `POST /grants`：管理员向某设备下发「当日额外配额」（time_grants），复用既有
  credits 下行链路与 `/client/confirm-credit` 入账路径，客户端零改动。
- `GET /grants`：查询下发记录（管理员 / 家长可读，用于详情页「今日下发记录」）。
- `POST /grants/{id}/revoke`：管理员撤回一条 granted 记录（R6 幂等）。
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Query, status

from app.api.deps import (
    AdminUser,
    CurrentUser,
    DbSession,
    IdempotencyKeyDep,
    PageDep,
)
from app.schemas.common import Page
from app.schemas.grant import GrantCreateIn, TimeGrantOut
from app.services import grant_service

router = APIRouter(prefix="/grants", tags=["下发时长"])


@router.post(
    "",
    response_model=TimeGrantOut,
    status_code=status.HTTP_201_CREATED,
    summary="下发当日额外时长",
)
def create_grant(
    payload: GrantCreateIn,
    db: DbSession,
    user: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
) -> TimeGrantOut:
    """🔴 管理员向设备下发当日额外配额（time_grants）。

    写入即生成 UUID（经 sync 第 11 步并入 credits 下行数组）；同时写
    `TIME_GRANTED` 审计。受 R4 频控：单设备单日下发次数与总额上限保护。

    ⚠️ 目标业务日期一律由**设备时区**决定（``device_today``），不信任前端
    传入的浏览器本地日期：客户端按设备时区算 ``effective_date``，若此处用
    管理员浏览器时区（可能不同于设备时区）会触发客户端 ``DATE_MISMATCH``
    静默丢弃，表现为「下发不生效」。
    """
    grant = grant_service.create(
        db,
        payload.device_id,
        payload.minutes,
        payload.reason,
        user,
        target_date=None,  # 强制回退到 device_today(device)，与客户端一致
    )
    return grant_service.to_out(db, grant)


@router.get("", response_model=Page[TimeGrantOut], summary="查询下发记录")
def list_grants(
    db: DbSession,
    _user: CurrentUser,
    pagination: PageDep,
    device_id: str | None = Query(None, description="按设备过滤"),
    status_filter: str | None = Query(None, alias="status", description="逗号分隔多值"),
) -> Page[TimeGrantOut]:
    """分页查询 time_grants，按 `granted_at DESC`。"""
    rows, total = grant_service.list_grants(
        db,
        device_id=device_id,
        status=status_filter,
        page=pagination.page,
        size=pagination.size,
    )
    return Page.build(
        [grant_service.to_out(db, row) for row in rows],
        pagination.page,
        pagination.size,
        total,
    )


@router.post(
    "/{grant_id}/revoke",
    response_model=TimeGrantOut,
    summary="撤回下发记录",
)
def revoke_grant(
    db: DbSession,
    user: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    grant_id: str = Path(..., description="下发记录 UUID"),
) -> TimeGrantOut:
    """🔴 撤回一条 granted 的下发记录（R6 幂等：非 granted 状态原样返回，等同 200）。

    撤回后该记录不再进入 sync 第 11 步的下行 union，客户端不会收到该额度。
    """
    grant = grant_service.revoke(db, grant_id, user)
    return grant_service.to_out(db, grant)
