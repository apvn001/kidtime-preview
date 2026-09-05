"""幂等 seed 内置默认模板（V8 默认规则模板修复）

背景：
  0002_rule_templates 建表时已带一条 is_builtin=True 的「默认模板」seed，
  但早期某些环境（手动建表 / 跳过 seed 的旧库）可能缺失该行，导致
  `rule_service.create_default_rule` 回退写死常量。

  本迁移做**幂等补种**：仅当 `rule_templates` 中不存在 `is_builtin=1` 的行时
  才插入一条「默认模板」（参数 = app/core/constants.py DEFAULT_* 常量，与代码同步维护）。
  已存在内置模板的库执行本迁移为 no-op。

Revision ID: 0009_seed_builtin_template
Revises: 0008_deduct_usage
Create Date: 2026-08-22 10:00:00.000000

"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.base  # noqa: F401  提供 UtcDateTime 自定义类型


revision: str = "0009_seed_builtin_template"
down_revision: Union[str, None] = "0008_deduct_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 幂等保护：已存在内置模板（is_builtin=1）则跳过，避免与 0002 的 seed 重复
    seed_timestamp = datetime.now(timezone.utc)
    op.execute(
        sa.text(
            """
            INSERT INTO rule_templates (
                name, description, is_builtin,
                weekday_quota_minutes, weekend_quota_minutes,
                allowed_start, allowed_end,
                continuous_limit_minutes, break_minutes, idle_minutes,
                reminder_points_json,
                parent_mode_timeout_minutes, sync_interval_seconds,
                enforcement_enabled, version,
                created_by, created_at, updated_at, updated_by
            )
            SELECT
                '默认模板', '系统内置默认规则', 1,
                40, 40,
                '08:00', '21:00',
                30, 10, 5,
                '[10, 5, 1]',
                15, 45,
                1, 1,
                NULL, :seed_ts, :seed_ts, NULL
            WHERE NOT EXISTS (
                SELECT 1 FROM rule_templates WHERE is_builtin = 1 LIMIT 1
            )
            """
        ).bindparams(seed_ts=seed_timestamp)
    )


def downgrade() -> None:
    # 只回滚本迁移补种的行：删除无设备规则溯源的「默认模板」
    op.execute(
        sa.text(
            """
            DELETE FROM rule_templates
            WHERE is_builtin = 1
              AND name = '默认模板'
              AND NOT EXISTS (
                  SELECT 1 FROM rule_profiles
                  WHERE rule_profiles.template_id = rule_templates.id
              )
            """
        )
    )
