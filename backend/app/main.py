"""FastAPI 应用工厂与生命周期。

中间件注册顺序（外 → 内）：`CORSMiddleware` → `RateLimitMiddleware`
→ `RequestContextMiddleware` → `IdempotencyMiddleware`。Starlette 的
`add_middleware` 是头插语义，因此**最后添加的最外层**，故按
Idempotency → RequestContext → RateLimit → CORS 的顺序添加。

把限流放在 `RequestContextMiddleware` 之外是刻意的：被限流的请求**不需要**
读取并缓存请求体，直接在最外层短路返回 429，CPU 与内存开销最低——这正是
P1-1 降级版要达到的目的（保护 2 vCPU 不被 bcrypt 打满）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import __version__
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.db.init_db import ensure_data_dir, sync_admin_from_env
from app.db.session import assert_db_path_safe, get_engine
from app.middleware.idempotency import IdempotencyMiddleware, cleanup_idempotency_keys
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 3600

HEALTH_PROBE_SQL = text("SELECT COUNT(*) FROM devices")
"""P0-9 健康探针的业务判据。

只查进程存活（"200 就算好"）会漏掉最常见的故障形态——**服务在跑但同步全失败**。
因此探针必须真的读一次库：``COUNT(*) FROM devices`` 同时验证了
「DB 文件可读」+「schema 已迁移」+「连接池可用」三件事。
"""

DB_REVISION_SQL = text("SELECT version_num FROM alembic_version")
"""V5 §7.9：健康探针回显当前数据库迁移版本。

升级编排脚本靠它判断 ``alembic upgrade head`` 是否真的落到了运行中的进程上
（而不是升了另一个库文件）。测试库用 ``create_all`` 建表、没有
``alembic_version`` 表，此时回退为 ``unknown``，**不影响健康判定**。
"""

UNKNOWN_REVISION = "unknown"


def _read_db_revision(conn: Any) -> str:
    """读取 `alembic_version.version_num`，读不到时回退为 ``unknown``。

    Args:
        conn: 已建立的数据库连接。

    Returns:
        当前迁移版本号；表不存在或为空时返回 ``unknown``。
    """
    try:
        row = conn.execute(DB_REVISION_SQL).first()
    except Exception:  # noqa: BLE001 - 缺表不算故障，仅降级为 unknown
        with contextlib.suppress(Exception):
            conn.rollback()
        return UNKNOWN_REVISION
    if row is None or not row[0]:
        return UNKNOWN_REVISION
    return str(row[0])


async def _idempotency_cleanup_loop() -> None:
    """后台任务：每小时清理一次过期幂等记录（D47 / S12）。"""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            removed = await asyncio.to_thread(cleanup_idempotency_keys)
            if removed:
                logger.info("已清理过期幂等记录 %d 条", removed)
        except asyncio.CancelledError:  # pragma: no cover - 关闭时正常取消
            raise
        except Exception:  # pragma: no cover - 后台任务不得因异常退出
            logger.exception("清理幂等记录失败")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动：路径自检 + 建目录 + 立即清理一次；关闭：取消后台任务。"""
    settings = get_settings()
    assert_db_path_safe(settings.DATABASE_URL)
    ensure_data_dir()
    # 🔴 S2（开源整改）：内嵌单机形态注入 ``KIDTIME_SEED_ADMIN_*`` 环境变量时，
    # 把库里内置 admin 口令同步为运行期随机口令（存在即更新、缺失即创建，
    # 旧库自动收敛）。普通部署无该环境变量，此调用恒为空操作。
    try:
        sync_admin_from_env()
    except Exception:  # pragma: no cover - 种子失败必须让启动失败，避免错口令继续服务
        logger.exception("同步内置 admin 运行期口令失败")
        raise
    try:
        removed = cleanup_idempotency_keys()
        if removed:
            logger.info("启动清理过期幂等记录 %d 条", removed)
    except Exception:  # pragma: no cover - 首启表可能尚未创建
        logger.warning("启动清理幂等记录被跳过（数据库可能尚未迁移）")

    task = asyncio.create_task(_idempotency_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    """构造 FastAPI 应用。"""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.LOG_DIR)
    # 🔴 启动即自检数据库路径，命中同步盘时直接拒绝启动（S1）
    assert_db_path_safe(settings.DATABASE_URL)

    # 🔴 P0-6 公网暴露面收口：非调试模式下三个文档端点全部 404。
    # 37 个端点的说明书挂在公网上，与租户数、实例数无关，一样会被扫到。
    # 本地开发在 backend/.env 里置 EXPOSE_API_DOCS=1 即可恢复。
    expose_docs = settings.EXPOSE_API_DOCS
    if not expose_docs:
        logger.info("已关闭 /docs、/redoc、/openapi.json（EXPOSE_API_DOCS=0）")

    app = FastAPI(
        title="KidTime API",
        description="家庭 Windows 电脑使用时间管理系统 —— 管理端与客户端统一后端。",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/openapi.json" if expose_docs else None,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
    )

    # 头插语义：最后添加的最外层
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(RequestContextMiddleware)

    # P1-1 降级版：进程内轻量限流（单 worker 前提下无需 Redis，见 D1.2）
    if settings.RATE_LIMIT_ENABLED:
        app.add_middleware(RateLimitMiddleware, rules=settings.rate_limit_rules)
    else:
        logger.info("进程内限流已关闭（RATE_LIMIT_ENABLED=0）")

    # D1.3 / G16：同源部署后 CORS 收敛为空集合 —— 为空则**完全不注册**中间件，
    # 连带消除 allow_credentials=True 的风险面。
    cors_origins = settings.cors_origin_list
    if cors_origins:
        logger.info("已启用 CORS，允许来源：%s", "、".join(cors_origins))
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id", "X-Server-Time", "Idempotency-Replayed"],
        )
    else:
        logger.info("CORS_ORIGINS 为空，未注册 CORSMiddleware（同源部署）")

    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/health", tags=["运维"], summary="健康检查")
    def health(response: Response) -> dict[str, str]:
        """P0-9 健康探针：进程存活 **且** 数据库可读才算健康。

        无鉴权、无敏感信息（只回显版本号与迁移版本号）。数据库探测失败时
        返回 503，供巡检脚本指标 2 与 Caddy 上游健康判断使用。

        V5 §7.9：额外回显 `db_revision`，升级脚本据此确认新 schema 已生效。
        """
        try:
            with get_engine().connect() as conn:
                conn.execute(HEALTH_PROBE_SQL).scalar_one()
                revision = _read_db_revision(conn)
        except Exception:  # noqa: BLE001 - 探针必须吞掉全部异常并降级
            logger.exception("健康探针数据库读取失败")
            response.status_code = 503
            return {
                "status": "degraded",
                "version": __version__,
                "db_revision": UNKNOWN_REVISION,
                "database": "error",
            }
        return {
            "status": "ok",
            "version": __version__,
            "db_revision": revision,
            "database": "ok",
        }

    return app


app = create_app()
