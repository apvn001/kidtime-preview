"""启动门禁状态机（1.2 增量 · Route A · 架构文档 §B · 1.4.3 修订）。

背景
----
1.1 的问题是：客户端一启动就 ``engine.start()``，计时立刻开跑。可现实里开机
之后往往是家长先坐下来处理点事，或者干脆没人用——这段时间白白算在孩子头上。

1.2 的解法是在**编排层**加一道门禁：``app.py`` 把原来的 ``_start()`` 拆成
``_start_services()``（同步/托盘/悬浮立刻起）和 ``_start_child_session()``
（**只有孩子入口被点击后**才 ``engine.start()``）。

🔴 **Route A 的核心约束：状态机零改动。**
    门禁完全活在编排层，:class:`~kidtime_client.core.state_machine.StateMachine`
    的 8 级优先级一行都不碰。``tests/test_state_machine.py`` 必须零改动全绿——
    这是 Route A 正确性的硬证据。如果实现过程中冒出「改一下状态机会更方便」
    的念头，那说明方案跑偏了，应该停下来找架构师，而不是动状态机。

🔴 **本模块禁止 import PySide6。**
    纯逻辑、可脱离 Qt 单测。渲染交给
    :class:`~kidtime_client.ui.gate_window.GateWindow`，多屏编排交给
    :class:`~kidtime_client.ui.gate_manager.GateManager`。
    ``tests/test_startup_gate.py`` 会扫描本文件源码来守这条线。

三个阶段
--------
``PENDING``
    首屏双入口，孩子还没选。**不计时**（引擎压根没 start）。
``PARENT_STANDBY``
    家长入口验证通过，已进入家长模式待机。**仍然不计时**（决策 3：只进家长
    模式，不自动弹面板，背景保持模糊锁屏）。
``CHILD_STARTED``
    孩子入口被点击，``engine.start()`` 已执行，门禁关闭并销毁。

1.4.3 修订：关机按钮固定显示
---------------------------
1.2 的「门禁无操作 5 分钟才露出关机按钮」（决策 1 + ``GATE_SHUTDOWN_IDLE_SECONDS``）
在 1.4.3 被**废弃**：需求 2a 要求锁屏上**固定**显示关机按钮，不再依赖空闲时长。
相应的 idle 读取、阈值判定、sticky 语义一并移除；「锁屏停留满 15 分钟自动关机」
的新语义由 :class:`~kidtime_client.core.lock_auto_shutdown.AutoShutdownController`
在编排层承担（本模块依旧不感知）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class GatePhase(str, Enum):
    """启动门禁的三个阶段。"""

    PENDING = "PENDING"
    """首屏双入口，尚未选择。不计时。"""

    PARENT_STANDBY = "PARENT_STANDBY"
    """家长已验证通过、进入家长模式待机。仍然不计时。"""

    CHILD_STARTED = "CHILD_STARTED"
    """孩子入口已选择，引擎已启动，门禁关闭。"""


@dataclass(frozen=True, slots=True)
class GateContext:
    """门禁窗口渲染所需的全部信息。

    做成 frozen dataclass 是为了让「上下文有没有变」这件事可以用一次 ``!=``
    判断完。

    Attributes:
        phase: 当前阶段。
    """

    phase: GatePhase


class StartupGateController:
    """门禁的纯逻辑核心。

    Args:
        无。1.4.3 起不再读取 idle 时长（废弃「5 分钟露出关机按钮」逻辑），
        关机按钮固定显示；阶段切换全部由显式方法驱动。
    """

    def __init__(self) -> None:
        self._phase: GatePhase = GatePhase.PENDING

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def phase(self) -> GatePhase:
        """当前阶段。"""
        return self._phase

    @property
    def show_shutdown(self) -> bool:
        """关机按钮是否显示。

        1.4.3 起**恒为 ``True``**（固定显示，废弃 1.2 的 5 分钟露出逻辑）。
        保留该属性是为了让 ``GateWindow`` / 既有断言不必感知废弃细节。
        """
        return True

    @property
    def is_open(self) -> bool:
        """门禁是否仍然挡在前面（孩子尚未开始使用）。"""
        return self._phase is not GatePhase.CHILD_STARTED

    @property
    def counts_time(self) -> bool:
        """当前阶段是否在给孩子计时。

        决策 1 的机器可读表述：门禁开着的时候一律不计时。这条属性存在的意义
        是让测试可以直接断言「门禁期不计时」，而不用去翻引擎的内部状态。
        """
        return self._phase is GatePhase.CHILD_STARTED

    @property
    def context(self) -> GateContext:
        """当前渲染上下文快照。"""
        return GateContext(phase=self._phase)

    # ------------------------------------------------------------------
    # 用户选择
    # ------------------------------------------------------------------
    def choose_child(self) -> bool:
        """孩子入口被点击：关闭门禁。

        调用方随后必须执行 ``app._start_child_session()``（即
        ``engine.start()``）——从这一刻起才开始计时。

        Returns:
            ``True`` 表示这次调用真的改变了阶段；重复点击返回 ``False``。
        """
        if self._phase is GatePhase.CHILD_STARTED:
            return False
        previous = self._phase
        self._phase = GatePhase.CHILD_STARTED
        logger.info("门禁：孩子入口被选择（%s -> CHILD_STARTED）", previous.value)
        return True

    def choose_parent(self) -> bool:
        """家长入口验证通过：切到家长待机态。

        🔴 决策 3：这里**只**改变阶段。不自动弹家长面板、不关闭门禁、
        背景继续保持模糊锁屏。要不要打开面板由家长自己再点一次。

        🔴 1.3 主路径已不再进入本方法（家长解锁直达桌面走
        :meth:`unlock_as_parent`）。本方法保留以兼容 1.2 测试与既有行为
        （``tests/test_startup_gate.py`` 零改动全绿）。

        Returns:
            ``True`` 表示阶段发生了变化。已经在待机态或门禁已关闭时返回
            ``False``。
        """
        if self._phase is not GatePhase.PENDING:
            return False
        self._phase = GatePhase.PARENT_STANDBY
        logger.info("门禁：家长验证通过（PENDING -> PARENT_STANDBY）")
        return True

    def unlock_as_parent(self) -> bool:
        """家长解锁直达桌面：PENDING → CHILD_STARTED（门禁关闭，来源为家长）。

        🔴 1.3（PRD P0-1）：家长入口验证通过后**直接**关闭门禁进入桌面，
        不再停在 PARENT_STANDBY 待机。调用方随后必须执行
        ``app._start_child_session()``（即 ``engine.start(keep_parent_mode=True)``），
        状态机在首个 tick 自动落 PARENT 态（不扣孩子时间、不弹锁屏）。

        与 :meth:`choose_child` 同走 ``CHILD_STARTED``（= 门禁已关），这保证
        家长模式退出时 :meth:`back_to_pending` 恒返回 ``False`` = no-op，
        **门禁绝不重弹**。

        Returns:
            ``True`` 表示阶段真的发生了变化；已在 ``CHILD_STARTED`` 或其他
            阶段时返回 ``False``（幂等：多屏重复点击只生效一次）。
        """
        if self._phase is not GatePhase.PENDING:
            return False
        self._phase = GatePhase.CHILD_STARTED
        logger.info("门禁：家长解锁直达桌面（PENDING -> CHILD_STARTED）")
        return True

    def back_to_pending(self) -> bool:
        """家长模式结束（超时/手动退出），退回双入口首屏。

        Returns:
            ``True`` 表示阶段发生了变化。
        """
        if self._phase is not GatePhase.PARENT_STANDBY:
            return False
        self._phase = GatePhase.PENDING
        logger.info("门禁：家长模式结束（PARENT_STANDBY -> PENDING）")
        return True


__all__ = ["GateContext", "GatePhase", "StartupGateController"]
