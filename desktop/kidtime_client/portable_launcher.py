"""便携单机版（KidTime 体验版）进程入口。

独立项目形态：内嵌后端随主流程无条件启动（见 ``kidtime_client.__main__``），
本模块仅作为 PyInstaller 冻结包的入口，转发给 ``main()``。
"""
from __future__ import annotations

from kidtime_client.__main__ import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
