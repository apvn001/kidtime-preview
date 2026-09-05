"""通用 Schema：时间格式、分页、错误体（API.md §1）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

T = TypeVar("T")


def format_utc(value: datetime) -> str:
    """把 datetime 渲染成 `2025-08-06T10:23:45Z`（API.md §1.4）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcTimestamp = Annotated[datetime, PlainSerializer(format_utc, return_type=str)]
"""统一的 UTC ISO8601 时间戳类型（输出带 `Z`，秒级精度）。"""


class ApiModel(BaseModel):
    """所有出参模型的基类：允许从 ORM 属性读取。"""

    model_config = ConfigDict(from_attributes=True)


class PageMeta(BaseModel):
    """分页元信息。"""

    page: int = Field(1, ge=1, description="当前页码")
    size: int = Field(20, ge=1, le=200, description="每页条数")
    total: int = Field(0, ge=0, description="总条数")
    pages: int = Field(0, ge=0, description="总页数")


class Page(BaseModel, Generic[T]):
    """统一分页响应体（API.md §1.6）。"""

    items: list[T] = Field(default_factory=list, description="当前页数据")
    page: int = Field(1, ge=1, description="当前页码")
    size: int = Field(20, ge=1, description="每页条数")
    total: int = Field(0, ge=0, description="总条数")
    pages: int = Field(0, ge=0, description="总页数")

    @classmethod
    def build(cls, items: list[T], page: int, size: int, total: int) -> "Page[T]":
        """按条数计算 `pages` 并构造分页体。"""
        pages = (total + size - 1) // size if size > 0 else 0
        return cls(items=items, page=page, size=size, total=total, pages=pages)


class ErrorResponse(BaseModel):
    """统一错误响应体（API.md §1.5）。"""

    code: str = Field(..., description="机器可读错误码")
    message: str = Field(..., description="中文人类可读描述")
    details: Any = Field(None, description="附加信息，422 时为字段错误数组")
    request_id: str = Field(..., description="与 X-Request-Id 一致")


class OkResponse(BaseModel):
    """简单成功响应。"""

    ok: bool = Field(True, description="恒为 true")
