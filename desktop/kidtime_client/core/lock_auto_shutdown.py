"""锁屏自动关机控制器（1.4.3 增量 · 需求 2b）。

背景
----
1.4.2 的启动门禁只在无操作 5 分钟后**露出**关机按钮，绝不自动关机（决策 1）。
1.4.3 的需求把它升级成两条：

1. 锁屏上**固定**显示关机按钮（废弃 5 分钟露出逻辑，见 ``startup_gate``）；
2. 锁屏**单次连续**停留满 15 分钟后，进入 30 秒关机预告；预告期内可点
   「取消关机」重新累计，预告归零则自动关机。

本模块只负责「累计 → 预告 → 关机」这一个**纯逻辑**状态机：

* 无 IO、无 Qt、不读全局时间——一切由调用方每秒喂一次 :meth:`tick`。
* 回调（``on_grace_started`` / ``on_grace_tick`` / ``on_grace_cancelled`` /
  ``on_shutdown`` / ``on_counting_tick``）由调用方注入，因此单元测试可以零
  真实等待地演练「满 15 分钟 → 预告 30 秒 → 关机」全链路。
* ``on_counting_tick`` 在 COUNTING 态每秒回调「距自动关机剩余秒数」，用于锁屏
  界面实时展示倒计时（1.4.4 增量）。
* 「家长待机态（PARENT_STANDBY）与强制休息（BREAK）不计入累计」由调用方
  通过 :meth:`set_counting` 表达，本控制器不感知门禁/锁屏的具体形态。

状态机
------
``IDLE``
    未锁屏。``start()`` 进入 ``COUNTING``。
``COUNTING``
    锁屏累计中。每秒 ``tick`` 加 1 秒；满阈值进入 ``GRACE``。
``GRACE``
    关机预告中。每秒 ``tick`` 减 1 秒；归零触发 ``on_shutdown``。
    ``cancel_grace()`` 回到 ``COUNTING`` 且**累计清零**（重新计时）。

🔴 语义红线（与产品确认）：「单次连续」= 一次锁屏内、扣除暂停（家长待机 /
休息）后的**连续**停留时长。解锁（``stop``）、取消预告（``cancel_grace``）
都会把累计清零；家长待机（``set_counting(False)``）只暂停不归零。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Final

from kidtime_client.constants import (
    LOCK_AUTO_SHUTDOWN_MINUTES,
    SHUTDOWN_GRACE_SECONDS,
)

logger = logging.getLogger(__name__)

#: 回调签名：无参回调（on_grace_cancelled / on_shutdown）。
_Callback = Callable[[], None]
#: 回调签名：携带剩余秒数的回调（on_grace_started / on_grace_tick）。
_SecondsCallback = Callable[[int], None]


class AutoShutdownState(str, Enum):
    """自动关机控制器的三态。"""

    IDLE = "IDLE"
    """未锁屏（累计已清零）。"""

    COUNTING = "COUNTING"
    """锁屏停留累计中。"""

    GRACE = "GRACE"
    """关机预告中（等待归零或用户取消）。"""


class AutoShutdownController:
    """锁屏停留累计 + 关机预告的纯逻辑核心。

    Args:
        threshold_minutes: 触发预告所需的连续停留分钟数。
            （默认取 ``constants.LOCK_AUTO_SHUTDOWN_MINUTES``。）
        grace_seconds: 预告时长（秒）。
            （默认取 ``constants.SHUTDOWN_GRACE_SECONDS``。）
        on_grace_started: 进入预告时回调 ``(grace_seconds)``。
        on_grace_tick: 预告期每秒回调 ``(remaining_seconds)``。
        on_grace_cancelled: 预告被取消（用户点「取消关机」或锁屏提前消失）。
        on_shutdown: 预告归零，应当执行关机。
        on_counting_tick: COUNTING 态每秒回调「距自动关机剩余秒数」
            （含预告时长，连续单调），供锁屏界面展示倒计时。
    """

    def __init__(
        self,
        threshold_minutes: int = LOCK_AUTO_SHUTDOWN_MINUTES,
        grace_seconds: int = SHUTDOWN_GRACE_SECONDS,
        on_grace_started: _SecondsCallback | None = None,
        on_grace_tick: _SecondsCallback | None = None,
        on_grace_cancelled: _Callback | None = None,
        on_shutdown: _Callback | None = None,
        on_counting_tick: _SecondsCallback | None = None,
    ) -> None:
        self._threshold_seconds: Final[int] = max(1, int(threshold_minutes) * 60)
        self._grace_seconds: Final[int] = max(1, int(grace_seconds))
        self._state: AutoShutdownState = AutoShutdownState.IDLE
        self._counting: bool = True
        self._elapsed_seconds: int = 0
        self._grace_left_seconds: int = 0
        self.on_grace_started = on_grace_started
        self.on_grace_tick = on_grace_tick
        self.on_grace_cancelled = on_grace_cancelled
        self.on_shutdown = on_shutdown
        self.on_counting_tick = on_counting_tick

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def state(self) -> AutoShutdownState:
        """当前状态。"""
        return self._state

    @property
    def threshold_seconds(self) -> int:
        """触发预告所需的连续停留秒数。"""
        return self._threshold_seconds

    @property
    def grace_seconds(self) -> int:
        """预告总时长（秒）。"""
        return self._grace_seconds

    @property
    def elapsed_seconds(self) -> int:
        """已累计的连续停留秒数（COUNTING 态有效）。"""
        return self._elapsed_seconds

    @property
    def grace_left_seconds(self) -> int:
        """预告剩余秒数（GRACE 态有效）。"""
        return self._grace_left_seconds

    @property
    def counting(self) -> bool:
        """当前是否在累计（家长待机 / 强制休息时为 ``False``）。"""
        return self._counting

    @property
    def is_active(self) -> bool:
        """是否处于「锁屏在岗」状态（COUNTING 或 GRACE）。"""
        return self._state is not AutoShutdownState.IDLE

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """锁屏显示：清零并从新开始累计。

        幂等：已在累计中（COUNTING/GRACE）时再次调用不重置累计，
        避免锁屏刷新（热插拔重建、样式切换）误把计时打回零。
        """
        if self._state is not AutoShutdownState.IDLE:
            return
        self._state = AutoShutdownState.COUNTING
        self._counting = True
        self._elapsed_seconds = 0
        self._grace_left_seconds = 0
        logger.info("锁屏自动关机：开始累计（阈值 %d 秒）", self._threshold_seconds)

    def stop(self) -> None:
        """锁屏隐藏：清零并回到 IDLE。

        若正处于预告期（锁屏提前消失，例如孩子被解锁、门禁关闭），
        一并触发 ``on_grace_cancelled`` 让调用方收起预告窗口。
        """
        if self._state is AutoShutdownState.IDLE:
            return
        was_grace = self._state is AutoShutdownState.GRACE
        self._state = AutoShutdownState.IDLE
        self._counting = True
        self._elapsed_seconds = 0
        self._grace_left_seconds = 0
        if was_grace:
            logger.info("锁屏自动关机：预告期锁屏消失，取消预告")
            self._fire(self.on_grace_cancelled)
        logger.info("锁屏自动关机：停止累计（回到 IDLE）")

    def set_counting(self, active: bool) -> None:
        """暂停 / 恢复累计（不归零）。

        家长待机态（PARENT_STANDBY）与强制休息（BREAK）期间由调用方
        传 ``False``：这段时间**不计入**「单次连续」停留时长，但已累计的
        秒数保留，恢复后继续。

        Args:
            active: ``False`` 暂停累计，``True`` 恢复累计。
        """
        self._counting = bool(active)

    def cancel_grace(self) -> None:
        """用户点「取消关机」：收起预告并**重新**开始累计。

        产品语义：预告只是提醒，不是判决。取消一次之后从 0 重新计满
        15 分钟才会再次预告，避免「取消后 1 秒又弹」的骚扰式循环。

        Returns:
            ``True`` 表示确实处于预告期并已取消；否则为 ``False``（no-op）。
        """
        if self._state is not AutoShutdownState.GRACE:
            return False
        self._state = AutoShutdownState.COUNTING
        self._counting = True
        self._elapsed_seconds = 0
        self._grace_left_seconds = 0
        logger.info("锁屏自动关机：用户取消预告，重新累计")
        self._fire(self.on_grace_cancelled)
        return True

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    def tick(self) -> None:
        """推进 1 秒。

        由锁屏管理器（``GateManager`` / ``OverlayManager``）持有的 1 秒
        心跳驱动；GRACE 态下同样依赖心跳把预告倒数到零。

        COUNTING 态每秒通过 :attr:`on_counting_tick` 回调「距自动关机剩余
        秒数」（含预告时长，连续单调），供锁屏界面实时展示倒计时。
        """
        if self._state is AutoShutdownState.COUNTING:
            if not self._counting:
                return  # 暂停期：这一秒不计入
            self._elapsed_seconds += 1
            if self._elapsed_seconds >= self._threshold_seconds:
                self._enter_grace()
            else:
                # 连续倒计时：剩余 = (阈值 - 已累计) + 预告时长，
                # 与 GRACE 期的 grace_left 无缝衔接成一条单调递减曲线。
                remaining = (
                    self._threshold_seconds
                    - self._elapsed_seconds
                    + self._grace_seconds
                )
                self._fire(self.on_counting_tick, remaining)
        elif self._state is AutoShutdownState.GRACE:
            self._grace_left_seconds -= 1
            if self._grace_left_seconds <= 0:
                self._fire_shutdown()
            else:
                self._fire(self.on_grace_tick, self._grace_left_seconds)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _enter_grace(self) -> None:
        """累计满阈值：进入预告期。"""
        self._state = AutoShutdownState.GRACE
        self._grace_left_seconds = self._grace_seconds
        logger.info(
            "锁屏自动关机：连续停留满 %d 秒，进入 %d 秒预告",
            self._threshold_seconds,
            self._grace_seconds,
        )
        self._fire(self.on_grace_started, self._grace_seconds)

    def _fire_shutdown(self) -> None:
        """预告归零：触发关机回调并回 IDLE。"""
        self._state = AutoShutdownState.IDLE
        self._elapsed_seconds = 0
        self._grace_left_seconds = 0
        logger.warning("锁屏自动关机：预告归零，执行关机")
        self._fire(self.on_shutdown)

    @staticmethod
    def _fire(callback: _Callback | _SecondsCallback | None, seconds: int | None = None) -> None:
        """安全地触发一个回调（异常绝不能让锁屏心跳崩掉）。"""
        if callback is None:
            return
        try:
            if seconds is None:
                callback()  # type: ignore[call-arg]
            else:
                callback(seconds)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover - 防御：回调失败不影响状态机
            logger.exception("锁屏自动关机回调执行失败")


__all__ = [
    "AutoShutdownController",
    "AutoShutdownState",
]
