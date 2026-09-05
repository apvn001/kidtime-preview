"""锁屏外观下发服务（v1.4.2 §2 / §3.5）：建 SET_LOCK_STYLE 指令。

🔴 关键语义：本服务仅在服务端**创建一条 `SET_LOCK_STYLE` 待下发指令**，无服务端
即时副作用——锁屏外观是纯客户端渲染态，由客户端在下次 `/client/sync` 拉取后本地应用
（写 `local_settings` 的 `SETTING_LOCK_STYLE` / `SETTING_LOCK_ALLOW_CHILD_SWITCH`）。
与 V7 的 `reset_service` 不同（RESET_USAGE 服务端立即清零用量），此处不发任何服务端状态变更。

下发复用既有 `command_service.create_batch`（内部含 `ADMIN_ONLY_COMMAND_TYPES` 越权兜底
+ 逐设备 command_id），本服务仅做入参规范化与类型放行的薄封装（镜像 `reset_service`
的 admin-only 内部下发定位）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.core.constants import CommandType, LOCK_STYLE_VALUES
from app.services import command_service

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User


def set_lock_style(
    db: Session,
    device_id: str,
    *,
    style: str,
    allow_child_switch: bool,
    user: "User",
    now: Any = None,
) -> tuple[str, list[Any], list[Any]]:
    """为单台设备创建一条 `SET_LOCK_STYLE` 待下发指令（admin-only）。

    仅构造指令；客户端下次 `/client/sync`（step10 `deliver_pending`）拉取后本地应用。
    服务端不下发任何即时状态变更（外观纯客户端渲染态）。

    Args:
        db: 数据库会话。
        device_id: 目标设备 id。
        style: ``"default"`` | ``"eyecare"`` | ``"eyecare2"``；非法值回退 ``"default"``。
        allow_child_switch: 是否允许孩子自主点击浮窗轮换样式。
        user: 操作管理员（须为 admin；create_batch 内部再做越权兜底）。
        now: 注入当前时间（G8），缺省取 utcnow。

    Returns:
        ``(batch_id, 已创建指令列表, 失败设备列表)``（与 ``create_batch`` 一致）。

    Raises:
        NotFoundError: 设备不存在（由 ``create_batch`` 抛出，转 404）。
    """
    # 规范化样式：非法值回退 default（与客户端 LOCK_STYLE_ORDER 兜底一致）。
    safe_style = style if style in LOCK_STYLE_VALUES else "default"

    # 复用既有 create_batch：内部含 ADMIN_ONLY_COMMAND_TYPES 越权兜底（非 admin → 403）
    # 与逐设备独立 command_id；设备不存在 → NotFoundError(404)，设备已停用 → 进入 failed。
    return command_service.create_batch(
        db,
        [device_id],
        CommandType.SET_LOCK_STYLE.value,
        {"style": safe_style, "allow_child_switch": bool(allow_child_switch)},
        user,
        now=now,
    )
