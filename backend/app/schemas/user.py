"""用户相关 Schema（API.md §3）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.security import validate_password_policy, validate_username
from app.schemas.common import ApiModel, UtcTimestamp


class UserOut(ApiModel):
    """账号出参（API.md §3.1）。"""

    id: int = Field(..., description="用户 id")
    username: str = Field(..., description="用户名")
    role: Literal["admin", "parent"] = Field(..., description="角色")
    is_active: bool = Field(..., description="是否启用")
    created_at: UtcTimestamp = Field(..., description="创建时间 UTC")


class UserCreateIn(BaseModel):
    """创建账号入参（API.md §3.2）。"""

    username: str = Field(..., min_length=3, max_length=64, description="用户名")
    password: str = Field(..., min_length=8, description="密码，≥8 位且含字母与数字")
    role: Literal["admin", "parent"] = Field("parent", description="角色，默认 parent")

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


class UserUpdateIn(BaseModel):
    """更新账号入参（API.md §3.3），全部可选但至少一项。"""

    role: Literal["admin", "parent"] | None = Field(None, description="新角色")
    is_active: bool | None = Field(None, description="启用/停用")
    new_password: str | None = Field(None, min_length=8, description="重置密码")

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str | None) -> str | None:
        """校验新密码强度。"""
        if value is not None:
            validate_password_policy(value)
        return value

    @model_validator(mode="after")
    def _at_least_one(self) -> "UserUpdateIn":
        """至少提供一个待更新字段。"""
        if self.role is None and self.is_active is None and self.new_password is None:
            raise ValueError("至少需要提供 role / is_active / new_password 之一")
        return self
