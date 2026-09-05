"""全局枚举与默认值常量（API.md §11 / ARCHITECTURE.md §3）。

枚举一律使用 `str` 混入，便于直接与数据库中的字符串值比较、JSON 序列化。
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class UserRole(str, Enum):
    """账号角色（D35/D36）。"""

    ADMIN = "admin"
    PARENT = "parent"


class DeviceStatus(str, Enum):
    """设备状态（D06）。"""

    ACTIVE = "active"
    RETIRED = "retired"


class OnlineStatus(str, Enum):
    """在线状态（计算值，D08）。"""

    ONLINE = "online"
    STALE = "stale"
    OFFLINE = "offline"


class ClientState(str, Enum):
    """客户端 8 状态机取值。"""

    DISABLED = "DISABLED"
    GRACE = "GRACE"
    PARENT = "PARENT"
    LOCKED_CURFEW = "LOCKED_CURFEW"
    LOCKED_QUOTA = "LOCKED_QUOTA"
    BREAK = "BREAK"
    IDLE_PAUSED = "IDLE_PAUSED"
    ACTIVE = "ACTIVE"


class ExtensionStatus(str, Enum):
    """延时申请状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CREDITED = "credited"


class CommandType(str, Enum):
    """远程指令类型。

    ⚠️ ``RESET_USAGE`` 是 **admin-only** 指令（V7 重置当日用量）：家长端
    ``POST /commands`` 的 schema 已收窄，不允许下发该类型；仅 ``POST /usage/reset``
    端点在 ``require_admin`` 下经 ``command_service.create_batch`` 内部构造。
    """

    UNLOCK_TEMP = "UNLOCK_TEMP"
    PAUSE_ENFORCEMENT = "PAUSE_ENFORCEMENT"
    RESUME_ENFORCEMENT = "RESUME_ENFORCEMENT"
    SYNC_NOW = "SYNC_NOW"
    RESET_USAGE = "RESET_USAGE"
    # V7 功能一：重置当日用量。仅管理员可经内部服务层下发（见 ADMIN_ONLY_COMMAND_TYPES）。
    # 注意：本值必须与 backend/app/models/command.py 的 type_valid CHECK、
    # alembic 迁移 0004、desktop/kidtime_client/constants.py、frontend/src/types/api.ts
    # 四处副本完全一致（架构 §5.3 / 测试 test_v7_command_type_sync.py 守卫）。
    SET_LOCK_STYLE = "SET_LOCK_STYLE"
    # v1.4.2 锁屏外观三样式（admin-only，镜像 RESET_USAGE）。
    # 仅管理员可经内部服务层下发（见 ADMIN_ONLY_COMMAND_TYPES）；家长端 POST /commands
    # 的 schema 经 ADMIN_ONLY_COMMAND_TYPES 越权兜底拒绝。
    # 注意：本值必须与 backend/app/models/command.py 的 type_valid CHECK、alembic 迁移
    # 0005、desktop/kidtime_client/constants.py、frontend/src/types/api.ts 四处副本
    # 完全一致（架构 §5.3 / 测试 test_v7_reset_and_grants.py 守卫）。
    DEDUCT_USAGE = "DEDUCT_USAGE"
    # V7 功能三：扣减用量（已采纳方案：最小客户端改动，镜像 RESET_USAGE）。
    # 服务端在 deduct() 内已立即把 used_minutes += applied 作为唯一真相源，
    # 客户端执行 DEDUCT_USAGE 仅为冗余同步（本地 used_seconds += applied*60）。
    # 仅管理员可经内部服务层下发（见 ADMIN_ONLY_COMMAND_TYPES）。
    # 注意：本值必须与 backend/app/models/command.py 的 type_valid CHECK、alembic 迁移
    # 0008、desktop/kidtime_client/constants.py、frontend/src/types/api.ts 四处副本
    # 完全一致（架构 §5.3 / 测试 test_deduct_usage.py 守卫）。


#: 1.4.2 锁屏外观三样式合法取值（单一真相源，对应代码审查 B6）。
#: 同时充当白名单：``SET_LOCK_STYLE`` 指令里的非法 ``style`` 一律回退 ``"default"``。
#: 取值字符串必须与以下副本完全一致：desktop/kidtime_client/constants.py 的
#: ``LOCK_STYLE_ORDER``（客户端白名单兜底）、frontend/src/types/api.ts 的
#: ``ThemePreference``、以及 ``user_preferences.theme`` 的 CHECK 约束。
LOCK_STYLE_VALUES: Final[tuple[str, ...]] = ("default", "eyecare", "eyecare2")


class GrantStatus(str, Enum):
    """下发时长（TimeGrant）状态机：granted → credited / revoked / expired。

    与 ExtensionStatus 的部分取值（credited / expired）字面相同但语义独立，
    故单列枚举，避免与延时申请状态混淆，并让拼写错误在比较/赋值时即暴露。
    """

    GRANTED = "granted"
    CREDITED = "credited"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CommandStatus(str, Enum):
    """远程指令生命周期（D49）。"""

    PENDING = "pending"
    DELIVERED = "delivered"
    ACKED = "acked"
    EXPIRED = "expired"


class EventSeverity(str, Enum):
    """事件级别。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventType(str, Enum):
    """事件类型，共 **31** 个。

    S4：含 TOKEN_REUSE_DETECTED；V4 追加 4 个模板事件；
    V5 追加 6 个（设备停用/恢复/删除 + 账号新建/修改/删除，增量架构设计 §2.3）；
    V7 追加 USAGE_RESET / TIME_GRANTED / USAGE_DEDUCTED。
    """

    STATE_CHANGED = "STATE_CHANGED"
    DAILY_USAGE = "DAILY_USAGE"
    RULE_UPDATED = "RULE_UPDATED"
    PARENT_MODE_ENTER = "PARENT_MODE_ENTER"
    PARENT_MODE_EXIT = "PARENT_MODE_EXIT"
    PASSWORD_FAILED = "PASSWORD_FAILED"
    EMERGENCY_GRACE = "EMERGENCY_GRACE"
    RECOVERY_CODE_USED = "RECOVERY_CODE_USED"
    TIME_TAMPER = "TIME_TAMPER"
    ABNORMAL_EXIT = "ABNORMAL_EXIT"
    SYNC_FAILED = "SYNC_FAILED"
    EXTENSION_SUBMITTED = "EXTENSION_SUBMITTED"
    EXTENSION_CREDITED = "EXTENSION_CREDITED"
    COMMAND_EXECUTED = "COMMAND_EXECUTED"
    DEVICE_PAIRED = "DEVICE_PAIRED"
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
    TOKEN_REUSE_DETECTED = "TOKEN_REUSE_DETECTED"
    RULE_TEMPLATE_CREATED = "RULE_TEMPLATE_CREATED"
    RULE_TEMPLATE_UPDATED = "RULE_TEMPLATE_UPDATED"
    RULE_TEMPLATE_DELETED = "RULE_TEMPLATE_DELETED"
    RULE_TEMPLATE_APPLIED = "RULE_TEMPLATE_APPLIED"

    # --- V5 增量（§2.3）---
    DEVICE_RETIRED = "DEVICE_RETIRED"
    """设备停用成功。device_id=设备 id，payload={name, retired_at}。"""

    DEVICE_UNRETIRED = "DEVICE_UNRETIRED"
    """设备恢复为在管。device_id=设备 id，payload={name, credential_revoked}。"""

    DEVICE_DELETED = "DEVICE_DELETED"
    """设备硬删除。

    ⚠️ 该事件的 `device_id` **必须为 NULL** —— `event_logs.device_id` 是
    ON DELETE CASCADE，若写上被删设备的 id，紧随其后的 DELETE devices 会把这条
    审计留痕一并级联删除。设备身份信息全部进 payload 快照。
    """

    USER_CREATED = "USER_CREATED"
    """新建账号。device_id=NULL，payload={user_id, username, role}。"""

    USER_UPDATED = "USER_UPDATED"
    """改角色 / 启停 / 重置密码。device_id=NULL，payload={user_id, username, changed}。"""

    USER_DELETED = "USER_DELETED"
    """删除账号。device_id=NULL，user_id=**执行者**（不是被删者），payload=被删账号快照。"""

    # --- V7（§2.2 / §3.3）：重置当日用量（ack 门控清零）+ 下发时长 ---
    USAGE_RESET = "USAGE_RESET"
    """当日用量被重置（清零 used/parent/break，可选清 bonus）。

    device_id=目标设备，payload={target_date, include_bonus, scope, command_id}。
    触发于客户端 ack ``RESET_USAGE`` 指令（status=success）后（方案 A：ack 门控清零）。
    """

    TIME_GRANTED = "TIME_GRANTED"
    """管理员向设备下发当日额外时长（time_grants）。

    device_id=目标设备，payload={grant_id, minutes, target_date, reason}。
    额度经既有 credits 下行链路入账，复用 ``/client/confirm-credit`` 路径。
    """

    USAGE_DEDUCTED = "USAGE_DEDUCTED"
    """管理员扣减设备当日用量（``used_minutes += applied``）。

    device_id=目标设备，payload={applied_minutes, requested_minutes, target_date,
    reason, command_id}。触发于 ``deduction_service.deduct()`` 服务端立即生效时
    （方案：服务端立即生效 + 客户端冗余指令，镜像 ``USAGE_RESET`` 的服务端立即清零）。
    """


class KpiId(str, Enum):
    """仪表盘 KPI 卡片 id。"""

    TODAY_USAGE = "today_usage"
    REMAINING = "remaining"
    ONLINE_DEVICES = "online_devices"
    PENDING_APPROVALS = "pending_approvals"
    ABNORMAL_EVENTS = "abnormal_events"


class CredentialRevokeReason(str, Enum):
    """设备凭证撤销原因。"""

    REPAIR = "repair"
    MANUAL_REVOKE = "manual_revoke"
    RETIRE = "retire"


# --- 在线状态阈值（D08） ---
ONLINE_THRESHOLD_SECONDS: Final[int] = 120
STALE_THRESHOLD_SECONDS: Final[int] = 900

# --- 规则默认值（D13/D14/D26/D46） ---
# 2026-09-05 按用户裁决统一：工作日/周末均 40 分钟、连续 30→休息 10、
# 提醒 (10,5,1)、家长模式超时 15（空闲判定 5、时段 08:00-21:00 不变）。
DEFAULT_WEEKDAY_QUOTA_MINUTES: Final[int] = 40
DEFAULT_WEEKEND_QUOTA_MINUTES: Final[int] = 40
DEFAULT_ALLOWED_START: Final[str] = "08:00"
DEFAULT_ALLOWED_END: Final[str] = "21:00"
DEFAULT_CONTINUOUS_LIMIT_MINUTES: Final[int] = 30
DEFAULT_BREAK_MINUTES: Final[int] = 10
DEFAULT_IDLE_MINUTES: Final[int] = 5
DEFAULT_REMINDER_POINTS: Final[tuple[int, ...]] = (10, 5, 1)
DEFAULT_PARENT_MODE_TIMEOUT_MINUTES: Final[int] = 15
DEFAULT_SYNC_INTERVAL_SECONDS: Final[int] = 45
DEFAULT_REMINDER_POINTS_JSON: Final[str] = "[10, 5, 1]"

# --- 其他业务常量 ---
DEFAULT_DEVICE_NAME: Final[str] = "新设备"
DEFAULT_TIMEZONE: Final[str] = "Asia/Shanghai"
MAX_PENDING_EXTENSIONS_PER_DAY: Final[int] = 3
"""同一设备同一 target_date 最多 3 条 pending（D30）。"""

# --- V7 下发时长（§3.3 / §5 R4 频控） ---
MAX_GRANTS_PER_DEVICE_PER_DAY: Final[int] = 5
"""同一设备同一 target_date 最多下发 5 条 time_grants（频控，对齐 MAX_PENDING_EXTENSIONS_PER_DAY）。"""
GRANT_MINUTES_MIN: Final[int] = 1
GRANT_MINUTES_MAX: Final[int] = 240
"""单条时长上下限（与延时申请一致）。"""

# --- V7 admin-only 指令白名单（§5 R2） ---
ADMIN_ONLY_COMMAND_TYPES: Final[frozenset[str]] = frozenset(
    {
        CommandType.RESET_USAGE.value,
        CommandType.SET_LOCK_STYLE.value,
        CommandType.DEDUCT_USAGE.value,
    }
)
"""家长端 ``POST /commands`` 绝不能下发的指令类型；仅 ``require_admin`` 端点可构造。

v1.4.2 起新增 ``SET_LOCK_STYLE``（锁屏外观三样式），与 ``RESET_USAGE`` 同约束：仅经
内部服务层（``lock_style_service.set_lock_style``）由 ``require_admin`` 端点下发。
V7 功能三新增 ``DEDUCT_USAGE``（扣减用量），同样仅经内部服务层
（``deduction_service.deduct``）由 ``require_admin`` 端点（``POST /usage/deduct``）下发。
"""

UNLOCK_TEMP_MIN_MINUTES: Final[int] = 5
UNLOCK_TEMP_MAX_MINUTES: Final[int] = 120
UNLOCK_TEMP_DEFAULT_MINUTES: Final[int] = 15

DEFAULT_DASHBOARD_LAYOUT_JSON: Final[str] = (
    '["today_usage", "remaining", "online_devices", "pending_approvals", "abnormal_events"]'
)

# --- /client/sync 上行条数上限（API.md §7.2.1） ---
SYNC_MAX_USAGE_ITEMS: Final[int] = 7
SYNC_MAX_EVENT_ITEMS: Final[int] = 200
SYNC_MAX_EXTENSION_ITEMS: Final[int] = 20
SYNC_MAX_COMMAND_ACK_ITEMS: Final[int] = 50
SYNC_MAX_CREDIT_CONFIRM_ITEMS: Final[int] = 50

# 用量查询最长区间
MAX_USAGE_RANGE_DAYS: Final[int] = 366

# --- 重置用量（V7 功能一，§5.2 R2）---
# 仅管理员可通过内部服务层下发的指令类型集合。命中且调用方非 admin → 403。
RESET_SCOPES: Final[tuple[str, ...]] = ("device", "global")
"""重置用量范围：设备 / 全局。本版不含 user（架构 §2.4）。"""

# --- 下发时长（V7 功能二，§3.3 / §5.4 R4） ---

MAX_GRANT_TOTAL_MINUTES_PER_DAY: Final[int] = 480
"""单设备单日下发总额上限（R4：防「5 次 × 240」无限堆额度）。"""

# --- V7 功能三：扣减用量（§2.2，已采纳「最小客户端改动」方案） ---
DEDUCT_MINUTES_MAX: Final[int] = 240
"""单条扣减用量上下限（与 GRANT_MINUTES_MAX 对齐，1-240）。"""

# --- PC 客户端版本更新（MVP 方案 A） ---
CHANNEL_STABLE: Final[str] = "stable"
"""发布通道。MVP 只有 stable 一个通道；未来加 beta 需改 client_versions 的 CHECK。"""

CLIENT_CHANNELS: Final[tuple[str, ...]] = (CHANNEL_STABLE,)
"""全部合法发布通道，与 client_versions.channel 的 CHECK 约束严格一致。"""
