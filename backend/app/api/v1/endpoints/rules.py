"""规则端点（API.md §5）。多设备强隔离：改 A 不影响 B。"""

from __future__ import annotations

from fastapi import APIRouter, Path

from app.api.deps import CurrentUser, DbSession, IdempotencyKeyDep
from app.schemas.rule import RuleProfileIn, RuleProfileOut
from app.services import device_service, rule_service

router = APIRouter(prefix="/devices", tags=["规则"])


@router.get("/{device_id}/rules", response_model=RuleProfileOut, summary="获取设备规则")
def get_rules(
    db: DbSession,
    _user: CurrentUser,
    device_id: str = Path(..., description="设备 UUID"),
) -> RuleProfileOut:
    """返回该设备当前规则快照。"""
    device_service.get_device_or_404(db, device_id)
    return rule_service.to_out(rule_service.get_rule_or_404(db, device_id))


@router.put("/{device_id}/rules", response_model=RuleProfileOut, summary="全量替换设备规则")
def update_rules(
    payload: RuleProfileIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
    device_id: str = Path(..., description="设备 UUID"),
) -> RuleProfileOut:
    """全量替换规则，`version` 自增并写 `RULE_UPDATED` 事件（parent 亦可，D36）。"""
    device_service.get_device_or_404(db, device_id)
    return rule_service.to_out(rule_service.update_rules(db, device_id, payload, user))
