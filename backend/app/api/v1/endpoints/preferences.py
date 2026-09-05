"""偏好端点（API.md §9.3 / §9.4）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession, IdempotencyKeyDep
from app.schemas.preference import PreferenceIn, PreferenceOut
from app.services import preference_service

router = APIRouter(prefix="/preferences", tags=["偏好"])


@router.get("", response_model=PreferenceOut, summary="获取个人偏好")
def get_preferences(db: DbSession, user: CurrentUser) -> PreferenceOut:
    """返回仪表盘布局与主题。"""
    return preference_service.to_out(preference_service.get_or_create(db, user))


@router.put("", response_model=PreferenceOut, summary="全量替换个人偏好")
def update_preferences(
    payload: PreferenceIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
) -> PreferenceOut:
    """全量替换仪表盘布局与主题。"""
    return preference_service.to_out(
        preference_service.update_preference(db, user, payload)
    )
