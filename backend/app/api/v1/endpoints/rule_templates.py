"""管控规则模板端点（落地架构设计 §2.3：T1-T7）。

统一前缀 `/api/v1/rule-templates`，全部端点 parent / admin 均可用（与既有
`GET/PUT /devices/{id}/rules` 一致）；写端点声明 `IdempotencyKeyDep`。
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Query

from app.api.deps import CurrentUser, DbSession, IdempotencyKeyDep, PageDep
from app.schemas.common import OkResponse, Page
from app.schemas.rule_template import (
    RuleTemplateApplyIn,
    RuleTemplateApplyOut,
    RuleTemplateDeviceOut,
    RuleTemplateIn,
    RuleTemplateOut,
)
from app.services import rule_template_service

router = APIRouter(prefix="/rule-templates", tags=["管控规则"])


@router.post("", response_model=RuleTemplateOut, summary="创建规则模板")
def create_template(
    payload: RuleTemplateIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
) -> RuleTemplateOut:
    """T1 创建模板（name + 11 参数，校验同 `RuleProfileIn`）。"""
    template = rule_template_service.create_template(db, payload, user)
    return rule_template_service.to_out(db, template)


@router.get("", response_model=Page[RuleTemplateOut], summary="规则模板列表")
def list_templates(
    db: DbSession,
    _user: CurrentUser,
    pagination: PageDep,
    q: str | None = Query(None, max_length=64, description="模板名称模糊搜索"),
) -> Page[RuleTemplateOut]:
    """T2 模板列表分页，`q` 对名称模糊。"""
    items, total = rule_template_service.list_templates(
        db, q=q, page=pagination.page, size=pagination.size
    )
    return Page.build(items, pagination.page, pagination.size, total)


@router.get("/{template_id}", response_model=RuleTemplateOut, summary="规则模板详情")
def get_template(
    db: DbSession,
    _user: CurrentUser,
    template_id: int = Path(..., ge=1, description="模板 id"),
) -> RuleTemplateOut:
    """T3 模板详情。"""
    template = rule_template_service.get_template_or_404(db, template_id)
    return rule_template_service.to_out(db, template)


@router.put("/{template_id}", response_model=RuleTemplateOut, summary="全量更新规则模板")
def update_template(
    payload: RuleTemplateIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
    template_id: int = Path(..., ge=1, description="模板 id"),
) -> RuleTemplateOut:
    """T4 全量更新模板：version+1；内置模板允许改参数、不允许改名。"""
    template = rule_template_service.get_template_or_404(db, template_id)
    updated = rule_template_service.update_template(db, template, payload, user)
    return rule_template_service.to_out(db, updated)


@router.delete("/{template_id}", response_model=OkResponse, summary="删除规则模板")
def delete_template(
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
    template_id: int = Path(..., ge=1, description="模板 id"),
) -> OkResponse:
    """T5 删除模板：内置返回 400；已应用设备 `template_id` 置空。"""
    template = rule_template_service.get_template_or_404(db, template_id)
    rule_template_service.delete_template(db, template, user)
    return OkResponse()


@router.post(
    "/{template_id}/apply",
    response_model=RuleTemplateApplyOut,
    summary="应用模板到设备",
)
def apply_template(
    payload: RuleTemplateApplyIn,
    db: DbSession,
    user: CurrentUser,
    _idempotency_key: IdempotencyKeyDep,
    template_id: int = Path(..., ge=1, description="模板 id"),
) -> RuleTemplateApplyOut:
    """T6 应用模板：逐台复制 11 参数 + version+1 + 事件 + 溯源。"""
    template = rule_template_service.get_template_or_404(db, template_id)
    return rule_template_service.apply_template(db, template, payload, user)


@router.get(
    "/{template_id}/devices",
    response_model=list[RuleTemplateDeviceOut],
    summary="模板应用状态",
)
def list_template_devices(
    db: DbSession,
    _user: CurrentUser,
    template_id: int = Path(..., ge=1, description="模板 id"),
) -> list[RuleTemplateDeviceOut]:
    """T7 全量 active 设备应用状态：`is_tweaked` / `applied_at`。"""
    template = rule_template_service.get_template_or_404(db, template_id)
    return rule_template_service.list_template_devices(db, template)
