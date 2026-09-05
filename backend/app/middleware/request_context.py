"""请求上下文中间件：request_id、body 缓存、响应头、耗时日志。

实现为**纯 ASGI 中间件**：需要在读取整个请求体之后把 body 回填给下游，
纯 ASGI 形式可以完全掌控 `receive` 通道，行为与 Starlette 版本无关。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, MutableMapping
from uuid import uuid4

from starlette.datastructures import MutableHeaders

from app.core.logging import request_id_var
from app.db.base import utcnow

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RequestContextMiddleware:
    """生成 `request_id`、缓存请求体、写入 `X-Request-Id` / `X-Server-Time`。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI 入口。"""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid4())
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        body = b""
        downstream_receive: Receive = receive
        if scope.get("method", "GET").upper() in BODY_METHODS:
            body = await _read_body(receive)
            downstream_receive = _make_replay_receive(body)

        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["raw_body"] = body

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("X-Request-Id", request_id)
                headers.append(
                    "X-Server-Time", utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                )
            await send(message)

        try:
            await self.app(scope, downstream_receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.debug(
                "%s %s 耗时 %.1fms",
                scope.get("method", "-"),
                scope.get("path", "-"),
                elapsed_ms,
            )
            request_id_var.reset(token)


async def _read_body(receive: Receive) -> bytes:
    """把 `http.request` 的全部分片读成完整 bytes。"""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunk = message.get("body", b"")
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


def _make_replay_receive(body: bytes) -> Receive:
    """返回一个把缓存 body 回放一次的 `receive` 可调用对象。"""
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return replay
