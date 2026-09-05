"""偏好服务：仪表盘布局与主题读写。"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_DASHBOARD_LAYOUT_JSON
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.user import User, UserPreference
from app.schemas.preference import PreferenceIn, PreferenceOut


def get_or_create(
    db: Session, user: User, *, now: datetime | None = None
) -> UserPreference:
    """读取用户偏好，不存在时按默认值创建。"""
    pref = db.get(UserPreference, user.id)
    if pref is not None:
        return pref
    moment = now or utcnow()
    pref = UserPreference(
        user_id=user.id,
        dashboard_layout_json=DEFAULT_DASHBOARD_LAYOUT_JSON,
        theme="default",
        updated_at=moment,
    )
    db.add(pref)
    db.flush()
    return pref


@retry_on_busy
def update_preference(
    db: Session, user: User, payload: PreferenceIn, *, now: datetime | None = None
) -> UserPreference:
    """全量替换用户偏好。"""
    moment = now or utcnow()
    pref = get_or_create(db, user, now=moment)
    pref.dashboard_layout_json = json.dumps(payload.dashboard_layout, ensure_ascii=False)
    pref.theme = payload.theme
    pref.updated_at = moment
    db.flush()
    return pref


def to_out(pref: UserPreference) -> PreferenceOut:
    """把 ORM 偏好转成出参。"""
    return PreferenceOut(
        dashboard_layout=pref.dashboard_layout,
        theme=pref.theme,  # type: ignore[arg-type]
        updated_at=pref.updated_at,
    )
