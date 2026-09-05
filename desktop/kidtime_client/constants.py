"""客户端全局枚举与默认常量。

字符串取值必须与后端 ``app/core/constants.py`` 完全一致，任何改名都会
打断 ``/client/sync`` 契约（API.md §7.2）。
"""

from __future__ import annotations

from enum import Enum
from typing import Final

APP_NAME: Final[str] = "KidTime"
APP_DISPLAY_NAME: Final[str] = "KidTime"
#: 客户端产品版本（**唯一权威来源**）。便携单机版以 ``1.0.0`` 起版，
#: 随心跳写入内嵌后端 ``devices.client_version``、随 ``--version`` 与启动日志
#: 展示。全项目引用统一走本常量（``__init__.__version__``、config/engine/api_client
#: 的 client_version），日后改版只动这一处。
CLIENT_VERSION: Final[str] = "1.0.0"
DEFAULT_TIMEZONE: Final[str] = "Asia/Shanghai"


class EventType(str, Enum):
    """事件类型（与后端 17 个枚举一一对应）。"""

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


class EventSeverity(str, Enum):
    """事件级别。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CommandType(str, Enum):
    """远程指令类型。

    🔴 新增枚举值必须五处副本完全一致（v1.4.2 技术方案 §1.1）：
    ``backend/app/core/constants.py`` / ``backend/app/models/command.py`` 的
    ``type_valid`` CHECK / ``alembic`` 迁移 / 本文件 /
    ``frontend/src/types/api.ts``。漏任一处即被一致性守卫测试拦下。
    """

    UNLOCK_TEMP = "UNLOCK_TEMP"
    PAUSE_ENFORCEMENT = "PAUSE_ENFORCEMENT"
    RESUME_ENFORCEMENT = "RESUME_ENFORCEMENT"
    SYNC_NOW = "SYNC_NOW"
    RESET_USAGE = "RESET_USAGE"
    #: v1.4.2 锁屏外观（admin-only，镜像 RESET_USAGE 的下发与处理链路）。
    SET_LOCK_STYLE = "SET_LOCK_STYLE"
    #: V7 功能三 扣减用量（admin-only，镜像 RESET_USAGE）：服务端已立即生效
    #: （used_minutes += applied），本地执行仅为冗余同步（used_seconds += applied*60）。
    DEDUCT_USAGE = "DEDUCT_USAGE"


class OutboxKind(str, Enum):
    """本地发件箱条目类型（ARCHITECTURE.md §4 `outbox.kind`）。"""

    EVENT = "event"
    EXTENSION_REQUEST = "extension_request"
    COMMAND_ACK = "command_ack"
    CREDIT_CONFIRM = "credit_confirm"


class OutboxStatus(str, Enum):
    """发件箱条目状态。"""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


# --- 规则默认值（PRD D13/D14/D26/D46） ---
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

# --- 上行条数上限（API.md §7.2.1，超出会被服务端 422 拒绝） ---
SYNC_MAX_USAGE_ITEMS: Final[int] = 7
SYNC_MAX_EVENT_ITEMS: Final[int] = 200
SYNC_MAX_EXTENSION_ITEMS: Final[int] = 20
SYNC_MAX_COMMAND_ACK_ITEMS: Final[int] = 50
SYNC_MAX_CREDIT_CONFIRM_ITEMS: Final[int] = 50

# --- 延时申请 ---
MAX_PENDING_EXTENSIONS_PER_DAY: Final[int] = 3
EXTENSION_MIN_MINUTES: Final[int] = 1
EXTENSION_MAX_MINUTES: Final[int] = 240

# --- UNLOCK_TEMP 指令参数范围（与后端 constants.py 一致，D19） ---
UNLOCK_TEMP_MIN_MINUTES: Final[int] = 5
UNLOCK_TEMP_MAX_MINUTES: Final[int] = 120
UNLOCK_TEMP_DEFAULT_MINUTES: Final[int] = 15

# --- 本地设置键名（local_settings 表） ---
SETTING_BASE_URL: Final[str] = "base_url"
SETTING_DEVICE_NAME: Final[str] = "device_name"
SETTING_TIMEZONE: Final[str] = "timezone"
SETTING_RULES_JSON: Final[str] = "rules_json"
SETTING_RULE_VERSION: Final[str] = "rule_version"
SETTING_PARENT_PASSWORD: Final[str] = "parent_password_hash"
SETTING_RECOVERY_CODE: Final[str] = "recovery_code_hash"
SETTING_SETUP_DONE: Final[str] = "setup_completed"
SETTING_AUTOSTART: Final[str] = "autostart_enabled"
SETTING_FLOATING_VISIBLE: Final[str] = "floating_widget_visible"
SETTING_FLOATING_POS: Final[str] = "floating_widget_pos"
SETTING_UNLOCK_UNTIL: Final[str] = "unlock_temp_until"
SETTING_ENFORCEMENT_PAUSED: Final[str] = "enforcement_paused"
SETTING_LAST_DAILY_EVENT_DATE: Final[str] = "last_daily_usage_event_date"

#: 🔴 1.2 重配对崩溃补偿标志（架构 §C.4.3）：步骤 ③ 之前写入新服务器地址，
#: 步骤 ⑦ 分界事件完成后删除。启动时若该值等于当前 ``SETTING_BASE_URL``，
#: 说明上次重绑崩在凭据切换与封存之间，需要补跑一次「封存 + 分界事件」。
SETTING_REBIND_IN_PROGRESS: Final[str] = "rebind_in_progress"

# --- 1.4.2 锁屏外观三样式（PRD FR-1 / FR-9） --------------------------------
#: 当前锁屏外观（``"default"|"eyecare"|"eyecare2"``）。
SETTING_LOCK_STYLE: Final[str] = "lock_style"
#: 是否允许孩子单点浮窗自主轮换锁屏外观（bool，默认 ``False``）。
SETTING_LOCK_ALLOW_CHILD_SWITCH: Final[str] = "lock_allow_child_switch"

#: 默认样式：蓝黑品牌渐变（沿用 1.2 的 ``COLOR_BRAND_GRADIENT_*``）。
LOCK_STYLE_DEFAULT: Final[str] = "default"
#: 护眼样式：米黄 + 淡绿浅色低蓝光。
LOCK_STYLE_EYECARE: Final[str] = "eyecare"
#: 护眼样式 2：暖橙米色低蓝光。
LOCK_STYLE_EYECARE2: Final[str] = "eyecare2"

#: 🔴 孩子单点浮窗的轮换顺序（FR-4：默认 → 护眼 → 护眼2 → 默认 …）。
#: 同时充当白名单：不在这个元组里的值一律回退成 ``LOCK_STYLE_DEFAULT``。
#: 取值字符串与后端 ``SET_LOCK_STYLE`` payload 的 ``style`` 字段完全一致。
LOCK_STYLE_ORDER: Final[tuple[str, ...]] = (
    LOCK_STYLE_DEFAULT,
    LOCK_STYLE_EYECARE,
    LOCK_STYLE_EYECARE2,
)


def normalize_lock_style(style: object) -> str:
    """把任意输入夹取成合法的锁屏样式标识。

    Args:
        style: 待校验的取值（可能来自服务端 payload 或本地设置表）。

    Returns:
        ``LOCK_STYLE_ORDER`` 中的一个；无法识别时返回 ``LOCK_STYLE_DEFAULT``。
    """
    text = str(style or "").strip()
    if text in LOCK_STYLE_ORDER:
        return text
    return LOCK_STYLE_DEFAULT


# --- 1.2 启动门禁（双入口锁屏）常量 -----------------------------------------
#: 门禁窗口的心跳周期：每秒问一次 ``IdleSource.idle_seconds()``。
#: 与引擎的 1 秒 tick 保持同一节奏，便于家长在日志里对齐时间轴。
GATE_TICK_INTERVAL_MS: Final[int] = 1000

#: 门禁窗口的多屏重建防抖窗口，与 ``OverlayManager.REBUILD_DEBOUNCE_MS`` 对齐。
GATE_REBUILD_DEBOUNCE_MS: Final[int] = 300

# --- 1.4.3 锁屏自动关机（需求 2b） ------------------------------------------
#: 锁屏（启动门禁 / 日常锁屏，强制休息 BREAK 除外）**单次连续**停留满该分钟数
#: 即进入关机预告。家长待机态（PARENT_STANDBY）与强制休息期间不计入累计。
LOCK_AUTO_SHUTDOWN_MINUTES: Final[int] = 15

#: 达到阈值后的关机预告秒数；预告期内可点「取消关机」重新累计（1.4.3 需求）。
SHUTDOWN_GRACE_SECONDS: Final[int] = 30

# --- 1.4.3 倒计时提醒弹窗（需求 1） ------------------------------------------
#: 15/5/1 分钟提醒弹窗的最长展示秒数；倒计时归零后自动关闭。
#: 用户也可点击「知道了」立即关闭。
REMINDER_POPUP_AUTO_CLOSE_SECONDS: Final[int] = 5

# --- 客户端版本更新（MVP 方案 A） -------------------------------------------
#: 唯一发布通道；与后端 ``app/core/constants.py`` 的 ``CHANNEL_STABLE`` 一致。
#: MVP 不做 beta/内测通道：多通道会立刻带来「回退到 stable」的降级语义问题，
#: 而降级安装在换名方案下需要额外的兼容性判定，收益不足。
CHANNEL_STABLE: Final[str] = "stable"

#: 自动检查更新的轮询周期（小时）。8 小时 ≈ 一天三次，足够让强制更新在一天内
#: 覆盖绝大多数在线设备，又不会把后端接口打成心跳。手动检查不受此值限制。
UPDATE_POLL_HOURS: Final[int] = 8

#: 客户端首次检查更新的延迟（毫秒）。启动瞬间要让同步、门禁、托盘先各就各位，
#: 更新检查是最低优先级的后台动作，往后错开 20 秒。
UPDATE_FIRST_CHECK_DELAY_MS: Final[int] = 20_000

#: 安装根目录下的本地版本文件名（与 ``update.version_store`` 保持一致）。
VERSION_JSON_FILENAME: Final[str] = "version.json"
