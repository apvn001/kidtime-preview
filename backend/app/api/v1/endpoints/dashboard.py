"""仪表盘端点（API.md §9.1 / §9.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.dashboard import DashboardOverviewOut, TrendsOut
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["仪表盘"])


@router.get("/overview", response_model=DashboardOverviewOut, summary="首页 KPI 概览")
def get_overview(db: DbSession, _user: CurrentUser) -> DashboardOverviewOut:
    """返回 5 张 KPI 卡片数据与 active 设备速览。"""
    return dashboard_service.overview(db)


@router.get("/trends", response_model=TrendsOut, summary="7/30 天趋势")
def get_trends(
    db: DbSession,
    _user: CurrentUser,
    days: int = Query(7, description="仅接受 7 或 30"),
    device_id: str | None = Query(None, description="缺省为全部设备合计"),
) -> TrendsOut:
    """返回连续日期趋势点，缺失日补 0。"""
    return dashboard_service.trends(db, days, device_id)
