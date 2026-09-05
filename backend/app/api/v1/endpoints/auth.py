"""认证端点（API.md §2.3–§2.8）。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.deps import CurrentUser, DbSession, IdempotencyKeyDep
from app.core.errors import ErrorCode, ForbiddenError
from app.schemas.auth import (
    ChangePasswordIn,
    LoginIn,
    LogoutIn,
    MeOut,
    RefreshIn,
    TokenPair,
)
from app.services import auth_service, preference_service

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login", response_model=TokenPair, summary="登录（幂等豁免）")
def login(payload: LoginIn, db: DbSession, request: Request) -> TokenPair:
    """账号登录。**不需要** `Idempotency-Key`（S2 幂等豁免）。"""
    return auth_service.login(
        db,
        payload.username,
        payload.password,
        user_agent=request.headers.get("User-Agent"),
    )


@router.post("/refresh", response_model=TokenPair, summary="刷新令牌（幂等豁免）")
def refresh(payload: RefreshIn, db: DbSession, request: Request) -> TokenPair:
    """刷新令牌，轮换发放（D37）。检测到复用会撤销整条 family 链。"""
    return auth_service.refresh(
        db, payload.refresh_token, user_agent=request.headers.get("User-Agent")
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="登出",
)
def logout(
    payload: LogoutIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
) -> Response:
    """撤销指定 refresh token 所属 family，或该用户全部 family。"""
    auth_service.logout(db, user, payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="修改密码",
)
def change_password(
    payload: ChangePasswordIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
) -> Response:
    """修改密码，副作用为撤销该用户全部 refresh token。"""
    auth_service.change_password(db, user, payload.old_password, payload.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut, summary="获取当前用户")
def get_me(db: DbSession, user: CurrentUser) -> MeOut:
    """返回当前用户信息与个人偏好。"""
    pref = preference_service.get_or_create(db, user)
    return MeOut(
        id=user.id,
        username=user.username,
        role=user.role,  # type: ignore[arg-type]
        is_active=user.is_active,
        created_at=user.created_at,
        preferences=preference_service.to_out(pref),
    )


@router.post("/register", summary="公开注册（恒 403）")
def register() -> None:
    """公开注册已关闭（D35），恒返回 403。"""
    raise ForbiddenError(
        "公开注册已关闭，请联系管理员创建账号", code=ErrorCode.REGISTRATION_CLOSED
    )
