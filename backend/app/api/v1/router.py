"""API v1 总路由：统一 prefix `/api/v1`，汇总全部 37 个端点。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    client,
    client_version,
    commands,
    dashboard,
    devices,
    events,
    extensions,
    grants,
    preferences,
    rules,
    rule_templates,
    setup,
    usage,
    users,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(setup.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(devices.router)
api_router.include_router(rules.router)
api_router.include_router(rule_templates.router)
api_router.include_router(usage.router)
api_router.include_router(extensions.router)
api_router.include_router(grants.router)
api_router.include_router(commands.router)
api_router.include_router(events.router)
api_router.include_router(dashboard.router)
api_router.include_router(preferences.router)
api_router.include_router(client.router)
# PC 客户端版本更新（MVP 方案 A）：匿名客户端端点 + 管理端发布端点。
api_router.include_router(client_version.client_router)
api_router.include_router(client_version.admin_router)

__all__ = ["api_router"]
