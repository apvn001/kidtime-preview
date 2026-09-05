"""设备管理端点（API.md §4）。"""

from __future__ import annotations

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import AdminUser, CurrentUser, DbSession, IdempotencyKeyDep
from app.schemas.command import CommandBatchOut, LockStyleIn
from app.schemas.device import (
    DeviceDeleteImpactOut,
    DeviceDeleteIn,
    DeviceDetailOut,
    DeviceOut,
    DeviceUnretireOut,
    DeviceUpdateIn,
    PairingCodeIn,
    PairingCodeOut,
    RevokeCredentialsIn,
    RevokeCredentialsOut,
)
from app.services import command_service, device_service, lock_style_service

router = APIRouter(prefix="/devices", tags=["设备管理"])


@router.post(
    "/pairing-codes",
    response_model=PairingCodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="生成配对码",
)
def create_pairing_code(
    payload: PairingCodeIn,
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
) -> PairingCodeOut:
    """生成配对码；携带 `device_id` 时为重新配对（D05）。"""
    record, display = device_service.create_pairing_code(
        db, admin, payload.device_id, payload.note
    )
    return PairingCodeOut(
        id=record.id,
        code=display,
        expires_at=record.expires_at,
        device_id=record.device_id,
        created_at=record.created_at,
    )


@router.get("", response_model=list[DeviceOut], summary="设备列表")
def list_devices(
    db: DbSession,
    _user: CurrentUser,
    status_filter: str | None = Query(
        None, alias="status", description="active | retired，缺省返回全部"
    ),
    keyword: str | None = Query(None, alias="q", description="按名称模糊搜索"),
) -> list[DeviceOut]:
    """返回设备列表（不分页，家庭场景设备数 <20）。"""
    devices = device_service.list_devices(db, status_filter, keyword)
    return device_service.build_device_out_list(db, devices)


@router.get("/{device_id}", response_model=DeviceDetailOut, summary="设备详情")
def get_device(
    db: DbSession,
    _user: CurrentUser,
    device_id: str = Path(..., description="设备 UUID"),
) -> DeviceDetailOut:
    """返回设备详情（含规则、近 7 天用量、凭证信息）。"""
    device = device_service.get_device_or_404(db, device_id)
    return device_service.build_device_detail(db, device)


@router.patch("/{device_id}", response_model=DeviceOut, summary="修改设备名称与时区")
def update_device(
    payload: DeviceUpdateIn,
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> DeviceOut:
    """修改设备展示名（D07）与时区（V5）。时区改动下次同步后生效。

    仅管理员可改（增量架构 §5.3 权限矩阵「编辑 | admin」）；非 admin 在前端
    连入口都不渲染，这里收紧后端防直接调 API。
    """
    device = device_service.update_device(
        db, device_id, name=payload.name, timezone_name=payload.timezone
    )
    return device_service.build_device_out(db, device)


@router.get(
    "/{device_id}/delete-impact",
    response_model=DeviceDeleteImpactOut,
    summary="删除影响预览",
)
def get_delete_impact(
    db: DbSession,
    _admin: AdminUser,
    device_id: str = Path(..., description="设备 UUID"),
) -> DeviceDeleteImpactOut:
    """返回删除该设备将连带清除的各子表真实条数，供 L3 弹窗展示。"""
    return device_service.get_delete_impact(db, device_id)


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="永久删除设备（不可恢复）",
)
def delete_device(
    payload: DeviceDeleteIn,
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> Response:
    """硬删除设备（V5 §5.1）。

    前置条件：设备必须已停用（`status=retired`），且 `confirm_name` 与设备名
    完全一致。删除后该设备的全部历史数据由数据库 CASCADE 清除，仅在
    `event_logs` 中保留一条 `DEVICE_DELETED` 留痕。
    """
    device_service.delete_device(db, device_id, admin, payload.confirm_name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{device_id}/unretire", response_model=DeviceUnretireOut, summary="恢复已停用设备"
)
def unretire_device(
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> DeviceUnretireOut:
    """恢复已停用设备（V5 §5.3）。

    ⚠️ 只恢复后台的管理可见性，**不恢复凭证**。`needs_repair=true` 时前端必须
    提示「需在客户端重新配对后才能恢复同步」。
    """
    device, needs_repair = device_service.unretire_device(db, device_id, admin)
    base = device_service.build_device_out(db, device)
    return DeviceUnretireOut(**base.model_dump(), needs_repair=needs_repair)


@router.post("/{device_id}/retire", response_model=DeviceOut, summary="停用设备")
def retire_device(
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> DeviceOut:
    """停用设备（D06）：撤销凭证、取消 pending 指令、历史数据只读保留。"""
    device = device_service.retire_device(db, device_id, admin)
    return device_service.build_device_out(db, device)


@router.post(
    "/{device_id}/repair",
    response_model=PairingCodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="重新配对，生成新配对码",
)
def repair_device(
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> PairingCodeOut:
    """重新配对（D05）：保留 `device_id` 与全部历史数据。"""
    record, display = device_service.create_pairing_code(db, admin, device_id)
    return PairingCodeOut(
        id=record.id,
        code=display,
        expires_at=record.expires_at,
        device_id=record.device_id,
        created_at=record.created_at,
    )


@router.post(
    "/{device_id}/revoke-credentials",
    response_model=RevokeCredentialsOut,
    summary="撤销设备全部有效凭证",
)
def revoke_credentials(
    payload: RevokeCredentialsIn,
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> RevokeCredentialsOut:
    """立即撤销该设备全部有效凭证，设备将无法同步直到重新配对。"""
    device, count, moment = device_service.revoke_credentials(
        db, device_id, admin, payload.reason
    )
    return RevokeCredentialsOut(
        device_id=device.id, revoked_count=count, revoked_at=moment
    )


@router.post(
    "/{device_id}/lock-style",
    response_model=CommandBatchOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="设置设备锁屏外观（admin-only）",
)
def set_device_lock_style(
    payload: LockStyleIn,
    db: DbSession,
    user: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> CommandBatchOut:
    """为指定设备下发 `SET_LOCK_STYLE` 指令（v1.4.2 锁屏外观三样式）。

    🔴 admin-only：须由 ``require_admin`` 端点下发；家长端 ``POST /commands`` 经
    ``ADMIN_ONLY_COMMAND_TYPES`` 越权兜底拒绝（与 ``RESET_USAGE`` 一致）。
    服务端仅创建待下发指令，客户端下次 ``/client/sync``（step10 ``deliver_pending``）
    拉取后本地应用外观，且不产生服务端即时状态变更（外观为纯客户端渲染态）。
    """
    batch_id, created, failed = lock_style_service.set_lock_style(
        db,
        device_id,
        style=payload.style,
        allow_child_switch=payload.allow_child_switch,
        user=user,
    )
    return CommandBatchOut(
        batch_id=batch_id,
        commands=[command_service.to_out(db, row) for row in created],
        failed=failed,
    )
