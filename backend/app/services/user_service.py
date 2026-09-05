"""用户服务：列表、创建、更新、删除（含最后管理员保护与操作人快照回填）。

V5 增量（增量架构设计 §5.2）：
    `delete_user` 承载 PRD 的 D1–D7。其中 D5「历史记录不得消失」的实现要点是
    **必须在 `DELETE FROM users` 之前**把 6 处 `*_name` 快照列填好 —— 一旦外键
    `ON DELETE SET NULL` 生效把 `created_by` 等列置空，用户名就永远查不回来了。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.constants import EventSeverity, EventType, UserRole
from app.core.errors import ConflictError, ErrorCode, NotFoundError
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.command import RemoteCommand
from app.models.device import PairingCode
from app.models.event import EventLog
from app.models.extension import ExtensionRequest
from app.models.rule import RuleProfile
from app.models.rule_template import RuleTemplate
from app.models.user import User
from app.schemas.user import UserCreateIn, UserUpdateIn
from app.services import event_service
from app.services.auth_service import revoke_all_user_tokens


def list_users(db: Session) -> list[User]:
    """返回全部账号（数量极少，不分页）。"""
    return list(db.execute(select(User).order_by(User.id)).scalars().all())


def count_active_admins(db: Session, exclude_user_id: int | None = None) -> int:
    """统计启用中的管理员数量。

    Args:
        db: 数据库会话。
        exclude_user_id: 排除的用户 id（用于判断"是否最后一个"）。
    """
    stmt = select(func.count()).select_from(User).where(
        User.role == UserRole.ADMIN.value, User.is_active.is_(True)
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return int(db.execute(stmt).scalar_one() or 0)


@retry_on_busy
def create_user(
    db: Session,
    payload: UserCreateIn,
    current_user: User | None = None,
    *,
    now: datetime | None = None,
) -> User:
    """创建账号（V5：补写 `USER_CREATED` 事件）。

    Args:
        db: 数据库会话。
        payload: 创建入参。
        current_user: 执行操作的管理员；为 ``None`` 时（如首次初始化）不写事件。
        now: 注入的当前时间（G8）。

    Returns:
        新建的账号。

    Raises:
        ConflictError: 用户名已被占用。
    """
    existing = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("用户名已被占用", code=ErrorCode.USERNAME_TAKEN)
    moment = now or utcnow()
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=True,
        created_at=moment,
        updated_at=moment,
    )
    db.add(user)
    db.flush()
    if current_user is not None:
        event_service.record(
            db,
            EventType.USER_CREATED,
            user_id=current_user.id,
            actor_name=current_user.username,
            payload={"user_id": user.id, "username": user.username, "role": user.role},
            now=moment,
        )
    return user


@retry_on_busy
def update_user(
    db: Session,
    user_id: int,
    payload: UserUpdateIn,
    current_user: User,
    *,
    now: datetime | None = None,
) -> User:
    """更新账号（角色 / 启用状态 / 重置密码），V5 补写 `USER_UPDATED` 事件。

    Raises:
        NotFoundError: 用户不存在。
        ConflictError: 触发最后管理员保护、试图停用自己或试图修改自己的角色。
    """
    target = db.get(User, user_id)
    if target is None:
        raise NotFoundError("用户不存在", code=ErrorCode.USER_NOT_FOUND)

    moment = now or utcnow()
    will_disable = payload.is_active is False
    will_demote = payload.role is not None and payload.role != UserRole.ADMIN.value

    if (
        target.role == UserRole.ADMIN.value
        and target.is_active
        and (will_disable or will_demote)
        and count_active_admins(db, exclude_user_id=target.id) == 0
    ):
        raise ConflictError(
            "不能停用或降级最后一个管理员", code=ErrorCode.LAST_ADMIN_PROTECTED
        )

    if will_disable and target.id == current_user.id:
        raise ConflictError("不能停用自己的账号", code=ErrorCode.CANNOT_DISABLE_SELF)

    # V5 §4.2：不能修改自己的角色（防管理员把自己降级后失去管理入口）
    if (
        payload.role is not None
        and target.id == current_user.id
        and payload.role != target.role
    ):
        raise ConflictError(
            "不能修改自己的角色", code=ErrorCode.CANNOT_DISABLE_SELF
        )

    changed: list[str] = []
    if payload.role is not None and payload.role != target.role:
        target.role = payload.role
        changed.append("role")
    if payload.is_active is not None:
        if payload.is_active != target.is_active:
            changed.append("is_active")
        target.is_active = payload.is_active
        if payload.is_active is False:
            revoke_all_user_tokens(db, target.id, now=moment)
    if payload.new_password is not None:
        target.password_hash = hash_password(payload.new_password)
        revoke_all_user_tokens(db, target.id, now=moment)
        changed.append("password")

    target.updated_at = moment
    db.flush()

    if changed:
        event_service.record(
            db,
            EventType.USER_UPDATED,
            user_id=current_user.id,
            actor_name=current_user.username,
            payload={
                "user_id": target.id,
                "username": target.username,
                "changed": changed,
            },
            now=moment,
        )
    return target


def _fill_operator_snapshots(db: Session, user_id: int, username: str) -> None:
    """删账号前回填 6 处操作人姓名快照（D5，增量架构设计 §5.2）。

    ⚠️ 必须在 ``DELETE FROM users`` **之前**调用：外键 `ON DELETE SET NULL`
    一旦生效，`created_by` / `decided_by` / `updated_by` 就被置空，
    再也无法反查用户名。

    Args:
        db: 数据库会话。
        user_id: 即将被删除的用户 id。
        username: 该用户当前用户名。
    """
    db.execute(
        update(EventLog)
        .where(EventLog.user_id == user_id, EventLog.actor_name.is_(None))
        .values(actor_name=username)
    )
    db.execute(
        update(RemoteCommand)
        .where(RemoteCommand.created_by == user_id)
        .values(created_by_name=username)
    )
    db.execute(
        update(ExtensionRequest)
        .where(ExtensionRequest.decided_by == user_id)
        .values(decided_by_name=username)
    )
    db.execute(
        update(RuleProfile)
        .where(RuleProfile.updated_by == user_id)
        .values(updated_by_name=username)
    )
    db.execute(
        update(RuleTemplate)
        .where(RuleTemplate.created_by == user_id)
        .values(created_by_name=username)
    )
    db.execute(
        update(RuleTemplate)
        .where(RuleTemplate.updated_by == user_id)
        .values(updated_by_name=username)
    )
    db.flush()


@retry_on_busy
def delete_user(
    db: Session, user_id: int, current_user: User, *, now: datetime | None = None
) -> None:
    """删除账号（V5 §5.2，落位 PRD D1–D7）。

    执行顺序**不可调换**：
        1. D1 不能删自己 / D2 不能删最后一个启用管理员；
        2. D5 回填 6 处 `*_name` 快照（必须早于 DELETE）；
        3. D6 写 `USER_DELETED` 事件（`user_id` 记的是**执行者**，不是被删者）；
        4. `DELETE FROM users`，由数据库完成 CASCADE（refresh_tokens →
           D4 会话立即失效、user_preferences、pairing_codes）
           与 SET NULL（remote_commands / extension_requests /
           rule_profiles / rule_templates / event_logs）。

    Args:
        db: 数据库会话。
        user_id: 待删除的用户 id。
        current_user: 执行操作的管理员。
        now: 注入的当前时间（G8）。

    Raises:
        NotFoundError: 用户不存在。
        ConflictError: 删除自己（D1）或删除最后一个启用管理员（D2）。
    """
    target = db.get(User, user_id)
    if target is None:
        raise NotFoundError("用户不存在", code=ErrorCode.USER_NOT_FOUND)
    if target.id == current_user.id:
        raise ConflictError("不能删除当前登录账号", code=ErrorCode.CANNOT_DELETE_SELF)
    if (
        target.role == UserRole.ADMIN.value
        and target.is_active
        and count_active_admins(db, exclude_user_id=target.id) == 0
    ):
        raise ConflictError(
            "不能删除最后一个启用的管理员", code=ErrorCode.LAST_ADMIN_PROTECTED
        )

    moment = now or utcnow()
    snapshot = {
        "user_id": target.id,
        "username": target.username,
        "role": target.role,
        "is_active": target.is_active,
        "deleted_pairing_codes": int(
            db.execute(
                select(func.count())
                .select_from(PairingCode)
                .where(PairingCode.created_by == target.id)
            ).scalar_one()
            or 0
        ),
    }

    _fill_operator_snapshots(db, target.id, target.username)

    event_service.record(
        db,
        EventType.USER_DELETED,
        severity=EventSeverity.WARNING,
        user_id=current_user.id,
        actor_name=current_user.username,
        payload=snapshot,
        now=moment,
    )
    db.flush()

    db.execute(sa_delete(User).where(User.id == target.id))
    db.expunge(target)
    db.flush()
