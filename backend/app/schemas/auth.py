"""初始化与认证 Schema（API.md §2）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.security import validate_password_policy, validate_username
from app.schemas.common import ApiModel, UtcTimestamp
from app.schemas.preference import PreferenceOut
from app.schemas.user import UserOut


class SetupStatusOut(BaseModel):
    """`GET /setup/status` 出参。"""

    initialized: bool = Field(..., description="是否已存在 admin 账号")
    server_time: UtcTimestamp = Field(..., description="服务端 UTC 时间")
    version: str = Field(..., description="后端版本")


class SetupInitIn(BaseModel):
    """`POST /setup/init` 入参。"""

    username: str = Field(..., min_length=3, max_length=64, description="管理员用户名")
    password: str = Field(..., min_length=8, description="管理员密码，≥8 位且含字母与数字")

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        """校验用户名字符集。"""
        return validate_username(value)

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        """校验密码强度（D38）。"""
        validate_password_policy(value)
        return value


class LoginIn(BaseModel):
    """`POST /auth/login` 入参。"""

    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class TokenPair(ApiModel):
    """令牌对（API.md §2.3）。"""

    access_token: str = Field(..., description="访问令牌 JWT")
    refresh_token: str = Field(..., description="刷新令牌，不透明随机串")
    token_type: Literal["bearer"] = Field("bearer", description="恒为 bearer")
    expires_in: int = Field(..., description="access token 剩余秒数")
    user: UserOut = Field(..., description="当前用户")


class RefreshIn(BaseModel):
    """`POST /auth/refresh` 入参。"""

    refresh_token: str = Field(..., min_length=1, description="刷新令牌")


class LogoutIn(BaseModel):
    """`POST /auth/logout` 入参。"""

    refresh_token: str | None = Field(
        None, description="提供则撤销该 token 及其 family；不提供则撤销该用户全部 family"
    )


class ChangePasswordIn(BaseModel):
    """`POST /auth/change-password` 入参。"""

    old_password: str = Field(..., min_length=1, description="旧密码")
    new_password: str = Field(..., min_length=8, description="新密码，≥8 位且含字母与数字")

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        """校验新密码强度。"""
        validate_password_policy(value)
        return value


class MeOut(ApiModel):
    """`GET /auth/me` 出参。"""

    id: int = Field(..., description="用户 id")
    username: str = Field(..., description="用户名")
    role: Literal["admin", "parent"] = Field(..., description="角色")
    is_active: bool = Field(..., description="是否启用")
    created_at: UtcTimestamp = Field(..., description="创建时间 UTC")
    preferences: PreferenceOut = Field(..., description="个人偏好")
