"""规则模板服务：CRUD / 应用到设备 / 应用状态（落地架构设计 §2.3）。

应用语义（方案 A · 复制）：逐台把模板 11 参数覆盖写入设备 `rule_profiles`，
`version+1`、写 `RULE_UPDATED` 事件（含 diff + source="rule_template" + 溯源）、
记录 `template_id`。设备规则仍是最终快照，强隔离语义不破。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.constants import DeviceStatus, EventType
from app.core.errors import BadRequestError, ErrorCode, NotFoundError
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.device import Device
from app.models.rule import RuleProfile
from app.models.rule_template import RuleTemplate
from app.models.user import User
from app.schemas.rule_template import (
    RuleTemplateApplyIn,
    RuleTemplateApplyItem,
    RuleTemplateApplyOut,
    RuleTemplateDeviceOut,
    RuleTemplateIn,
    RuleTemplateOut,
)
from app.services import device_service, event_service, rule_service


def get_template_or_404(db: Session, template_id: int) -> RuleTemplate:
    """按 id 查询模板。

    Raises:
        NotFoundError: 模板不存在。
    """
    template = db.get(RuleTemplate, template_id)
    if template is None:
        raise NotFoundError("规则模板不存在", code=ErrorCode.RULE_TEMPLATE_NOT_FOUND)
    return template


def _applied_count(db: Session, template_id: int) -> int:
    """统计已溯源到该模板的设备规则数。"""
    stmt = (
        select(func.count())
        .select_from(RuleProfile)
        .where(RuleProfile.template_id == template_id)
    )
    return int(db.execute(stmt).scalar_one() or 0)


def to_out(db: Session, template: RuleTemplate) -> RuleTemplateOut:
    """把 ORM 模板转成出参（聚合 `applied_device_count`）。"""
    return RuleTemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        is_builtin=template.is_builtin,
        weekday_quota_minutes=template.weekday_quota_minutes,
        weekend_quota_minutes=template.weekend_quota_minutes,
        allowed_start=template.allowed_start,
        allowed_end=template.allowed_end,
        continuous_limit_minutes=template.continuous_limit_minutes,
        break_minutes=template.break_minutes,
        idle_minutes=template.idle_minutes,
        reminder_points=template.reminder_points,
        parent_mode_timeout_minutes=template.parent_mode_timeout_minutes,
        sync_interval_seconds=template.sync_interval_seconds,
        enforcement_enabled=template.enforcement_enabled,
        version=template.version,
        created_by=template.created_by,
        created_by_name=template.created_by_name,
        created_at=template.created_at,
        updated_at=template.updated_at,
        updated_by=template.updated_by,
        updated_by_name=template.updated_by_name,
        applied_device_count=_applied_count(db, template.id),
    )


def _apply_payload(template: RuleTemplate, rule: RuleProfile) -> None:
    """把模板 11 参数复制到设备规则对象（不 flush，由调用方统一提交）。"""
    rule.weekday_quota_minutes = template.weekday_quota_minutes
    rule.weekend_quota_minutes = template.weekend_quota_minutes
    rule.allowed_start = template.allowed_start
    rule.allowed_end = template.allowed_end
    rule.continuous_limit_minutes = template.continuous_limit_minutes
    rule.break_minutes = template.break_minutes
    rule.idle_minutes = template.idle_minutes
    rule.reminder_points = template.reminder_points
    rule.parent_mode_timeout_minutes = template.parent_mode_timeout_minutes
    rule.sync_interval_seconds = template.sync_interval_seconds
    rule.enforcement_enabled = template.enforcement_enabled


@retry_on_busy
def create_template(
    db: Session,
    payload: RuleTemplateIn,
    user: User,
    *,
    now: datetime | None = None,
) -> RuleTemplate:
    """创建模板：version=1、is_builtin=False，写 `RULE_TEMPLATE_CREATED` 事件。"""
    moment = now or utcnow()
    template = RuleTemplate(
        name=payload.name.strip(),
        description=payload.description,
        is_builtin=False,
        weekday_quota_minutes=payload.weekday_quota_minutes,
        weekend_quota_minutes=payload.weekend_quota_minutes,
        allowed_start=payload.allowed_start,
        allowed_end=payload.allowed_end,
        continuous_limit_minutes=payload.continuous_limit_minutes,
        break_minutes=payload.break_minutes,
        idle_minutes=payload.idle_minutes,
        parent_mode_timeout_minutes=payload.parent_mode_timeout_minutes,
        sync_interval_seconds=payload.sync_interval_seconds,
        enforcement_enabled=payload.enforcement_enabled,
        version=1,
        created_by=user.id,
        created_at=moment,
        updated_at=moment,
        updated_by=user.id,
    )
    template.reminder_points = payload.reminder_points
    db.add(template)
    db.flush()
    event_service.record(
        db,
        EventType.RULE_TEMPLATE_CREATED,
        user_id=user.id,
        payload={
            "template_id": template.id,
            "name": template.name,
            "version": template.version,
        },
        now=moment,
    )
    return template


@retry_on_busy
def update_template(
    db: Session,
    template: RuleTemplate,
    payload: RuleTemplateIn,
    user: User,
    *,
    now: datetime | None = None,
) -> RuleTemplate:
    """全量更新模板：version+1；内置模板允许改参数、不允许改名。

    Raises:
        BadRequestError: 试图修改内置模板名称。
    """
    moment = now or utcnow()
    if template.is_builtin and payload.name.strip() != template.name:
        raise BadRequestError(
            "内置模板不允许改名", code=ErrorCode.BUILTIN_TEMPLATE_PROTECTED
        )
    template.name = payload.name.strip()
    template.description = payload.description
    template.weekday_quota_minutes = payload.weekday_quota_minutes
    template.weekend_quota_minutes = payload.weekend_quota_minutes
    template.allowed_start = payload.allowed_start
    template.allowed_end = payload.allowed_end
    template.continuous_limit_minutes = payload.continuous_limit_minutes
    template.break_minutes = payload.break_minutes
    template.idle_minutes = payload.idle_minutes
    template.reminder_points = payload.reminder_points
    template.parent_mode_timeout_minutes = payload.parent_mode_timeout_minutes
    template.sync_interval_seconds = payload.sync_interval_seconds
    template.enforcement_enabled = payload.enforcement_enabled
    template.version = template.version + 1
    template.updated_at = moment
    template.updated_by = user.id
    db.flush()
    event_service.record(
        db,
        EventType.RULE_TEMPLATE_UPDATED,
        user_id=user.id,
        payload={
            "template_id": template.id,
            "name": template.name,
            "version": template.version,
        },
        now=moment,
    )
    return template


@retry_on_busy
def delete_template(
    db: Session,
    template: RuleTemplate,
    user: User,
    *,
    now: datetime | None = None,
) -> None:
    """删除模板：内置禁止；已应用设备 `template_id` 置空（设备规则不受影响）。

    Raises:
        BadRequestError: 试图删除内置模板。
    """
    moment = now or utcnow()
    if template.is_builtin:
        raise BadRequestError(
            "内置模板不允许删除", code=ErrorCode.BUILTIN_TEMPLATE_PROTECTED
        )
    # SQLite 默认未强制 FK 级联，显式置空已应用设备溯源（双保险）
    db.execute(
        update(RuleProfile)
        .where(RuleProfile.template_id == template.id)
        .values(template_id=None)
    )
    db.delete(template)
    db.flush()
    event_service.record(
        db,
        EventType.RULE_TEMPLATE_DELETED,
        user_id=user.id,
        payload={
            "template_id": template.id,
            "name": template.name,
            "version": template.version,
        },
        now=moment,
    )


def list_templates(
    db: Session,
    *,
    q: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[RuleTemplateOut], int]:
    """分页查询模板，`q` 对名称模糊；返回 `(当前页, 总数)`。"""
    base = select(RuleTemplate)
    if q:
        base = base.where(RuleTemplate.name.like(f"%{q}%"))
    total = int(
        db.execute(select(func.count()).select_from(base.subquery())).scalar_one() or 0
    )
    rows = (
        db.execute(
            base.order_by(RuleTemplate.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return [to_out(db, row) for row in rows], total


@retry_on_busy
def apply_template(
    db: Session,
    template: RuleTemplate,
    payload: RuleTemplateApplyIn,
    user: User,
    *,
    now: datetime | None = None,
) -> RuleTemplateApplyOut:
    """应用模板到目标设备（复制语义）：逐台覆盖 + version+1 + 事件 + 溯源。

    Raises:
        BadRequestError: `all=false` 且 `device_ids` 为空。
    """
    moment = now or utcnow()
    if payload.all:
        target_ids = list(
            db.execute(
                select(Device.id).where(Device.status == DeviceStatus.ACTIVE.value)
            ).scalars().all()
        )
    else:
        target_ids = list(dict.fromkeys(payload.device_ids or []))
    if not target_ids:
        raise BadRequestError(
            "请指定至少一台目标设备", code=ErrorCode.RULE_TEMPLATE_APPLY_EMPTY
        )

    applied: list[str] = []
    skipped: list[RuleTemplateApplyItem] = []
    for device_id in target_ids:
        device = db.get(Device, device_id)
        if device is None:
            skipped.append(
                RuleTemplateApplyItem(device_id=device_id, reason="device_not_found")
            )
            continue
        if device.status != DeviceStatus.ACTIVE.value:
            skipped.append(
                RuleTemplateApplyItem(device_id=device_id, reason="device_retired")
            )
            continue

        rule = rule_service.get_rule(db, device_id)
        if rule is None:
            rule = rule_service.create_default_rule(db, device_id, now=moment)
        before = rule_service.snapshot(rule)
        _apply_payload(template, rule)
        rule.version = rule.version + 1
        rule.updated_at = moment
        rule.updated_by = user.id
        rule.template_id = template.id
        db.flush()

        after = rule_service.snapshot(rule)
        diff = {
            field: {"from": before[field], "to": after[field]}
            for field in rule_service.DIFF_FIELDS
            if before[field] != after[field]
        }
        event_service.record(
            db,
            EventType.RULE_UPDATED,
            device_id=device_id,
            user_id=user.id,
            payload={
                "version": rule.version,
                "diff": json.loads(json.dumps(diff, default=str)),
                "source": "rule_template",
                "template_id": template.id,
                "template_name": template.name,
            },
            now=moment,
        )
        applied.append(device_id)

    event_service.record(
        db,
        EventType.RULE_TEMPLATE_APPLIED,
        user_id=user.id,
        payload={
            "template_id": template.id,
            "template_name": template.name,
            "version": template.version,
            "applied_count": len(applied),
            "skipped_count": len(skipped),
        },
        now=moment,
    )
    return RuleTemplateApplyOut(applied=applied, skipped=skipped)


def _tweaked(template: RuleTemplate, rule: RuleProfile | None) -> bool:
    """设备 11 参数与模板任一不同即为「已微调」；无规则设备不算。"""
    if rule is None:
        return False
    return any(
        getattr(rule, field) != getattr(template, field)
        for field in rule_service.DIFF_FIELDS
    )


def list_template_devices(
    db: Session,
    template: RuleTemplate,
    *,
    now: datetime | None = None,
) -> list[RuleTemplateDeviceOut]:
    """全量 active 设备应用状态（T7）：`is_tweaked` + `applied_at` 溯源。"""
    moment = now or utcnow()
    devices = (
        db.execute(
            select(Device)
            .where(Device.status == DeviceStatus.ACTIVE.value)
            .order_by(Device.created_at.asc())
        )
        .scalars()
        .all()
    )
    result: list[RuleTemplateDeviceOut] = []
    for device in devices:
        rule = rule_service.get_rule(db, device.id)
        applied_at: datetime | None = (
            rule.updated_at if rule is not None and rule.template_id == template.id else None
        )
        result.append(
            RuleTemplateDeviceOut(
                device_id=device.id,
                device_name=device.name,
                online_status=device_service.compute_online_status(
                    device.last_seen_at, moment
                ),
                is_tweaked=_tweaked(template, rule),
                applied_at=applied_at,
            )
        )
    return result
