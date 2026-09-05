"""用户管理端点（API.md §3，admin 鉴权）。

V5 增量（增量架构设计 §4.1 #4、§4.2）：
    - 新增 `DELETE /users/{user_id}`（幂等键必需，204），落位 PRD D1–D7；
    - `POST` / `PATCH` 把当前管理员透传给 service 层，用于补写
      `USER_CREATED` / `USER_UPDATED` 事件与「不能修改自己的角色」校验。
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Response, status

from app.api.deps import AdminUser, DbSession, IdempotencyKeyDep
from app.schemas.user import UserCreateIn, UserOut, UserUpdateIn
from app.services import user_service

router = APIRouter(prefix="/users", tags=["用户管理"])


@router.get("", response_model=list[UserOut], summary="账号列表")
def list_users(db: DbSession, _admin: AdminUser) -> list[UserOut]:
    """返回全部账号（不分页）。"""
    return [UserOut.model_validate(user) for user in user_service.list_users(db)]


@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建账号",
)
def create_user(
    payload: UserCreateIn,
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
) -> UserOut:
    """创建新账号（V5：补写 `USER_CREATED` 事件）。"""
    return UserOut.model_validate(user_service.create_user(db, payload, admin))


@router.patch("/{user_id}", response_model=UserOut, summary="更新账号")
def update_user(
    payload: UserUpdateIn,
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    user_id: int = Path(..., description="目标用户 id"),
) -> UserOut:
    """更新角色 / 启用状态 / 重置密码（V5：补写 `USER_UPDATED` 事件）。"""
    return UserOut.model_validate(user_service.update_user(db, user_id, payload, admin))


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除账号",
)
def delete_user(
    db: DbSession,
    admin: AdminUser,
    _idempotency_key: IdempotencyKeyDep,
    user_id: int = Path(..., description="目标用户 id"),
) -> Response:
    """硬删除账号（V5 §4.1 #4）。

    服务端保证：D1 不能删自己、D2 不能删最后一个启用管理员、
    D5 先回填 6 处操作人姓名快照、D6 写 `USER_DELETED` 事件，
    随后由数据库 CASCADE / SET NULL 完成关联清理（D3、D4、D7）。
    """
    user_service.delete_user(db, user_id, admin)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
