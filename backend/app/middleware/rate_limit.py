"""进程内轻量限流中间件（P1-1 降级版 / 决策 D1.2）。

**为什么可以不用 Redis**：MVP 锁定 ``uvicorn --workers 1``（决策 D1.1），
单进程内的内存计数器天然一致，不存在多实例计数分裂问题。这在多实例下行不通，
但在 MVP 下是最简解——约 100 行、零新增依赖（只用标准库）。

**覆盖范围**：只保护三个真正会打爆 2 vCPU 的关键路径，其余路径一律放行。

============  ==========================================================
路径          放大器
============  ==========================================================
``/auth/login``   bcrypt cost 12 ≈ 250 ms/次，是最典型的 CPU DoS 放大器
``/client/pair``  bcrypt cost 10 + 配对码校验
``/client/sync``  写事务 + bcrypt 设备凭证校验，且是唯一的高频路径
============  ==========================================================

**⚠️ 已知局限（技术债 T-07 / G15，本批次不做 XFF 透传）**：
Caddy 与后端同机，反代过来的 ``scope["client"]`` 恒为 ``127.0.0.1``，
因此本中间件实际上是**全局窗口**而非"按真实客户端 IP"。这对"保护 CPU 不被打满"
的目标依然有效（全局上限就是 CPU 的保护伞），但无法只封禁单个恶意 IP。
引入 SLB 或需要按真实 IP 风控时，请回链 P0-9 的 XFF 透传部分。
阈值设定已充分考虑该局限：``/client/sync`` 的默认 60 次/60 秒，相对
3 台设备 × 45 秒同步周期（合计约 4 次/分钟）留了 15 倍余量。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Final, MutableMapping

from app.core.errors import ErrorCode, error_payload

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

DEFAULT_LIMIT: Final[str] = "60/60"
"""解析失败时的兜底表达式：60 次 / 60 秒。"""

MAX_TRACKED_KEYS: Final[int] = 1024
"""内存保护：最多跟踪多少个 (路径, 客户端) 组合，超出后清空最旧的一半。"""


class RateLimitRule:
    """一条限流规则：窗口 ``window_seconds`` 秒内最多 ``max_requests`` 次。

    Attributes:
        max_requests: 窗口内允许的最大请求数。
        window_seconds: 滑动窗口长度（秒）。
    """

    __slots__ = ("max_requests", "window_seconds")

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(1, int(window_seconds))

    @classmethod
    def parse(cls, expression: str) -> "RateLimitRule":
        """把 ``"10/60"`` 这样的表达式解析成规则对象。

        Args:
            expression: 形如 ``次数/窗口秒数`` 的字符串。

        Returns:
            解析出的规则；格式非法时回退到 :data:`DEFAULT_LIMIT` 并打一条警告。
        """
        text = (expression or "").strip()
        try:
            raw_count, raw_window = text.split("/", 1)
            return cls(int(raw_count.strip()), int(raw_window.strip()))
        except (ValueError, AttributeError):
            logger.warning("限流表达式 “%s” 非法，已回退为 %s", expression, DEFAULT_LIMIT)
            count, window = DEFAULT_LIMIT.split("/")
            return cls(int(count), int(window))

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"RateLimitRule({self.max_requests}/{self.window_seconds}s)"


class SlidingWindowCounter:
    """按 key 记录请求时间戳的滑动窗口计数器（进程内、线程安全）。"""

    def __init__(self) -> None:
        self._hits: dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, rule: RateLimitRule, now: float | None = None) -> int:
        """尝试记录一次请求。

        Args:
            key: 计数分组键（``路径|客户端标识``）。
            rule: 该路径适用的限流规则。
            now: 当前单调时间；``None`` 时取 :func:`time.monotonic`。

        Returns:
            ``0`` 表示放行；正整数表示被限流，值为建议的 ``Retry-After`` 秒数。
        """
        moment = time.monotonic() if now is None else now
        deadline = moment - rule.window_seconds
        with self._lock:
            if len(self._hits) > MAX_TRACKED_KEYS:
                self._evict_locked()
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] <= deadline:
                bucket.popleft()
            if len(bucket) >= rule.max_requests:
                # 最早一次请求滑出窗口即可再次放行，向上取整保证不会提前重试。
                wait = rule.window_seconds - (moment - bucket[0])
                return max(1, int(wait) + 1)
            bucket.append(moment)
            return 0

    def reset(self) -> None:
        """清空全部计数（测试与手工解封用）。"""
        with self._lock:
            self._hits.clear()

    def _evict_locked(self) -> None:
        """内存保护：key 数量超限时丢弃空桶，仍超限则整体清空。

        必须在持有 ``self._lock`` 的前提下调用。
        """
        empty = [key for key, bucket in self._hits.items() if not bucket]
        for key in empty:
            del self._hits[key]
        if len(self._hits) > MAX_TRACKED_KEYS:
            logger.warning("限流计数器 key 数量超过 %d，已整体重置", MAX_TRACKED_KEYS)
            self._hits.clear()


def client_key(scope: Scope) -> str:
    """从 ASGI scope 提取客户端标识。

    优先取 TCP 对端地址；同机反代场景下恒为 ``127.0.0.1``（见模块 docstring 的
    局限说明）。取不到时统一归入 ``unknown`` 桶。
    """
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return "unknown"


class RateLimitMiddleware:
    """纯 ASGI 限流中间件：命中阈值返回 429 + ``Retry-After``。

    与项目其余中间件保持一致，实现为纯 ASGI 而非 ``BaseHTTPMiddleware``，
    避免额外的任务包装开销。
    """

    def __init__(
        self,
        app: Any,
        *,
        rules: dict[str, str] | None = None,
        counter: SlidingWindowCounter | None = None,
    ) -> None:
        """构造中间件。

        Args:
            app: 下游 ASGI 应用。
            rules: ``{路径: "次数/窗口秒数"}`` 映射；``None`` 表示不限流。
            counter: 复用外部计数器（测试用）；``None`` 时新建。
        """
        self.app = app
        self.rules: dict[str, RateLimitRule] = {
            path: RateLimitRule.parse(expr) for path, expr in (rules or {}).items()
        }
        self.counter = counter or SlidingWindowCounter()
        if self.rules:
            logger.info(
                "已启用进程内限流：%s",
                "、".join(f"{path} {rule!r}" for path, rule in self.rules.items()),
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI 入口。"""
        if scope.get("type") != "http" or not self.rules:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        rule = self.rules.get(path.rstrip("/") or "/")
        if rule is None:
            await self.app(scope, receive, send)
            return

        retry_after = self.counter.check(f"{path}|{client_key(scope)}", rule)
        if retry_after == 0:
            await self.app(scope, receive, send)
            return

        logger.warning(
            "限流拦截 %s（上限 %d 次/%d 秒），建议 %d 秒后重试",
            path,
            rule.max_requests,
            rule.window_seconds,
            retry_after,
        )
        await _send_429(send, retry_after)


async def _send_429(send: Send, retry_after: int) -> None:
    """直接下发统一格式的 429 响应体（不经过 FastAPI 异常处理器）。"""
    import json

    payload = error_payload(
        ErrorCode.RATE_LIMITED,
        "请求过于频繁，请稍后重试",
        {"retry_after_seconds": retry_after},
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", str(retry_after).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
