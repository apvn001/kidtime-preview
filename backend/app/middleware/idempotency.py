"""🔴 幂等中间件（ARCHITECTURE.md §7.3 / D47 / A19）。

规则：
  * 全部 `POST/PUT/PATCH/DELETE` 写请求必须携带 `Idempotency-Key`（UUIDv4）
  * 豁免 `/api/v1/auth/login` 与 `/api/v1/auth/refresh`（S2）——
    refresh 是一次性轮换语义，重放会返回**已失效**的 token，导致登录死循环
  * 占位 INSERT → 并发同 key 返回 409 → 2xx 完成写库 / 非 2xx 删占位允许重试
  * 同 key 不同 body → 422 `IDEMPOTENCY_KEY_REUSED`
  * 重放响应带 `Idempotency-Replayed: true`
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable, Final

from sqlalchemy import delete, or_
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.errors import ErrorCode, json_error
from app.core.security import decode_access_token, is_uuid, sha256_hex
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.idempotency import IdempotencyKey

logger = logging.getLogger(__name__)

_JSON_MEDIA_TYPE: Final[str] = "application/json"

class IdempotencyMiddleware(BaseHTTPMiddleware):
    """写请求幂等拦截与重放。"""

    WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
        {"/api/v1/auth/login", "/api/v1/auth/refresh"}
    )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """幂等主流程。"""
        path = request.url.path
        if (
            request.method not in self.WRITE_METHODS
            or not path.startswith("/api/v1")
            or path in self.EXEMPT_PATHS
        ):
            return await call_next(request)

        key = request.headers.get("Idempotency-Key")
        if not key:
            return json_error(
                400, ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "缺少 Idempotency-Key 请求头"
            )
        if not is_uuid(key):
            return json_error(
                400, ErrorCode.IDEMPOTENCY_KEY_INVALID, "Idempotency-Key 必须是 UUID"
            )

        body = self._raw_body(request)
        req_hash = self._request_hash(request.method, path, request.url.query, body)
        actor = self._actor(request)

        # 🔴 数据库操作必须离开事件循环（P0 死锁修复，S1）：
        # 路由侧 `get_db()` 的 commit 挂在请求 exit stack 上，若此处同步写库会
        # 阻塞事件循环 → 下游 teardown 排不上调度 → SQLite 写锁不释放 → 互等至
        # busy_timeout 超时。`run_in_threadpool` 让出事件循环并在线程池中执行，
        # 其本身就是一次让下游 teardown 得到调度的机会。
        early = await run_in_threadpool(
            self._acquire_or_replay,
            key,
            req_hash,
            f"{request.method} {path}",
            actor,
        )
        if early is not None:
            return early

        try:
            response = await call_next(request)
            resp_body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        except Exception:
            await run_in_threadpool(self._drop_placeholder, key)
            raise

        await run_in_threadpool(self._finalize, key, response, resp_body)

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=resp_body,
            status_code=response.status_code,
            media_type=response.media_type,
            headers=headers,
        )

    @staticmethod
    def _acquire_or_replay(
        key: str,
        req_hash: str,
        endpoint: str,
        actor: str,
    ) -> Response | None:
        """查重放 / 冲突校验 / 插入占位（同步方法，由线程池执行）。

        Returns:
            `None` 表示占位成功、可以放行；否则返回应立即回给客户端的响应。
        """
        with SessionLocal() as db:
            row = db.get(IdempotencyKey, key)
            if row is not None:
                if row.request_hash != req_hash:
                    return json_error(
                        422,
                        ErrorCode.IDEMPOTENCY_KEY_REUSED,
                        "同一 Idempotency-Key 用于了不同的请求",
                    )
                if row.state == "in_progress":
                    return json_error(
                        409,
                        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                        "相同请求正在处理中，请稍后重试",
                    )
                return Response(
                    content=(row.response_body or "").encode("utf-8"),
                    status_code=row.response_status or 200,
                    media_type=_JSON_MEDIA_TYPE,
                    headers={"Idempotency-Replayed": "true"},
                )
            try:
                db.add(
                    IdempotencyKey(
                        key=key,
                        endpoint=endpoint,
                        request_hash=req_hash,
                        actor=actor,
                        state="in_progress",
                        created_at=utcnow(),
                    )
                )
                db.commit()
            except IntegrityError:
                db.rollback()
                return json_error(
                    409, ErrorCode.IDEMPOTENCY_IN_PROGRESS, "相同请求正在处理中，请稍后重试"
                )
        return None

    @staticmethod
    def _finalize(key: str, response: Response, resp_body: bytes) -> None:
        """写完成态 / 非 2xx 删占位（同步方法，由线程池执行）。

        兜底语义：业务事务此时已提交，幂等记账失败不应把成功响应改成 500；
        尽力清除占位行，保证同 key 后续重试不会永久 409。
        """
        try:
            with SessionLocal() as db:
                row = db.get(IdempotencyKey, key)
                if row is not None:
                    if 200 <= response.status_code < 300:
                        row.state = "completed"
                        row.response_status = response.status_code
                        row.response_body = resp_body.decode("utf-8", errors="replace")
                        row.completed_at = utcnow()
                    else:
                        db.delete(row)
                    db.commit()
        except Exception:
            logger.exception("幂等记账失败（key=%s, status=%s），清除占位", key, response.status_code)
            IdempotencyMiddleware._drop_placeholder(key)

    @staticmethod
    def _drop_placeholder(key: str) -> None:
        """异常路径下清除占位记录，允许原 key 重试。"""
        try:
            with SessionLocal() as db:
                row = db.get(IdempotencyKey, key)
                if row is not None and row.state == "in_progress":
                    db.delete(row)
                    db.commit()
        except Exception:  # pragma: no cover - 清理失败不应掩盖原异常
            logger.exception("清理幂等占位记录失败：%s", key)

    @staticmethod
    def _raw_body(request: Request) -> bytes:
        """取出 `RequestContextMiddleware` 缓存的原始请求体。"""
        raw = request.scope.get("state", {}).get("raw_body")
        return raw if isinstance(raw, (bytes, bytearray)) else b""

    @staticmethod
    def _actor(request: Request) -> str:
        """识别请求发起方：`user:{id}` / `device:{id}` / `anon`。"""
        device_id = request.headers.get("X-Device-Id")
        if device_id:
            return f"device:{device_id}"
        authorization = request.headers.get("Authorization")
        if authorization and authorization.lower().startswith("bearer "):
            try:
                payload = decode_access_token(authorization.split(" ", 1)[1].strip())
            except Exception:
                return "anon"
            return f"user:{payload.sub}"
        return "anon"

    @staticmethod
    def _request_hash(method: str, path: str, query: str, body: bytes) -> str:
        """`sha256(method|path|sorted_query|body)`。"""
        sorted_query = "&".join(sorted(query.split("&"))) if query else ""
        return sha256_hex(
            f"{method}|{path}|{sorted_query}|{body.decode('utf-8', errors='replace')}"
        )


def cleanup_idempotency_keys(now: Any = None) -> int:
    """清理过期与残留的幂等记录（S12）。

    Args:
        now: 注入的当前时间（G8）。

    Returns:
        被删除的记录数。
    """
    settings = get_settings()
    moment = now or utcnow()
    ttl_cutoff = moment - timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS)
    stuck_cutoff = moment - timedelta(
        minutes=settings.IDEMPOTENCY_IN_PROGRESS_TIMEOUT_MINUTES
    )
    with SessionLocal() as db:
        stmt = delete(IdempotencyKey).where(
            or_(
                IdempotencyKey.created_at < ttl_cutoff,
                (IdempotencyKey.state == "in_progress")
                & (IdempotencyKey.created_at < stuck_cutoff),
            )
        )
        result = db.execute(stmt)
        db.commit()
        return int(result.rowcount or 0)
