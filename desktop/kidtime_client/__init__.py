"""KidTime Windows 桌面客户端包。

🔴 三条不可动摇的架构底线（ARCHITECTURE.md §0.2 / §9.4）：

1. **本地 SQLite 绝不落在同步盘**：默认 ``%LOCALAPPDATA%\\KidTime\\client``，
   启动时由 :func:`kidtime_client.storage.database.LocalDatabase.assert_db_path_safe`
   强制自检。
2. **Single-Writer 线程模型**：所有 SQLite 写操作只在 GUI 主线程执行；
   ``SyncWorker`` 只做 HTTP，绝不碰数据库。
3. **额度入账幂等**：``credited_ledger`` 先行 + ``BEGIN IMMEDIATE``，
   崩溃恢复只重发确认，绝不重复加时。

版本号唯一权威来源是 ``kidtime_client/constants.py`` 的 ``CLIENT_VERSION``；
本文件的 ``__version__`` 直接引用它（便携单机版 = ``1.0.0``）。
"""

from __future__ import annotations

from kidtime_client.constants import CLIENT_VERSION

__all__ = ["__version__"]

__version__ = CLIENT_VERSION
