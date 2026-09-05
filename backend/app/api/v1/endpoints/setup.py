"""初始化端点（API.md §2.1 / §2.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app import __version__
from app.api.deps import DbSession, IdempotencyKeyDep
from app.db.base import utcnow
from app.schemas.auth import SetupInitIn, SetupStatusOut, TokenPair
from app.services import auth_service

router = APIRouter(tags=["初始化"])


@router.get("/setup/status", response_model=SetupStatusOut, summary="获取初始化状态")
def get_setup_status(db: DbSession) -> SetupStatusOut:
    """返回系统是否已完成首次初始化。无鉴权。"""
    return SetupStatusOut(
        initialized=auth_service.is_initialized(db),
        server_time=utcnow(),
        version=__version__,
    )


@router.post(
    "/setup/init",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    summary="首次初始化，创建 admin 账号",
)
def init_setup(
    payload: SetupInitIn,
    db: DbSession,
    request: Request,
    _idempotency_key: IdempotencyKeyDep,
) -> TokenPair:
    """创建首个管理员账号并直接返回令牌对（A01）。"""
    return auth_service.init_admin(
        db,
        payload.username,
        payload.password,
        user_agent=request.headers.get("User-Agent"),
    )
