"""额度入账确认统一分发（V7 §3.3.3）：在 `ExtensionRequest` 与 `TimeGrant` 两张表间路由。

`/client/sync` 第 8 步与 `/client/confirm-credit` 端点都调用本服务，按 id 先在
`extension_requests` 查、未命中再查 `time_grants`，命中即置 `credited` 并写审计。
客户端零改动即可复用既有 `credited_ledger` 精确一次去重语义。
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy.orm import Session

from app.core.constants import EventType, ExtensionStatus, GrantStatus
from app.db.base import utcnow
from app.models.extension import ExtensionRequest
from app.models.grant import TimeGrant
from app.services import event_service


def confirm(
    db: Session,
    device_id: str,
    request_ids: Sequence[str],
    *,
    now: datetime | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """确认一组额度已在客户端入账（重复确认幂等成功）。

    路由顺序：先 `ExtensionRequest` 后 `TimeGrant`。两表 id 空间不同（UUIDv4），
    不会冲突。

    Returns:
        ``(confirmed, already_credited, not_found)``。
    """
    moment = now or utcnow()
    confirmed: list[str] = []
    already: list[str] = []
    missing: list[str] = []

    for request_id in request_ids:
        ext = db.get(ExtensionRequest, request_id)
        if ext is not None and ext.device_id == device_id:
            if ext.status == ExtensionStatus.CREDITED.value:
                already.append(request_id)
                continue
            if ext.status != ExtensionStatus.APPROVED.value:
                missing.append(request_id)
                continue
            ext.status = ExtensionStatus.CREDITED.value
            ext.credited_at = moment
            confirmed.append(request_id)
            event_service.record(
                db,
                EventType.EXTENSION_CREDITED,
                device_id=device_id,
                payload={
                    "request_id": request_id,
                    "approved_minutes": ext.approved_minutes,
                    "target_date": ext.target_date.isoformat(),
                },
                now=moment,
            )
            continue

        grant = db.get(TimeGrant, request_id)
        if grant is not None and grant.device_id == device_id:
            if grant.status == GrantStatus.CREDITED.value:
                already.append(request_id)
                continue
            if grant.status != GrantStatus.GRANTED.value:
                missing.append(request_id)
                continue
            grant.status = GrantStatus.CREDITED.value
            grant.credited_at = moment
            confirmed.append(request_id)
            event_service.record(
                db,
                EventType.TIME_GRANTED,
                device_id=device_id,
                payload={
                    "phase": "credited",
                    "grant_id": request_id,
                    "minutes": grant.minutes,
                    "target_date": grant.target_date.isoformat(),
                },
                now=moment,
            )
            continue

        missing.append(request_id)

    db.flush()
    return confirmed, already, missing
