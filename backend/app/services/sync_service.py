"""🔴 系统心脏：`/client/sync` 全流程编排（API.md §7.2.3 十二步，顺序不可调换）。

上行（3–8：心跳 → 用量 → 事件 → 申请 → 指令回执 → 入账确认）
**必须全部先于**下行（9–11：规则下发 → 指令下发 → 额度下发）。
否则同一轮同步会把刚 ACK 的指令再次下发、把刚确认入账的额度重复发放。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.constants import ExtensionStatus
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.device import Device
from app.models.extension import ExtensionRequest
from app.schemas.client_sync import (
    AcceptedCounts,
    CreditItem,
    DeviceSyncInfo,
    ExtensionResultItem,
    SyncRequest,
    SyncResponse,
)
from app.services import (
    command_service,
    device_service,
    event_service,
    extension_service,
    grant_service,
    rule_service,
    usage_service,
)

logger = logging.getLogger(__name__)


def _result_item(row: ExtensionRequest) -> ExtensionResultItem:
    """把申请行转成下行状态回执项。"""
    reject_reason = (
        extension_service.REJECT_REASON_PARENT
        if row.status == ExtensionStatus.REJECTED.value
        else None
    )
    return ExtensionResultItem(
        id=row.id,
        status=row.status,  # type: ignore[arg-type]
        approved_minutes=row.approved_minutes,
        decided_at=row.decided_at,
        reject_reason=reject_reason,
    )


@retry_on_busy
def sync(
    db: Session, device: Device, req: SyncRequest, *, now: datetime | None = None
) -> SyncResponse:
    """执行一次完整同步（单事务内，严格按 12 步顺序）。

    Args:
        db: 数据库会话。
        device: 已通过设备鉴权的设备（第 1 步已完成）。
        req: 同步请求体。
        now: 注入的当前时间（G8）。

    Returns:
        `SyncResponse`。
    """
    moment = now or utcnow()

    # ---------------- 上行 ----------------
    # 3. 更新设备心跳（覆盖写，天然幂等）
    device_service.touch_heartbeat(
        db,
        device,
        client_version=req.client_version,
        timezone_name=req.timezone,
        os_info=req.os_info,
        state=req.state,
        state_changed_at=req.state_changed_at,
        now=moment,
    )

    # 4. 用量 Upsert：ON CONFLICT DO UPDATE SET x = MAX(旧, 新)（D33）
    accepted_usage = 0
    for item in req.usage:
        usage_service.upsert_daily_usage(db, device.id, item, now=moment)
        accepted_usage += 1

    # 5. 事件入库：ON CONFLICT(client_event_id) DO NOTHING（A05）
    accepted_events = event_service.bulk_insert(db, device.id, req.events, now=moment)

    # 6. 申请入库：PK 去重 + 每日 3 条 pending 上限（D30）
    accepted_extensions = 0
    limit_rejected: dict[str, str] = {}
    for upload in req.extension_requests:
        _row, created, reject_reason = extension_service.submit(
            db,
            device,
            upload.id,
            upload.target_date,
            upload.requested_minutes,
            upload.reason,
            upload.client_created_at,
            now=moment,
        )
        if created:
            accepted_extensions += 1
        if reject_reason:
            limit_rejected[upload.id] = reject_reason

    # 7. 指令回执：仅 pending/delivered 可 ack
    acked_ids, _ignored_ids = command_service.ack(
        db, device.id, req.command_acks, now=moment
    )

    # 8. 额度确认：approved → credited（D31）
    confirmed_ids, _already_ids, _missing_ids = extension_service.confirm_credit(
        db, device.id, req.credit_confirms, now=moment
    )

    # 惰性过期扫描：target_date 已过的 pending 申请置 expired（D27）
    today = usage_service.device_today(device, moment)
    extension_service.expire_stale(db, device.id, today, now=moment)

    # ---------------- 下行 ----------------
    rule = rule_service.get_rule(db, device.id)
    if rule is None:
        rule = rule_service.create_default_rule(db, device.id, now=moment)

    # 9. 规则下发：version 不同才下发完整快照（S7）
    rules_out = rule_service.to_out(rule) if rule.version != req.rule_version else None

    # 10. 指令下发：先批量过期，再取出并置 delivered（D49）
    commands = command_service.deliver_pending(db, device.id, now=moment)

    # 11. 额度下发：approved 且 approved_minutes>0 且 target_date >= 设备今日-1（S13）
    credits = [
        CreditItem(
            request_id=row.id,
            target_date=row.target_date,
            approved_minutes=int(row.approved_minutes or 0),
            requested_minutes=row.requested_minutes,
            decided_at=row.decided_at or moment,
        )
        for row in extension_service.list_deliverable_credits(db, device.id, today)
    ]
    # V7：合并 time_grants 当日下行额度（同一 `credits` 数组，复用客户端入账路径）
    for grant in grant_service.list_deliverable(db, device.id, today):
        credits.append(
            CreditItem(
                request_id=grant.id,
                target_date=grant.target_date,
                approved_minutes=int(grant.minutes),
                requested_minutes=int(grant.minutes),
                decided_at=grant.granted_at,
            )
        )

    # 12. 组装响应
    results: dict[str, ExtensionResultItem] = {}
    for row in extension_service.list_recent_for_device(db, device.id, today):
        results[row.id] = _result_item(row)
    for upload in req.extension_requests:
        if upload.id in limit_rejected:
            results[upload.id] = ExtensionResultItem(
                id=upload.id,
                status="rejected",
                approved_minutes=None,
                decided_at=None,
                reject_reason=limit_rejected[upload.id],
            )
            continue
        if upload.id not in results:
            row = db.get(ExtensionRequest, upload.id)
            if row is not None:
                results[upload.id] = _result_item(row)

    return SyncResponse(
        server_time=moment,
        device=DeviceSyncInfo(
            id=device.id,
            name=device.name,
            status=device.status,  # type: ignore[arg-type]
            timezone=device.timezone,
        ),
        rules=rules_out,
        commands=commands,
        credits=credits,
        extension_results=list(results.values()),
        accepted=AcceptedCounts(
            usage=accepted_usage,
            events=accepted_events,
            extension_requests=accepted_extensions,
            command_acks=len(acked_ids),
            credit_confirms=len(confirmed_ids),
        ),
        next_sync_seconds=rule.sync_interval_seconds,
    )
