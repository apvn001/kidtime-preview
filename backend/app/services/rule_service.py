"""规则服务：读写、`version` 自增、变更事件（D13/D18/D26）。"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import (
    DEFAULT_ALLOWED_END,
    DEFAULT_ALLOWED_START,
    DEFAULT_BREAK_MINUTES,
    DEFAULT_CONTINUOUS_LIMIT_MINUTES,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_PARENT_MODE_TIMEOUT_MINUTES,
    DEFAULT_REMINDER_POINTS_JSON,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    DEFAULT_WEEKDAY_QUOTA_MINUTES,
    DEFAULT_WEEKEND_QUOTA_MINUTES,
    EventType,
)
from app.core.errors import ErrorCode, NotFoundError
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.rule import RuleProfile
from app.models.rule_template import RuleTemplate
from app.models.user import User
from app.schemas.rule import RuleProfileIn, RuleProfileOut
from app.services import event_service

DIFF_FIELDS = (
    "weekday_quota_minutes",
    "weekend_quota_minutes",
    "allowed_start",
    "allowed_end",
    "continuous_limit_minutes",
    "break_minutes",
    "idle_minutes",
    "reminder_points",
    "parent_mode_timeout_minutes",
    "sync_interval_seconds",
    "enforcement_enabled",
)


def _builtin_template(db: Session) -> RuleTemplate | None:
    """取内置默认模板（`is_builtin=True` 至多一条，按 id 升序取第一条）。

    V8 修复：新配对客户端的默认规则改为「内置模板 = 默认模板」——管理员在管控规则页
    修改内置模板参数后，新配对设备自动应用最新参数，而不再写死 `DEFAULT_*` 常量。
    """
    return db.execute(
        select(RuleTemplate)
        .where(RuleTemplate.is_builtin.is_(True))
        .order_by(RuleTemplate.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def create_default_rule(
    db: Session, device_id: str, *, now: datetime | None = None
) -> RuleProfile:
    """为新配对的设备创建默认规则（D04）。

    V8 变更：优先复制内置模板（`RuleTemplate.is_builtin=True`）的 11 参数并记录
    `template_id` 溯源；无内置模板（如旧库未 seed）时回退 `DEFAULT_*` 常量。
    """
    moment = now or utcnow()
    template = _builtin_template(db)

    rule = RuleProfile(
        device_id=device_id,
        # 内置模板存在 → 以其 11 参数为准；否则回退常量默认值
        weekday_quota_minutes=(
            template.weekday_quota_minutes if template else DEFAULT_WEEKDAY_QUOTA_MINUTES
        ),
        weekend_quota_minutes=(
            template.weekend_quota_minutes if template else DEFAULT_WEEKEND_QUOTA_MINUTES
        ),
        allowed_start=template.allowed_start if template else DEFAULT_ALLOWED_START,
        allowed_end=template.allowed_end if template else DEFAULT_ALLOWED_END,
        continuous_limit_minutes=(
            template.continuous_limit_minutes
            if template
            else DEFAULT_CONTINUOUS_LIMIT_MINUTES
        ),
        break_minutes=template.break_minutes if template else DEFAULT_BREAK_MINUTES,
        idle_minutes=template.idle_minutes if template else DEFAULT_IDLE_MINUTES,
        reminder_points_json=(
            template.reminder_points_json if template else DEFAULT_REMINDER_POINTS_JSON
        ),
        parent_mode_timeout_minutes=(
            template.parent_mode_timeout_minutes
            if template
            else DEFAULT_PARENT_MODE_TIMEOUT_MINUTES
        ),
        sync_interval_seconds=(
            template.sync_interval_seconds if template else DEFAULT_SYNC_INTERVAL_SECONDS
        ),
        enforcement_enabled=(
            template.enforcement_enabled if template is not None else True
        ),
        template_id=template.id if template else None,
        version=1,
        updated_at=moment,
        updated_by=None,
    )
    db.add(rule)
    db.flush()
    return rule


def get_rule(db: Session, device_id: str) -> RuleProfile | None:
    """按设备 id 查询规则，不存在返回 ``None``。"""
    return db.get(RuleProfile, device_id)


def get_rule_or_404(db: Session, device_id: str) -> RuleProfile:
    """按设备 id 查询规则。

    Raises:
        NotFoundError: 该设备尚无规则配置。
    """
    rule = db.get(RuleProfile, device_id)
    if rule is None:
        raise NotFoundError("该设备尚无规则配置", code=ErrorCode.RULE_NOT_FOUND)
    return rule


def to_out(rule: RuleProfile) -> RuleProfileOut:
    """把 ORM 规则转成出参。"""
    # 11 个规则字段以 DIFF_FIELDS 为单一事实源，与 update_rules 共用
    _fields = {_f: getattr(rule, _f) for _f in DIFF_FIELDS}
    return RuleProfileOut(
        device_id=rule.device_id,
        **_fields,
        version=rule.version,
        template_id=rule.template_id,
        updated_at=rule.updated_at,
        updated_by=rule.updated_by,
        updated_by_name=rule.updated_by_name,
    )


def base_quota_for(rule: RuleProfile, target: date) -> int:
    """按 D13 计算指定日期的基础配额：周一至周五工作日，周六周日周末。"""
    return (
        rule.weekend_quota_minutes if target.weekday() >= 5 else rule.weekday_quota_minutes
    )


def snapshot(rule: RuleProfile) -> dict[str, Any]:
    """取出用于 diff 的字段快照（公共，供模板 apply / is_tweaked 比对复用）。"""
    return {field: getattr(rule, field) for field in DIFF_FIELDS}


@retry_on_busy
def update_rules(
    db: Session,
    device_id: str,
    payload: RuleProfileIn,
    user: User,
    *,
    now: datetime | None = None,
) -> RuleProfile:
    """全量替换规则，`version` 自增并写 `RULE_UPDATED` 事件（含 diff）。

    `reminder_points` 服务端去重后降序存储（D26）。
    """
    rule = get_rule_or_404(db, device_id)
    moment = now or utcnow()
    before = snapshot(rule)

    # 11 个规则字段以 DIFF_FIELDS 为单一事实源，避免新增参数时漏改
    for _field in DIFF_FIELDS:
        setattr(rule, _field, getattr(payload, _field))
    rule.version = rule.version + 1
    rule.updated_at = moment
    rule.updated_by = user.id
    db.flush()

    after = snapshot(rule)
    diff = {
        field: {"from": before[field], "to": after[field]}
        for field in DIFF_FIELDS
        if before[field] != after[field]
    }
    event_service.record(
        db,
        EventType.RULE_UPDATED,
        device_id=device_id,
        user_id=user.id,
        payload={"version": rule.version, "diff": json.loads(json.dumps(diff, default=str))},
        now=moment,
    )
    return rule


@retry_on_busy
def set_enforcement(
    db: Session,
    device_id: str,
    enabled: bool,
    user: User,
    *,
    now: datetime | None = None,
) -> RuleProfile:
    """改写 `enforcement_enabled` 并 `version += 1`（D18）。

    供 `PAUSE_ENFORCEMENT` / `RESUME_ENFORCEMENT` 指令在同一事务内调用。
    """
    rule = get_rule_or_404(db, device_id)
    if rule.enforcement_enabled == enabled:
        return rule
    moment = now or utcnow()
    before = rule.enforcement_enabled
    rule.enforcement_enabled = enabled
    rule.version = rule.version + 1
    rule.updated_at = moment
    rule.updated_by = user.id
    db.flush()
    event_service.record(
        db,
        EventType.RULE_UPDATED,
        device_id=device_id,
        user_id=user.id,
        payload={
            "version": rule.version,
            "diff": {"enforcement_enabled": {"from": before, "to": enabled}},
            "source": "remote_command",
        },
        now=moment,
    )
    return rule
