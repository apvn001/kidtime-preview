"""PC 客户端版本更新端点（MVP 方案 A）。

两个 router：

* ``client_router``（``/client``）：**匿名**可达（不带设备凭证），供未配对 / 已配对
  的客户端统一检查更新。限流由 ``RateLimitMiddleware`` 按路径精确匹配施加
  （规则见 ``app/core/config.py::rate_limit_rules``），本文件无需再挂限流依赖。
* ``admin_router``（``/admin/client``）：发布端点，依赖 ``require_admin``。

⚠️ 判定语义（与桌面端 ``update/checker.py`` 一致）：
  * ``update_required``  = ``current < min_version``（强制，不给稍后）
  * ``optional_available`` = ``latest.version > current`` 且 **非** 强制
  * 该通道无任何发布时：``latest=null``、两个布尔均 ``False``、``min_version=null``。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import AdminUser, DbSession
from app.core.constants import CHANNEL_STABLE
from app.core.errors import ErrorCode, NotFoundError
from app.schemas.client_version import (
    AdminVersionPublishIn,
    AdminVersionPublishOut,
    AssetOut,
    VersionCheckIn,
    VersionCheckOut,
    VersionInfoOut,
)
from app.services import client_version_service

client_router = APIRouter(prefix="/client", tags=["客户端"])
admin_router = APIRouter(prefix="/admin/client", tags=["管理端"])


@client_router.get(
    "/version/latest",
    response_model=VersionInfoOut,
    summary="查询指定通道最新版本",
)
def get_latest_version(
    db: DbSession,
    channel: str = Query(CHANNEL_STABLE, max_length=16, description="发布通道"),
) -> VersionInfoOut:
    """返回该通道最新发布版本；无发布时 404 ``VERSION_NOT_FOUND``。"""
    row = client_version_service.get_latest(db, channel)
    if row is None:
        raise NotFoundError(
            "该通道暂无已发布版本", code=ErrorCode.VERSION_NOT_FOUND
        )
    return VersionInfoOut.model_validate(row)


@client_router.post(
    "/version/check",
    response_model=VersionCheckOut,
    summary="检查是否需要更新",
)
def check_version(payload: VersionCheckIn, db: DbSession) -> VersionCheckOut:
    """依据 ``current_version`` 与通道 ``min_version`` / ``latest`` 计算更新判定。

    ``device_id`` 仅作记录用途，不做鉴权校验（匿名端点）。
    """
    latest = client_version_service.get_latest(db, payload.channel)
    if latest is None:
        # 该通道从未发布：既不强更也无可选更新。
        return VersionCheckOut(
            update_required=False,
            optional_available=False,
            min_version=None,
            latest=None,
        )

    update_required = (
        client_version_service.compare_semver_or_400(
            payload.current_version, latest.min_version
        )
        < 0
    )
    newer_available = (
        client_version_service.compare_semver_or_400(
            latest.version, payload.current_version
        )
        > 0
    )
    optional_available = newer_available and not update_required
    return VersionCheckOut(
        update_required=update_required,
        optional_available=optional_available,
        min_version=latest.min_version,
        latest=VersionInfoOut.model_validate(latest),
    )


@client_router.get(
    "/update/asset",
    response_model=AssetOut,
    summary="按版本号取下载物",
)
def get_asset(
    db: DbSession,
    version: str = Query(..., max_length=32, description="目标版本号"),
    channel: str = Query(CHANNEL_STABLE, max_length=16, description="发布通道"),
) -> AssetOut:
    """返回指定版本的下载直链与校验信息；不存在时 404 ``ASSET_NOT_FOUND``。"""
    row = client_version_service.get_by_version(db, channel, version)
    if row is None:
        raise NotFoundError(
            "请求的版本不存在", code=ErrorCode.ASSET_NOT_FOUND
        )
    return AssetOut(
        version=row.version,
        download_url=row.download_url,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
    )


@admin_router.post(
    "/version",
    response_model=AdminVersionPublishOut,
    status_code=status.HTTP_201_CREATED,
    summary="发布客户端版本（幂等 upsert）",
)
def publish_version(
    payload: AdminVersionPublishIn,
    db: DbSession,
    admin: AdminUser,
) -> AdminVersionPublishOut:
    """管理员发布一个版本；重复用同一 ``(channel, version)`` 调用是幂等的。"""
    row = client_version_service.publish(db, payload, admin)
    return AdminVersionPublishOut.model_validate(row)
