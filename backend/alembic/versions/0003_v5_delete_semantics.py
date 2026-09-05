"""v5: fk ondelete policy + operator name snapshots

本期唯一 revision（增量架构设计 §0.1 决策 A）：SQLite 的 DDL 具备事务性，
单 revision 失败即整体回滚，不会出现「改了 3 张表卡在第 4 张」的半截态。

变更内容（§2.1 / §2.2）：
  1. `pairing_codes.created_by`  → ON DELETE CASCADE（原无 ondelete）
  2. `pairing_codes.device_id`   → ON DELETE CASCADE（原无 ondelete）
  3. `remote_commands.created_by`     → NOT NULL 改可空 + ON DELETE SET NULL
  4. `extension_requests.decided_by`  → ON DELETE SET NULL
  5. `rule_profiles.updated_by`       → ON DELETE SET NULL
  6. `rule_templates.created_by`      → ON DELETE SET NULL
  7. `rule_templates.updated_by`      → ON DELETE SET NULL
  + 6 个操作人姓名快照列（与外键改造合并进同一次表重建，§0.1 决策 C）

⚠️ 外键开关由 `alembic/env.py` 在事务外处理（PRAGMA 在事务内是 no-op），
   本文件内不得再写 `PRAGMA foreign_keys`。

Revision ID: 0003_v5_delete_semantics
Revises: 0002
Create Date: 2026-08-14 09:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.base  # noqa: F401  提供 UtcDateTime 自定义类型


revision: str = '0003_v5_delete_semantics'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 操作人姓名快照列统一类型（与 users.username 的 String(64) 对齐）
_SNAP = sa.String(length=64)

# 本次需要整表重建的 5 张表
_REBUILT_TABLES: tuple[str, ...] = (
    'pairing_codes',
    'remote_commands',
    'extension_requests',
    'rule_profiles',
    'rule_templates',
)


def _assert_not_referenced(bind, tables: Sequence[str]) -> None:
    """确认这些表没有被其他表通过外键引用。

    SQLite ≥ 3.25 在 `ALTER TABLE ... RENAME` 时会自动改写其他表指向它的外键
    子句，可能把外部引用改指到 `_alembic_tmp_*` 临时表。经静态核查本期重建的
    5 张表均为叶子表，此处再做一次运行时断言兜底（§1.1 陷阱 T2、§10-Q7）。

    Args:
        bind: 当前迁移连接。
        tables: 待重建的表名列表。

    Raises:
        RuntimeError: 存在外部表引用了待重建表。
    """
    names = [
        row[0]
        for row in bind.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    targets = set(tables)
    for table_name in names:
        if table_name in targets:
            # 自引用不在防护范围内（本期 5 张表均无自引用）
            continue
        rows = bind.exec_driver_sql(
            f'PRAGMA foreign_key_list("{table_name}")'
        ).fetchall()
        for fk in rows:
            referred = fk[2]
            if referred in targets:
                raise RuntimeError(
                    f"迁移中止：表 {table_name} 通过外键引用了待重建表 {referred}，"
                    "整表重建会连带改写其外键指向，需改用显式重建流程。"
                )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        _assert_not_referenced(bind, _REBUILT_TABLES)

    # ── 1) pairing_codes：两条外键都补 CASCADE（瞬时凭据，随主体消亡）──────
    #     created_by 保持 NOT NULL（§10-Q1：配对码 TTL 分钟级，无审计价值；
    #     未过期的属于活凭据，创建人被删后应立即作废）
    with op.batch_alter_table('pairing_codes', schema=None, recreate='always') as batch_op:
        batch_op.drop_constraint('fk_pairing_codes_created_by_users', type_='foreignkey')
        batch_op.drop_constraint('fk_pairing_codes_device_id_devices', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_pairing_codes_created_by_users',
            'users', ['created_by'], ['id'], ondelete='CASCADE',
        )
        batch_op.create_foreign_key(
            'fk_pairing_codes_device_id_devices',
            'devices', ['device_id'], ['id'], ondelete='CASCADE',
        )

    # ── 2) remote_commands：NOT NULL → NULL + SET NULL + 姓名快照列 ────────
    #     ⚠️ 全库唯一的 downgrade 不可逆点（§3.5）
    with op.batch_alter_table('remote_commands', schema=None, recreate='always') as batch_op:
        batch_op.alter_column('created_by', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column('created_by_name', _SNAP, nullable=True))
        batch_op.drop_constraint('fk_remote_commands_created_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_remote_commands_created_by_users',
            'users', ['created_by'], ['id'], ondelete='SET NULL',
        )

    # ── 3) extension_requests ─────────────────────────────────────────────
    with op.batch_alter_table('extension_requests', schema=None, recreate='always') as batch_op:
        batch_op.add_column(sa.Column('decided_by_name', _SNAP, nullable=True))
        batch_op.drop_constraint('fk_extension_requests_decided_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_extension_requests_decided_by_users',
            'users', ['decided_by'], ['id'], ondelete='SET NULL',
        )

    # ── 4) rule_profiles ──────────────────────────────────────────────────
    with op.batch_alter_table('rule_profiles', schema=None, recreate='always') as batch_op:
        batch_op.add_column(sa.Column('updated_by_name', _SNAP, nullable=True))
        batch_op.drop_constraint('fk_rule_profiles_updated_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_rule_profiles_updated_by_users',
            'users', ['updated_by'], ['id'], ondelete='SET NULL',
        )

    # ── 5) rule_templates（两条操作人外键）─────────────────────────────────
    with op.batch_alter_table('rule_templates', schema=None, recreate='always') as batch_op:
        batch_op.add_column(sa.Column('created_by_name', _SNAP, nullable=True))
        batch_op.add_column(sa.Column('updated_by_name', _SNAP, nullable=True))
        batch_op.drop_constraint('fk_rule_templates_created_by_users', type_='foreignkey')
        batch_op.drop_constraint('fk_rule_templates_updated_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_rule_templates_created_by_users',
            'users', ['created_by'], ['id'], ondelete='SET NULL',
        )
        batch_op.create_foreign_key(
            'fk_rule_templates_updated_by_users',
            'users', ['updated_by'], ['id'], ondelete='SET NULL',
        )

    # ── 6) event_logs：仅加列，不改外键 → 无需重建，SQLite ADD COLUMN 为 O(1) ──
    op.add_column('event_logs', sa.Column('actor_name', _SNAP, nullable=True))


def downgrade() -> None:
    """降级。

    ⚠️ 见 §3.5：仅在「升级后从未执行过删账号」时可用。正式回滚请用备份文件
    整体还原（`scripts/rollback_v5.ps1`），不要依赖本函数。

    Raises:
        RuntimeError: `remote_commands.created_by` 已存在 NULL 行，物理上不可逆。
    """
    bind = op.get_bind()
    null_created_by = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM remote_commands WHERE created_by IS NULL"
    ).scalar()
    if null_created_by:
        raise RuntimeError(
            f"无法降级：remote_commands 存在 {null_created_by} 条 created_by 为空的记录"
            "（升级后已发生过账号删除，created_by 已被 SET NULL）。"
            "请改用备份文件整体还原：scripts/rollback_v5.ps1 -Stamp <ts>"
        )

    # 反序回退：event_logs → rule_templates → rule_profiles →
    #           extension_requests → remote_commands → pairing_codes
    op.drop_column('event_logs', 'actor_name')

    with op.batch_alter_table('rule_templates', schema=None, recreate='always') as batch_op:
        batch_op.drop_constraint('fk_rule_templates_created_by_users', type_='foreignkey')
        batch_op.drop_constraint('fk_rule_templates_updated_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_rule_templates_created_by_users', 'users', ['created_by'], ['id'],
        )
        batch_op.create_foreign_key(
            'fk_rule_templates_updated_by_users', 'users', ['updated_by'], ['id'],
        )
        batch_op.drop_column('updated_by_name')
        batch_op.drop_column('created_by_name')

    with op.batch_alter_table('rule_profiles', schema=None, recreate='always') as batch_op:
        batch_op.drop_constraint('fk_rule_profiles_updated_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_rule_profiles_updated_by_users', 'users', ['updated_by'], ['id'],
        )
        batch_op.drop_column('updated_by_name')

    with op.batch_alter_table('extension_requests', schema=None, recreate='always') as batch_op:
        batch_op.drop_constraint('fk_extension_requests_decided_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_extension_requests_decided_by_users', 'users', ['decided_by'], ['id'],
        )
        batch_op.drop_column('decided_by_name')

    with op.batch_alter_table('remote_commands', schema=None, recreate='always') as batch_op:
        batch_op.drop_constraint('fk_remote_commands_created_by_users', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_remote_commands_created_by_users', 'users', ['created_by'], ['id'],
        )
        batch_op.drop_column('created_by_name')
        batch_op.alter_column('created_by', existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table('pairing_codes', schema=None, recreate='always') as batch_op:
        batch_op.drop_constraint('fk_pairing_codes_created_by_users', type_='foreignkey')
        batch_op.drop_constraint('fk_pairing_codes_device_id_devices', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_pairing_codes_created_by_users', 'users', ['created_by'], ['id'],
        )
        batch_op.create_foreign_key(
            'fk_pairing_codes_device_id_devices', 'devices', ['device_id'], ['id'],
        )
