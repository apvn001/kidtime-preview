"""用量端点（API.md §6 / V7 §2.3）。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AdminUser, CurrentUser, DbSession, IdempotencyKeyDep
from app.core.constants import DeviceStatus, RESET_SCOPES
from app.core.errors import ErrorCode, UnprocessableEntityError
from app.core.utils import parse_csv_filter
from app.db.base import utcnow
from app.schemas.usage import (
    DailyUsageOut,
    DeductUsageIn,
    DeductUsageOut,
    ResetUsageIn,
    ResetUsageOut,
    UsageSummaryOut,
)
from app.services import deduction_service, device_service, reset_service, usage_service

router = APIRouter(tags=["用量"])


@router.get(
    "/devices/{device_id}/usage",
    response_model=list[DailyUsageOut],
    summary="设备用量区间查询",
)
def get_device_usage(
    db: DbSession,
    _user: CurrentUser,
    device_id: str = Path(..., description="设备 UUID"),
    date_from: date | None = Query(None, alias="from", description="起始日期，默认 to-29 天"),
    date_to: date | None = Query(None, alias="to", description="结束日期，默认设备时区今日"),
) -> list[DailyUsageOut]:
    """按区间升序返回用量，缺失日期不补零（前端自行补齐）。"""
    device = device_service.get_device_or_404(db, device_id)
    default_from, default_to = usage_service.default_range(device)
    start = date_from or default_from
    end = date_to or default_to
    rows = usage_service.list_usage(db, device_id, start, end)
    return [usage_service.to_out(row) for row in rows]


@router.get("/usage/summary", response_model=UsageSummaryOut, summary="跨设备用量汇总")
def get_usage_summary(
    db: DbSession,
    _user: CurrentUser,
    target_date: date | None = Query(
        None, alias="date", description="指定日期，缺省为各设备本地今日"
    ),
    device_ids: str | None = Query(
        None, description="逗号分隔的设备 id，缺省为全部 active 设备"
    ),
) -> UsageSummaryOut:
    """跨设备用量汇总。"""
    if device_ids:
        wanted = parse_csv_filter(device_ids)
        devices = [device_service.get_device_or_404(db, item) for item in wanted]
    else:
        devices = device_service.list_devices(db, status=DeviceStatus.ACTIVE.value)
    return usage_service.summary(db, devices, target_date)


@router.post(
    "/usage/reset",
    response_model=ResetUsageOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="重置当日用量（ack 门控清零）",
)
def reset_usage(
    payload: ResetUsageIn,
    db: DbSession,
    user: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
) -> ResetUsageOut:
    """🔴 重置当日用量（服务端立即清零 + 客户端冗余通知）。

    - ``scope=device``：必须提供 ``device_id``，仅重置该设备目标日用量。
    - ``scope=global``：L3 高危，必须在 ``confirm_text`` 中**逐字输入** ``RESET``，
      否则 422 拒绝（前端逐字输入是第一道闸门，此处服务端二次校验防绕过）。

    服务端收到请求后立即清零 ``used/parent/break``（默认保留 ``bonus_minutes``），
    并下发 `RESET_USAGE` 指令；客户端下次同步时也会执行本地清零。旧客户端即便
    不识别的该指令，网页后台的用量已经生效。
    """
    if payload.scope not in RESET_SCOPES:
        raise UnprocessableEntityError(
            "不支持的重置范围", code=ErrorCode.USER_SCOPE_NOT_SUPPORTED
        )
    if payload.scope == "global" and payload.confirm_text != "RESET":
        raise UnprocessableEntityError(
            "全局重置为高危操作，必须在 confirm_text 中逐字输入 RESET",
            code=ErrorCode.RESET_CONFIRM_REQUIRED,
        )

    moment = utcnow()
    # 目标业务日期一律由**设备时区**决定，不信任前端传入的浏览器本地日期：
    # 客户端按设备时区算 effective_date，若此处用管理员浏览器时区（可能不同于
    # 设备时区）会导致 RESET_USAGE 落到错误的一天，表现为「重置不生效」。
    if payload.scope == "device":
        device = device_service.get_device_or_404(db, payload.device_id)
        resolved_target = usage_service.device_today(device, moment)
    else:
        # global：交由 request_reset 按各设备时区今日分别清零（传 None）。
        resolved_target = None

    # 记录「意图」审计 + 创建 RESET_USAGE 指令；请求层已用 require_admin 守卫。
    batch_id, created, failed, dispatched = reset_service.request_reset(
        db,
        payload.scope,
        device_id=payload.device_id,
        target_date=resolved_target,
        include_bonus=payload.include_bonus,
        user=user,
    )
    _ = (batch_id, failed, dispatched)  # 审计与失败信息已写入 request_reset 内部

    target_date = resolved_target or moment.date()
    return ResetUsageOut(
        scope=payload.scope,
        target_date=target_date,
        total_devices=len(created),
        command_ids=[c.id for c in created],
        status="ok",
        message=(
            "已重置该设备当日用量"
            if payload.scope == "device"
            else f"已全局重置 {len(created)} 台设备当日用量"
        ),
    )


@router.post(
    "/usage/deduct",
    response_model=DeductUsageOut,
    status_code=status.HTTP_200_OK,
    summary="扣减当日用量（服务端立即生效 + 客户端指令同步）",
)
def deduct_usage(
    payload: DeductUsageIn,
    db: DbSession,
    user: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
) -> DeductUsageOut:
    """🔴 扣减设备当日用量（V7 功能三）。

    对指定设备当天已用/剩余用量做主动扣减：服务端**立即**把
    ``daily_usage.used_minutes += applied``（网页后台实时可见剩余下降），并下发
    ``DEDUCT_USAGE`` 指令让客户端本地 ``used_seconds += applied*60`` 同步扣减。

    - 越界（``minutes > remaining``）：钳制为 ``applied = remaining``，剩余下限 0。
    - ``applied = 0``（剩余已为 0）：幂等 no-op，不下发指令。
    - 幂等：同一 ``Idempotency-Key`` 重复请求返回首次结果，``used_minutes`` 不重复累加。

    需管理员令牌（``require_admin``）；目标业务日期由**设备时区**决定，不信任前端日期。
    """
    result = deduction_service.deduct(
        db,
        payload.device_id,
        payload.minutes,
        payload.reason,
        user,
    )
    if result.applied_minutes <= 0:
        message = "该设备当日剩余用量已为 0，未产生扣减"
    elif result.clamped:
        message = f"已按剩余用量钳制扣减 {result.applied_minutes} 分钟"
    else:
        message = f"已扣减 {result.applied_minutes} 分钟"
    return DeductUsageOut(
        device_id=result.device_id,
        target_date=result.target_date,
        requested_minutes=result.requested_minutes,
        applied_minutes=result.applied_minutes,
        clamped=result.clamped,
        command_id=result.command_id,
        remaining_minutes=result.remaining_after,
        status="ok",
        message=message,
    )
