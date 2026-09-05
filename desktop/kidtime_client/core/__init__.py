"""客户端核心业务逻辑。

除 :mod:`kidtime_client.core.engine` 之外，本包**不依赖 Qt、不做 IO、不读全局时间**，
全部时间来源经 :class:`kidtime_client.core.clock.Clock` 注入，因此可被
`FakeClock` 完全驱动，测试零真实等待（ARCHITECTURE.md §9.6）。
"""

from __future__ import annotations

__all__: list[str] = []
