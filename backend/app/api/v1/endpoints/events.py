"""事件日志端点（API.md §8.6 / §8.7）。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession, PageDepLarge
from app.schemas.common import Page
from app.schemas.event import EventOut
from app.services import event_service

router = APIRouter(prefix="/events", tags=["事件日志"])


@router.get("", response_model=Page[EventOut], summary="事件列表")
def list_events(
    db: DbSession,
    _user: CurrentUser,
    pagination: PageDepLarge,
    device_id: str | None = Query(None, description="按设备过滤"),
    type_filter: str | None = Query(
        None, alias="type", description="逗号分隔多值，如 TIME_TAMPER,SYNC_FAILED"
    ),
    severity_filter: str | None = Query(
        None, alias="severity", description="逗号分隔多值，info/warning/error"
    ),
    date_from: date | None = Query(None, description="按设备本地日过滤，起始"),
    date_to: date | None = Query(None, description="按设备本地日过滤，结束"),
    keyword: str | None = Query(None, alias="q", description="payload 模糊匹配"),
) -> Page[EventOut]:
    """分页查询事件，按 `occurred_at DESC`。"""
    items, total = event_service.list_events(
        db,
        device_id=device_id,
        type_filter=type_filter,
        severity_filter=severity_filter,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
        page=pagination.page,
        size=pagination.size,
    )
    return Page.build(items, pagination.page, pagination.size, total)


@router.get("/{event_id}", response_model=EventOut, summary="事件详情")
def get_event(
    db: DbSession,
    _user: CurrentUser,
    event_id: int = Path(..., description="事件 id"),
) -> EventOut:
    """按 id 返回单条事件。"""
    return event_service.get_event(db, event_id)
