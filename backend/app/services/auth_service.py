"""认证服务：首启初始化、登录、refresh 轮换与复用检测、登出、改密（D35/D37/D38）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.constants import EventSeverity, EventType, UserRole
from app.core.errors import (
    ErrorCode,
    ForbiddenError,
    UnauthorizedError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    new_family_id,
    sha256_hex,
    validate_password_policy,
    verify_password,
)
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.user import RefreshToken, User
from app.schemas.auth import TokenPair
from app.schemas.user import UserOut
from app.services import event_service


def is_initialized(db: Session) -> bool:
    """系统是否已完成首次初始化（存在任一 admin 账号）。"""
    stmt = select(func.count()).select_from(User).where(User.role == UserRole.ADMIN.value)
    return int(db.execute(stmt).scalar_one() or 0) > 0


def _user_out(user: User) -> UserOut:
    """把 ORM 用户转成出参。"""
    return UserOut.model_validate(user)


def _issue_token_pair(
    db: Session,
    user: User,
    *,
    family_id: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> TokenPair:
    """签发一组新的 access + refresh 令牌。

    Args:
        db: 数据库会话。
        user: 目标用户。
        family_id: 复用已有轮换链；``None`` 时新建。
        user_agent: 记录发起端 UA。
        now: 注入的当前时间（G8）。
    """
    moment = now or utcnow()
    access_token, expires_in = create_access_token(user.id, user.role)
    raw_refresh, token_hash, expires_at = create_refresh_token(now=moment)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            family_id=family_id or new_family_id(),
            issued_at=moment,
            expires_at=expires_at,
            user_agent=(user_agent or None),
        )
    )
    db.flush()
    return TokenPair(
        access_token=access_token,
        refresh_token=raw_refresh,
        token_type="bearer",
        expires_in=expires_in,
        user=_user_out(user),
    )


@retry_on_busy
def init_admin(
    db: Session,
    username: str,
    password: str,
    *,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> TokenPair:
    """首次初始化：创建 admin 账号并直接返回令牌对（A01）。

    Raises:
        ForbiddenError: 系统已完成初始化（P0-6 收口，见下方注释）。
    """
    if is_initialized(db):
        # 🔴 P0-6 / F1 前提 A3：初始化完成后该端点必须**彻底关闭**。
        # 原实现返回 409（"状态冲突"，语义上暗示"换个参数也许能成"），
        # 现统一为 403（"这个入口已经关了"），与 /auth/register 的恒 403 对齐。
        # 机器可读错误码仍保留 ALREADY_INITIALIZED，前端判断逻辑不受影响。
        raise ForbiddenError("系统已完成初始化", code=ErrorCode.ALREADY_INITIALIZED)
    validate_password_policy(password)
    moment = now or utcnow()
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=UserRole.ADMIN.value,
        is_active=True,
        created_at=moment,
        updated_at=moment,
    )
    db.add(user)
    db.flush()
    return _issue_token_pair(db, user, user_agent=user_agent, now=moment)


@retry_on_busy
def login(
    db: Session,
    username: str,
    password: str,
    *,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> TokenPair:
    """账号登录。

    Raises:
        UnauthorizedError: 用户名或密码错误（不区分，防枚举）。
        ForbiddenError: 账号已停用。
    """
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        raise UnauthorizedError("用户名或密码错误", code=ErrorCode.INVALID_CREDENTIALS)
    if not user.is_active:
        raise ForbiddenError("账号已停用", code=ErrorCode.USER_DISABLED)
    return _issue_token_pair(db, user, user_agent=user_agent, now=now)


def _revoke_family(db: Session, family_id: str, now: datetime) -> int:
    """撤销整条轮换链上尚未撤销的全部 token。"""
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return int(db.execute(stmt).rowcount or 0)


@retry_on_busy
def refresh(
    db: Session,
    refresh_token: str,
    *,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> TokenPair:
    """刷新令牌，轮换发放（D37）。

    检测到已撤销/已被替换的 token 被再次使用时，撤销整条 `family_id` 链，
    写 `TOKEN_REUSE_DETECTED` 事件并返回 401。

    Raises:
        UnauthorizedError: 令牌无效、已过期或检测到复用。
        ForbiddenError: 账号已停用。
    """
    moment = now or utcnow()
    token_hash = sha256_hex(refresh_token)
    row = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None:
        raise UnauthorizedError(
            "刷新令牌无效或已过期", code=ErrorCode.INVALID_REFRESH_TOKEN
        )

    if row.revoked_at is not None or row.replaced_by is not None:
        _revoke_family(db, row.family_id, moment)
        event_service.record(
            db,
            EventType.TOKEN_REUSE_DETECTED,
            severity=EventSeverity.ERROR,
            user_id=row.user_id,
            payload={
                "family_id": row.family_id,
                "reason": "revoked" if row.revoked_at is not None else "replaced",
            },
            now=moment,
        )
        raise UnauthorizedError(
            "检测到令牌复用，已撤销全部会话", code=ErrorCode.REFRESH_TOKEN_REUSED
        )

    if row.expires_at <= moment:
        raise UnauthorizedError(
            "刷新令牌无效或已过期", code=ErrorCode.INVALID_REFRESH_TOKEN
        )

    user = db.get(User, row.user_id)
    if user is None:
        raise UnauthorizedError(
            "刷新令牌无效或已过期", code=ErrorCode.INVALID_REFRESH_TOKEN
        )
    if not user.is_active:
        raise ForbiddenError("账号已停用", code=ErrorCode.USER_DISABLED)

    access_token, expires_in = create_access_token(user.id, user.role)
    raw_refresh, new_hash, expires_at = create_refresh_token(now=moment)

    row.revoked_at = moment
    row.replaced_by = new_hash
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=new_hash,
            family_id=row.family_id,
            issued_at=moment,
            expires_at=expires_at,
            user_agent=(user_agent or row.user_agent),
        )
    )
    db.flush()
    return TokenPair(
        access_token=access_token,
        refresh_token=raw_refresh,
        token_type="bearer",
        expires_in=expires_in,
        user=_user_out(user),
    )


@retry_on_busy
def logout(
    db: Session,
    user: User,
    refresh_token: str | None = None,
    *,
    now: datetime | None = None,
) -> None:
    """登出：撤销指定 token 所属 family，或该用户全部 family。"""
    moment = now or utcnow()
    if refresh_token:
        row = db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == sha256_hex(refresh_token),
                RefreshToken.user_id == user.id,
            )
        ).scalar_one_or_none()
        if row is not None:
            _revoke_family(db, row.family_id, moment)
            return
    revoke_all_user_tokens(db, user.id, now=moment)


def revoke_all_user_tokens(db: Session, user_id: int, *, now: datetime | None = None) -> int:
    """撤销某用户的全部未撤销 refresh token。"""
    moment = now or utcnow()
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=moment)
    )
    return int(db.execute(stmt).rowcount or 0)


@retry_on_busy
def change_password(
    db: Session,
    user: User,
    old_password: str,
    new_password: str,
    *,
    now: datetime | None = None,
) -> None:
    """修改密码，副作用为撤销该用户全部 refresh token。

    Raises:
        UnauthorizedError: 旧密码错误。
    """
    if not verify_password(old_password, user.password_hash):
        raise UnauthorizedError("用户名或密码错误", code=ErrorCode.INVALID_CREDENTIALS)
    validate_password_policy(new_password)
    moment = now or utcnow()
    user.password_hash = hash_password(new_password)
    user.updated_at = moment
    revoke_all_user_tokens(db, user.id, now=moment)
    db.flush()
