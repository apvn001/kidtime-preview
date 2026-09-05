"""🔴 8 状态优先级状态机（PRD §5.1 / ARCHITECTURE.md §5.8）。

**纯逻辑**：无 IO、无 Qt、不读全局时间。全部输入经 :class:`TickInput` 注入，
因此可被 `FakeClock` 完全驱动，测试零真实等待。

优先级自上而下，**首个命中即生效**，顺序不可调换::

    DISABLED → GRACE → PARENT → LOCKED_CURFEW → LOCKED_QUOTA
             → BREAK → IDLE_PAUSED → ACTIVE

关键语义：

* 宵禁**优先于**配额（LOCKED_CURFEW 在 LOCKED_QUOTA 之前）。
* PARENT 与 BREAK **不扣**孩子配额。
* GRACE **允许使用且正常扣配额**（应急放行不是免费时间）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Final, FrozenSet

from kidtime_client.constants import EventSeverity, EventType
from kidtime_client.core.rules import (
    RuleSnapshot,
    base_quota_for,
    is_within_allowed_window,
    next_allowed_datetime,
)


class State(str, Enum):
    """客户端 8 状态（数字注释即优先级）。"""

    DISABLED = "DISABLED"  # 1
    GRACE = "GRACE"  # 2
    PARENT = "PARENT"  # 3
    LOCKED_CURFEW = "LOCKED_CURFEW"  # 4
    LOCKED_QUOTA = "LOCKED_QUOTA"  # 5
    BREAK = "BREAK"  # 6
    IDLE_PAUSED = "IDLE_PAUSED"  # 7
    ACTIVE = "ACTIVE"  # 8


COUNTS_CHILD_TIME: Final[FrozenSet[State]] = frozenset({State.ACTIVE, State.GRACE})
"""扣孩子配额的状态。"""

COUNTS_CONTINUOUS: Final[FrozenSet[State]] = frozenset({State.ACTIVE, State.GRACE})
"""累加连续使用时长的状态。"""

COUNTS_PARENT_TIME: Final[FrozenSet[State]] = frozenset({State.PARENT})
"""计入 `parent_minutes` 的状态。"""

SHOWS_OVERLAY: Final[FrozenSet[State]] = frozenset(
    {State.LOCKED_CURFEW, State.LOCKED_QUOTA, State.BREAK}
)
"""需要展示全屏遮罩的状态。"""

OVERLAY_CURFEW: Final[str] = "CURFEW"
OVERLAY_QUOTA: Final[str] = "QUOTA"
OVERLAY_BREAK: Final[str] = "BREAK"
OVERLAY_TAMPER: Final[str] = "TAMPER"

_OVERLAY_REASON_BY_STATE: Final[dict[State, str]] = {
    State.LOCKED_CURFEW: OVERLAY_CURFEW,
    State.LOCKED_QUOTA: OVERLAY_QUOTA,
    State.BREAK: OVERLAY_BREAK,
}

_REASON_TEXT: Final[dict[State, str]] = {
    State.DISABLED: "管控已暂停",
    State.GRACE: "临时放行中",
    State.PARENT: "家长模式",
    State.LOCKED_CURFEW: "当前不在允许使用时段",
    State.LOCKED_QUOTA: "今日时间已用完",
    State.BREAK: "强制休息中",
    State.IDLE_PAUSED: "检测到长时间无操作，已暂停计时",
    State.ACTIVE: "正常使用中",
}


@dataclass(frozen=True)
class TickInput:
    """一次 tick 的全部输入。

    Attributes:
        now_utc: 当前 UTC 时间。
        now_local: 当前本地时间（tz-aware）。
        effective_date: 由 `TimeGuard` 给出的业务日期（不回退）。
        monotonic_delta: 已钳制的计时增量（秒）。
        rules: 规则快照。
        used_seconds: 当日孩子已用秒数。
        bonus_minutes: 当日额外额度分钟。
        grace_until: 放行截止 UTC 时间；``None`` 表示无放行。
        parent_mode_active: 家长模式是否激活。
        break_end_at: 强制休息结束 UTC 时间；``None`` 表示不在休息中。
        continuous_use_seconds: 已累计的连续使用秒数。
        idle_seconds: 键鼠空闲秒数。
        session_locked: Windows 会话是否锁定。
        fired_reminders: 当日已触发过的提醒节点集合。
    """

    now_utc: datetime
    now_local: datetime
    effective_date: date
    monotonic_delta: float
    rules: RuleSnapshot
    used_seconds: float
    bonus_minutes: int
    grace_until: datetime | None
    parent_mode_active: bool
    break_end_at: datetime | None
    continuous_use_seconds: float
    idle_seconds: float
    session_locked: bool
    fired_reminders: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class PendingEvent:
    """状态机产出的待落库事件。"""

    event_type: str
    severity: str
    payload: dict


@dataclass(frozen=True)
class TickOutput:
    """一次 tick 的全部产出。调用方据此更新持久化状态与 UI。"""

    state: State
    previous_state: State
    changed: bool
    reason: str
    child_delta_seconds: float
    parent_delta_seconds: float
    continuous_use_seconds: float
    break_end_at: datetime | None
    break_started: bool
    break_finished: bool
    effective_quota_minutes: int
    remaining_minutes: float
    reminders_fired: tuple[int, ...]
    overlay_visible: bool
    overlay_reason: str | None
    overlay_countdown_seconds: int | None
    next_available_local: datetime | None
    events: tuple[PendingEvent, ...]


class StateMachine:
    """纯逻辑状态机。

    Attributes:
        _state: 当前状态。
    """

    def __init__(self, initial_state: State = State.ACTIVE) -> None:
        """初始化。

        Args:
            initial_state: 初始状态，默认 `ACTIVE`。
        """
        self._state: State = initial_state

    @property
    def state(self) -> State:
        """当前状态。"""
        return self._state

    def reset_for_new_day(self) -> None:
        """日切复位（D17）：连续使用由调用方清零，本方法无内部状态需重置。"""
        pass

    # ------------------------------------------------------------ 主流程
    def tick(self, inp: TickInput) -> TickOutput:
        """执行一次状态判定与计时结算。

        内部严格按 8 步执行，顺序不可调换（ARCHITECTURE.md §5.8）。

        Args:
            inp: 本次 tick 的输入。

        Returns:
            本次 tick 的产出。
        """
        previous = self._state
        rules = inp.rules

        # --- 1. 判定状态（8 级优先级，首个命中即生效） ---
        base_quota = base_quota_for(rules, inp.effective_date)
        effective_quota = base_quota + max(0, inp.bonus_minutes)
        used_minutes_float = inp.used_seconds / 60.0
        remaining = max(0.0, effective_quota - used_minutes_float)

        state = self._decide(inp, remaining)

        # --- 2. 计时增量归属 ---
        delta = max(0.0, inp.monotonic_delta)
        child_delta = delta if state in COUNTS_CHILD_TIME else 0.0
        parent_delta = delta if state in COUNTS_PARENT_TIME else 0.0

        # --- 3. 连续使用累加 / 冻结 ---
        continuous = float(inp.continuous_use_seconds)
        break_end_at = inp.break_end_at
        break_finished = False

        # 1.4.5：连续使用（45 分钟强制休息周期）仅在 ACTIVE/GRACE 累加；其余
        # 所有状态（IDLE_PAUSED / PARENT / LOCKED_CURFEW / LOCKED_QUOTA / BREAK）
        # 一律冻结——不累加、不清零。该计时器是自"上次强制休息结束（或会话开始）"
        # 以来的真实屏幕时长，不受门禁 / 锁屏 / 空闲 / 家长态影响，因此"耗光额度
        # → 申请延时"等循环也无法绕过强制休息（每次恢复都接着算，而非从 0 重来）。
        if state in COUNTS_CONTINUOUS:
            continuous += delta

        # D24 ①：强制休息自然结束 → 连续使用清零（周期内唯一清零点）。
        # 1.4.5 起配额耗尽锁屏不再清零（改为冻结），与上方设计一致。
        if previous == State.BREAK and state != State.BREAK:
            if break_end_at is not None and inp.now_utc >= break_end_at:
                break_finished = True
            continuous = 0.0
            break_end_at = None

        # --- 4. 连续使用触顶 → 立刻改判为 BREAK ---
        break_started = False
        limit_seconds = max(1, rules.continuous_limit_minutes) * 60
        if state == State.ACTIVE and continuous >= limit_seconds:
            break_end_at = inp.now_utc + timedelta(minutes=rules.break_minutes)
            break_started = True
            state = State.BREAK
            # 改判后本 tick 不再计入孩子时间
            child_delta = 0.0

        # --- 5. 配额结算（用最新的 child_delta 修正剩余量展示） ---
        used_after = inp.used_seconds + child_delta
        remaining_after = max(0.0, effective_quota - used_after / 60.0)

        # --- 6. 提醒判定 ---
        reminders: list[int] = []
        if state in COUNTS_CHILD_TIME:
            for point in sorted(rules.reminder_points, reverse=True):
                if point in inp.fired_reminders or point in reminders:
                    continue
                if remaining_after <= point:
                    reminders.append(point)

        # --- 7. 遮罩输出 ---
        overlay_visible = state in SHOWS_OVERLAY
        overlay_reason = _OVERLAY_REASON_BY_STATE.get(state)
        countdown: int | None = None
        next_available: datetime | None = None
        if state == State.BREAK and break_end_at is not None:
            countdown = max(0, int((break_end_at - inp.now_utc).total_seconds()))
        if state == State.LOCKED_CURFEW:
            next_available = next_allowed_datetime(rules, inp.now_local)
        elif state == State.LOCKED_QUOTA:
            next_available = _next_midnight(inp.now_local)

        # --- 8. 汇总事件 ---
        changed = state != previous
        reason = _REASON_TEXT.get(state, state.value)
        events: list[PendingEvent] = []
        if changed:
            events.append(
                PendingEvent(
                    event_type=EventType.STATE_CHANGED.value,
                    severity=EventSeverity.INFO.value,
                    payload={
                        "from": previous.value,
                        "to": state.value,
                        "reason": reason,
                        "remaining_minutes": round(remaining_after, 2),
                    },
                )
            )

        self._state = state
        return TickOutput(
            state=state,
            previous_state=previous,
            changed=changed,
            reason=reason,
            child_delta_seconds=child_delta,
            parent_delta_seconds=parent_delta,
            continuous_use_seconds=continuous,
            break_end_at=break_end_at,
            break_started=break_started,
            break_finished=break_finished,
            effective_quota_minutes=effective_quota,
            remaining_minutes=remaining_after,
            reminders_fired=tuple(reminders),
            overlay_visible=overlay_visible,
            overlay_reason=overlay_reason,
            overlay_countdown_seconds=countdown,
            next_available_local=next_available,
            events=tuple(events),
        )

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _decide(inp: TickInput, remaining_minutes: float) -> State:
        """按 PRD §5.1 的 8 级优先级判定状态。

        Args:
            inp: tick 输入。
            remaining_minutes: 本 tick 开始时的剩余分钟。

        Returns:
            命中的状态。
        """
        # 1. 管控关闭
        if not inp.rules.enforcement_enabled:
            return State.DISABLED
        # 2. 临时放行（应急宽限 / 远程 UNLOCK_TEMP）
        if inp.grace_until is not None and inp.now_utc < inp.grace_until:
            return State.GRACE
        # 3. 家长模式
        if inp.parent_mode_active:
            return State.PARENT
        # 4. 宵禁（优先于配额）
        if not is_within_allowed_window(inp.rules, inp.now_local.time()):
            return State.LOCKED_CURFEW
        # 5. 配额耗尽
        if remaining_minutes <= 0:
            return State.LOCKED_QUOTA
        # 6. 强制休息
        if inp.break_end_at is not None and inp.now_utc < inp.break_end_at:
            return State.BREAK
        # 7. 空闲 / 会话锁定
        if inp.session_locked or inp.idle_seconds >= inp.rules.idle_minutes * 60:
            return State.IDLE_PAUSED
        # 8. 正常使用
        return State.ACTIVE


def _next_midnight(local_now: datetime) -> datetime:
    """返回本地下一个零点（配额耗尽时的"明天再来"提示）。"""
    tomorrow = local_now.date() + timedelta(days=1)
    return datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0, tzinfo=local_now.tzinfo
    )
