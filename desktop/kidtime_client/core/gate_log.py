"""门禁本地日志（1.2 增量 · Q-A12，用户已批准完整版）。

记录五件事（架构 §C.5，格式细节以架构师最终设计为准；当前按「数据目录下
JSONL 文件 + 按大小轮转」实现）：

① 每次门禁弹出时间戳
② 选的入口（家长 / 孩子）
③ 家长验证失败次数 / 时间 / 方式（密码还是恢复码，**只记类型不记内容**）
④ 触发锁定（5 次 → 60 秒冷却 / 10 次 → 15 分钟应急放行）
⑤ 5 分钟无操作是否出现关机按钮

🔴 红线：
* **只写本地文件，不联网**；零第三方依赖、零 Qt 依赖。
* **不改 schema.sql** —— 这是文件日志，不是数据库表。
* **绝不记录密码 / 恢复码的明文或哈希**。每条记录里只有事件类型与次数，
  任何「秘密内容」字段都不允许存在。

轮转策略：主文件 ``gate.log`` 超过 ``MAX_BYTES``（默认 1 MiB）时，
当前文件滚动为 ``gate.1.log``（丢弃更旧的），重新打开主文件续写。
进程内按需打开/关闭，避免长期持有句柄。

🔴 落盘位置对齐架构 §C.5：调用方必须传 ``config.log_dir``
（即 ``default_data_dir()/logs``），与客户端日志同目录，**不要在 app 层
自行拼接路径**。目录不存在时由本模块 ``mkdir(parents=True, exist_ok=True)``
负责创建。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: 主日志文件超过该字节数后轮转。
MAX_BYTES: Final[int] = 1_048_576  # 1 MiB


class GateLog:
    """把门禁事件以 JSONL 形式追加到数据目录。

    Args:
        log_dir: 日志文件所在目录（生产环境传 ``config.log_dir``，即
            ``default_data_dir()/logs``）。
        max_bytes: 轮转阈值（字节）；测试注入小值验证轮转。
    """

    def __init__(self, log_dir: Path, max_bytes: int = MAX_BYTES) -> None:
        self._log_dir = Path(log_dir)
        self._max_bytes = max(1, int(max_bytes))
        self._main = self._log_dir / "gate.log"
        self._rotated = self._log_dir / "gate.1.log"
        self._fh = None

    # ------------------------------------------------------------------
    # 事件 API（全部 main-thread 调用；每条一行 JSON）
    # ------------------------------------------------------------------
    def log_gate_open(self, screen_count: int) -> None:
        """① 门禁弹出。

        Args:
            screen_count: 当时覆盖的屏幕数。
        """
        self._write({"event": "gate_open", "screens": int(screen_count)})

    def log_entrance(self, entrance: str) -> None:
        """② 选择的入口。

        Args:
            entrance: ``"parent"`` 或 ``"child"``。
        """
        self._write({"event": "entrance", "entrance": str(entrance)})

    def log_verify_failed(self, method: str) -> None:
        """③ 家长验证失败。

        Args:
            method: ``"password"`` 或 ``"recovery_code"``——**只记类型**。
                绝不记录尝试的文本内容。
        """
        self._write({"event": "verify_failed", "method": str(method)})

    def log_lockout(self, kind: str, threshold: int) -> None:
        """④ 触发锁定/放行。

        Args:
            kind: ``"cooldown"``（5 次 → 60 秒冷却）或 ``"emergency_grace"``
                （10 次 → 15 分钟应急放行）。
            threshold: 触发阈值（5 或 10）。
        """
        self._write({"event": "lockout", "kind": str(kind), "threshold": int(threshold)})

    def log_shutdown_visible(self, idle_seconds: float) -> None:
        """⑤ 5 分钟无操作后出现关机按钮。

        Args:
            idle_seconds: 触发时的空闲秒数（诊断用）。
        """
        self._write({"event": "shutdown_visible", "idle_seconds": float(idle_seconds)})

    # ------------------------------------------------------------------
    # 读取与清理（测试 / 运维）
    # ------------------------------------------------------------------
    def read_lines(self) -> list[dict]:
        """读取当前主文件里的全部记录（测试与排障用）。

        Returns:
            已解析的 JSON 对象列表（按写入顺序）。
        """
        if not self._main.exists():
            return []
        records: list[dict] = []
        for line in self._main.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:  # pragma: no cover - 半截行（崩溃残留）
                logger.debug("忽略 gate_log 半截行：%r", line[:80])
        return records

    def close(self) -> None:
        """关闭文件句柄（应用退出时调用）。"""
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:  # pragma: no cover - 防御
                pass
            self._fh = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _write(self, payload: dict) -> None:
        """追加一行 JSON；必要时先轮转。"""
        try:
            if self._fh is None:
                self._log_dir.mkdir(parents=True, exist_ok=True)
                self._fh = self._main.open("a", encoding="utf-8")
            if self._fh.tell() >= self._max_bytes:
                self._rotate()
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
        except OSError:
            # 日志失败绝不能影响门禁本身（写不进日志 ≠ 门禁失灵）。
            logger.warning("写入门禁日志失败", exc_info=True)

    def _rotate(self) -> None:
        """把主文件滚动为 ``gate.1.log``，重新打开主文件。"""
        try:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            if self._rotated.exists():
                self._rotated.unlink()
            if self._main.exists():
                self._main.replace(self._rotated)
        except OSError:
            logger.warning("轮转门禁日志失败", exc_info=True)
        finally:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._fh = self._main.open("a", encoding="utf-8")


__all__ = ["MAX_BYTES", "GateLog"]
