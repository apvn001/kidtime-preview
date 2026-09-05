"""客户端本地存储层（SQLite）。

🔴 Single-Writer：本包所有写操作只允许在 GUI 主线程调用（ARCHITECTURE.md §9.4 R1）。
"""

from __future__ import annotations

from kidtime_client.storage.database import LocalDatabase, UnsafeDatabasePathError

__all__ = ["LocalDatabase", "UnsafeDatabasePathError"]
