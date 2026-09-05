"""FastAPI 依赖注入（ARCHITECTURE.md §5.2）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Iterator

from fastapi import Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import (
    BadRequestError,
    ErrorCode,
    ForbiddenError,
    UnauthorizedError,
)
from app.core.security import decode_access_token, is_uuid
from app.db.session import SessionLocal
from app.models.device import Device
from app.models.user import User


def get_db() -> Iterator[Session]:
    """每请求一个独立 Session；正常结束提交、异常回滚（D51）。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """解析 `Authorization: Bearer <token>` 并返回启用中的用户。

    Raises:
        UnauthorizedError: 缺头、格式错、签名/过期无效或用户不存在。
        ForbiddenError: 账号已停用。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError()
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_access_token(token)
    try:
        user_id = int(payload.sub)
    except (TypeError, ValueError) as exc:
        raise UnauthorizedError() from exc
    user = db.get(User, user_id)
    if user is None:
        raise UnauthorizedError()
    if not user.is_active:
        raise ForbiddenError("账号已停用", code=ErrorCode.USER_DISABLED)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    """要求当前用户为管理员（D36）。

    Raises:
        ForbiddenError: 角色不是 ``admin``。
    """
    if user.role != "admin":
        raise ForbiddenError("该操作需要管理员权限", code=ErrorCode.ADMIN_REQUIRED)
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def get_current_device(
    db: DbSession,
    x_device_id: Annotated[str, Header(alias="X-Device-Id")],
    x_device_secret: Annotated[str, Header(alias="X-Device-Secret")],
) -> Device:
    """设备鉴权：`X-Device-Id` + `X-Device-Secret`（D42）。

    Raises:
        UnauthorizedError: 设备不存在或凭证无效/已撤销。
        ForbiddenError: 设备已停用。
    """
    # 延迟导入避免 deps <-> services 的循环引用
    from app.services.device_service import authenticate_device

    return authenticate_device(db, x_device_id, x_device_secret)


CurrentDevice = Annotated[Device, Depends(get_current_device)]


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """校验 `Idempotency-Key` 的存在性与格式。

    真正的去重在 `IdempotencyMiddleware` 完成，本依赖只做前置校验与 OpenAPI 文档化。

    Raises:
        BadRequestError: 缺失或非 UUID。
    """
    if not idempotency_key:
        raise BadRequestError(
            "缺少 Idempotency-Key 请求头", code=ErrorCode.IDEMPOTENCY_KEY_REQUIRED
        )
    if not is_uuid(idempotency_key):
        raise BadRequestError(
            "Idempotency-Key 必须是 UUID", code=ErrorCode.IDEMPOTENCY_KEY_INVALID
        )
    return idempotency_key


IdempotencyKeyDep = Annotated[str, Depends(get_idempotency_key)]


@dataclass(frozen=True)
class Pagination:
    """分页参数。"""

    page: int
    size: int

    @property
    def offset(self) -> int:
        """SQL OFFSET 值。"""
        return (self.page - 1) * self.size


def get_pagination(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    size: int = Query(20, ge=1, le=200, description="每页条数，1-200"),
) -> Pagination:
    """解析分页参数（API.md §1.6）。"""
    return Pagination(page=page, size=size)


PageDep = Annotated[Pagination, Depends(get_pagination)]


def get_pagination_large(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    size: int = Query(50, ge=1, le=200, description="每页条数，1-200"),
) -> Pagination:
    """事件列表默认 size=50（API.md §8.6）。"""
    return Pagination(page=page, size=size)


PageDepLarge = Annotated[Pagination, Depends(get_pagination_large)]
