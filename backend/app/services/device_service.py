"""设备服务：配对码、配对、列表（含在线状态）、改名改时区、停用/恢复、
撤销凭证、硬删除与删除影响预览、设备鉴权。

V5 增量（增量架构设计 §5.1/§5.3）：
    - `delete_device` **只依赖数据库 CASCADE**（决策 D，单一真相源），服务层
      不写任何逐表 DELETE；
    - `DEVICE_DELETED` 事件的 `device_id` **必须是 NULL**，且必须
      「先写事件、后删设备、同一事务」——否则留痕会被 CASCADE 一起删掉；
    - `unretire` 不恢复凭证，响应带 `needs_repair`。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    CommandStatus,
    CredentialRevokeReason,
    DeviceStatus,
    EventSeverity,
    EventType,
    ExtensionStatus,
    ONLINE_THRESHOLD_SECONDS,
    OnlineStatus,
    STALE_THRESHOLD_SECONDS,
)
from app.core.errors import (
    BadRequestError,
    ConflictError,
    ErrorCode,
    ForbiddenError,
    GoneError,
    NotFoundError,
    TooManyRequestsError,
    UnauthorizedError,
)
from app.core.security import (
    device_secret_lookup,
    generate_device_secret,
    generate_pairing_code,
    hash_device_secret,
    new_uuid,
    normalize_pairing_code,
    sha256_hex,
    verify_device_secret,
)
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.command import RemoteCommand
from app.models.device import Device, DeviceCredential, PairingCode
from app.models.event import EventLog
from app.models.extension import ExtensionRequest
from app.models.rule import RuleProfile
from app.models.usage import DailyUsage
from app.models.user import User
from app.schemas.common import format_utc
from app.schemas.device import (
    ClientPairIn,
    DeviceDeleteImpactOut,
    DeviceDetailOut,
    DeviceOut,
)
from app.services import event_service, rule_service, usage_service


def compute_online_status(
    last_seen_at: datetime | None, now: datetime | None = None
) -> str:
    """按 D08 计算在线状态：≤120s online，≤900s stale，其余 offline。"""
    if last_seen_at is None:
        return OnlineStatus.OFFLINE.value
    moment = now or utcnow()
    elapsed = (moment - last_seen_at).total_seconds()
    if elapsed <= ONLINE_THRESHOLD_SECONDS:
        return OnlineStatus.ONLINE.value
    if elapsed <= STALE_THRESHOLD_SECONDS:
        return OnlineStatus.STALE.value
    return OnlineStatus.OFFLINE.value


def normalize_timezone(name: str | None) -> str:
    """校验 IANA 时区名，非法值回落到 `Asia/Shanghai`（D09）。"""
    return str(event_service.resolve_timezone(name).key)


def get_device_or_404(db: Session, device_id: str) -> Device:
    """按 id 查询设备。

    Raises:
        NotFoundError: 设备不存在。
    """
    device = db.get(Device, device_id)
    if device is None:
        raise NotFoundError("设备不存在", code=ErrorCode.DEVICE_NOT_FOUND)
    return device


def list_devices(
    db: Session, status: str | None = None, keyword: str | None = None
) -> list[Device]:
    """按状态与名称模糊搜索设备列表。"""
    stmt = select(Device)
    if status:
        stmt = stmt.where(Device.status == status)
    if keyword:
        stmt = stmt.where(Device.name.like(f"%{keyword}%"))
    return list(db.execute(stmt.order_by(Device.created_at.asc())).scalars().all())


def active_credential(db: Session, device_id: str) -> DeviceCredential | None:
    """查询设备当前未撤销的凭证。"""
    return db.execute(
        select(DeviceCredential).where(
            DeviceCredential.device_id == device_id,
            DeviceCredential.revoked_at.is_(None),
        )
    ).scalar_one_or_none()


def revoke_active_credentials(
    db: Session, device_id: str, reason: str, *, now: datetime | None = None
) -> int:
    """撤销设备全部未撤销凭证，返回撤销条数。"""
    moment = now or utcnow()
    stmt = (
        update(DeviceCredential)
        .where(
            DeviceCredential.device_id == device_id,
            DeviceCredential.revoked_at.is_(None),
        )
        .values(revoked_at=moment, revoked_reason=reason)
    )
    count = int(db.execute(stmt).rowcount or 0)
    db.flush()
    return count


def authenticate_device(db: Session, device_id: str, secret: str) -> Device:
    """设备鉴权（D42）。

    Raises:
        UnauthorizedError: 设备不存在或凭证无效/已撤销。
        ForbiddenError: 设备已停用。
    """
    device = db.get(Device, device_id) if device_id else None
    if device is None:
        raise UnauthorizedError(
            "设备凭证无效或已撤销", code=ErrorCode.INVALID_DEVICE_CREDENTIAL
        )
    if device.status == DeviceStatus.RETIRED.value:
        raise ForbiddenError("设备已停用", code=ErrorCode.DEVICE_RETIRED)
    if not secret:
        raise UnauthorizedError(
            "设备凭证无效或已撤销", code=ErrorCode.INVALID_DEVICE_CREDENTIAL
        )
    lookup = device_secret_lookup(secret)
    credential = db.execute(
        select(DeviceCredential).where(
            DeviceCredential.device_id == device_id,
            DeviceCredential.revoked_at.is_(None),
            DeviceCredential.secret_lookup == lookup,
        )
    ).scalar_one_or_none()
    if credential is None or not verify_device_secret(secret, credential.secret_hash):
        raise UnauthorizedError(
            "设备凭证无效或已撤销", code=ErrorCode.INVALID_DEVICE_CREDENTIAL
        )
    return device


@retry_on_busy
def create_pairing_code(
    db: Session,
    user: User,
    device_id: str | None = None,
    note: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[PairingCode, str]:
    """生成配对码（D01/D02/D03/D05）。

    Args:
        db: 数据库会话。
        user: 发起的管理员。
        device_id: 提供则为重新配对，绑定到已有设备。
        note: 备注，当前仅用于人工识别，不落库（`pairing_codes` 表无该列）。
        now: 注入的当前时间（G8）。

    Returns:
        ``(配对码记录, 明文展示码 'XXXX-XXXX')``。

    Raises:
        NotFoundError: 目标设备不存在。
        ConflictError: 目标设备已停用。
        TooManyRequestsError: 该账号活跃码已达上限（默认 5）。
    """
    settings = get_settings()
    moment = now or utcnow()

    if device_id:
        device = get_device_or_404(db, device_id)
        if device.status == DeviceStatus.RETIRED.value:
            raise ConflictError("设备已停用", code=ErrorCode.DEVICE_RETIRED)

    active_count = int(
        db.execute(
            select(func.count())
            .select_from(PairingCode)
            .where(
                PairingCode.created_by == user.id,
                PairingCode.used_at.is_(None),
                PairingCode.expires_at > moment,
            )
        ).scalar_one()
        or 0
    )
    if active_count >= settings.PAIRING_CODE_MAX_ACTIVE:
        raise TooManyRequestsError(
            f"未使用的配对码已达上限（{settings.PAIRING_CODE_MAX_ACTIVE} 个）",
            code=ErrorCode.PAIRING_CODE_LIMIT,
        )

    display, normalized, code_hash = generate_pairing_code()
    record = PairingCode(
        code_hash=code_hash,
        code_prefix=normalized[:4],
        created_by=user.id,
        device_id=device_id,
        expires_at=moment + timedelta(minutes=settings.PAIRING_CODE_TTL_MINUTES),
        created_at=moment,
    )
    db.add(record)
    db.flush()
    return record, display


@retry_on_busy
def pair_device(
    db: Session, payload: ClientPairIn, *, now: datetime | None = None
) -> tuple[Device, str, RuleProfile]:
    """设备配对（API.md §7.1 服务端行为 1–6）。

    Returns:
        ``(设备, 明文 device_secret, 规则)``。

    Raises:
        NotFoundError: 配对码不存在。
        ConflictError: 配对码已被使用 / 目标设备已停用。
        GoneError: 配对码已过期。
    """
    moment = now or utcnow()
    normalized = normalize_pairing_code(payload.code)
    code_hash = sha256_hex(normalized)

    record = db.execute(
        select(PairingCode).where(PairingCode.code_hash == code_hash)
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("配对码不存在", code=ErrorCode.PAIRING_CODE_NOT_FOUND)
    if record.used_at is not None:
        raise ConflictError("配对码已被使用", code=ErrorCode.PAIRING_CODE_USED)
    if record.expires_at <= moment:
        raise GoneError("配对码已过期", code=ErrorCode.PAIRING_CODE_EXPIRED)

    timezone_name = normalize_timezone(payload.timezone)
    is_repair = record.device_id is not None

    if is_repair:
        device = db.get(Device, record.device_id)
        if device is None:
            raise NotFoundError("设备不存在", code=ErrorCode.DEVICE_NOT_FOUND)
        if device.status == DeviceStatus.RETIRED.value:
            raise ConflictError("设备已停用", code=ErrorCode.DEVICE_RETIRED)
        # 🔴 旧凭证在此刻才撤销，保证生成码期间旧机仍可同步（API.md §4.6）
        revoke_active_credentials(
            db, device.id, CredentialRevokeReason.REPAIR.value, now=moment
        )
    else:
        device = Device(
            id=new_uuid(),
            name=payload.device_name.strip() or "新设备",
            timezone=timezone_name,
            status=DeviceStatus.ACTIVE.value,
            created_at=moment,
            updated_at=moment,
        )
        db.add(device)
        db.flush()

    device.name = payload.device_name.strip() or device.name
    device.timezone = timezone_name
    device.client_version = payload.client_version
    device.os_info = payload.os_info
    device.paired_at = moment
    device.last_seen_at = moment
    device.updated_at = moment
    db.flush()

    rule = rule_service.get_rule(db, device.id)
    if rule is None:
        rule = rule_service.create_default_rule(db, device.id, now=moment)

    secret = generate_device_secret()
    db.add(
        DeviceCredential(
            device_id=device.id,
            secret_hash=hash_device_secret(secret),
            secret_lookup=device_secret_lookup(secret),
            issued_at=moment,
        )
    )

    record.used_at = moment
    record.used_by_device_id = device.id
    db.flush()

    event_service.record(
        db,
        EventType.DEVICE_PAIRED,
        device_id=device.id,
        user_id=record.created_by,
        payload={
            "repair": is_repair,
            "device_name": device.name,
            "client_version": payload.client_version,
            "timezone": timezone_name,
        },
        now=moment,
    )
    return device, secret, rule


@retry_on_busy
def update_device(
    db: Session,
    device_id: str,
    *,
    name: str | None = None,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> Device:
    """修改设备展示名与时区（V5 §4.2）。

    Args:
        db: 数据库会话。
        device_id: 设备 id。
        name: 新展示名，``None`` 表示不改。
        timezone_name: 新 IANA 时区，``None`` 表示不改。
            时区改动**不立即生效**，下次同步后由客户端应用（共享知识 12）。
        now: 注入的当前时间（G8）。

    Returns:
        更新后的设备。

    Raises:
        NotFoundError: 设备不存在。
    """
    device = get_device_or_404(db, device_id)
    if name is not None:
        device.name = name.strip()
    if timezone_name is not None:
        device.timezone = normalize_timezone(timezone_name)
    device.updated_at = now or utcnow()
    db.flush()
    return device


@retry_on_busy
def retire_device(
    db: Session, device_id: str, user: User, *, now: datetime | None = None
) -> Device:
    """停用设备（D06）：撤销凭证 + 取消 pending 指令 + 写事件。

    Raises:
        ConflictError: 设备已处于停用状态。
    """
    device = get_device_or_404(db, device_id)
    if device.status == DeviceStatus.RETIRED.value:
        raise ConflictError(
            "设备已处于停用状态", code=ErrorCode.DEVICE_ALREADY_RETIRED
        )
    moment = now or utcnow()
    device.status = DeviceStatus.RETIRED.value
    device.updated_at = moment
    revoked = revoke_active_credentials(
        db, device.id, CredentialRevokeReason.RETIRE.value, now=moment
    )
    db.execute(
        update(RemoteCommand)
        .where(
            RemoteCommand.device_id == device.id,
            RemoteCommand.status == CommandStatus.PENDING.value,
        )
        .values(status=CommandStatus.EXPIRED.value)
    )
    db.flush()
    event_service.record(
        db,
        EventType.CREDENTIAL_REVOKED,
        severity=EventSeverity.WARNING,
        device_id=device.id,
        user_id=user.id,
        actor_name=user.username,
        payload={"reason": CredentialRevokeReason.RETIRE.value, "revoked_count": revoked},
        now=moment,
    )
    # V5 §4.2：停用本身也要留痕，不能只有「凭证被撤销」这一条
    event_service.record(
        db,
        EventType.DEVICE_RETIRED,
        severity=EventSeverity.WARNING,
        device_id=device.id,
        user_id=user.id,
        actor_name=user.username,
        payload={"name": device.name, "retired_at": format_utc(moment)},
        now=moment,
    )
    return device


@retry_on_busy
def unretire_device(
    db: Session, device_id: str, user: User, *, now: datetime | None = None
) -> tuple[Device, bool]:
    """恢复已停用设备（V5 §5.3）。

    恢复的是**后台管理可见性**，不是客户端连接能力 —— 停用时凭证已被吊销且
    本操作**不重新签发**，因此调用方必须把 `needs_repair` 透传给前端提示重新配对。

    Args:
        db: 数据库会话。
        device_id: 设备 id。
        user: 执行操作的管理员。
        now: 注入的当前时间（G8）。

    Returns:
        ``(设备, 是否需要重新配对)``。

    Raises:
        NotFoundError: 设备不存在。
        ConflictError: 设备当前不是「已停用」状态。
    """
    device = get_device_or_404(db, device_id)
    if device.status != DeviceStatus.RETIRED.value:
        raise ConflictError(
            "该设备当前不是已停用状态", code=ErrorCode.DEVICE_NOT_RETIRED_FOR_UNRETIRE
        )
    moment = now or utcnow()
    device.status = DeviceStatus.ACTIVE.value
    device.updated_at = moment
    db.flush()

    needs_repair = active_credential(db, device.id) is None
    event_service.record(
        db,
        EventType.DEVICE_UNRETIRED,
        device_id=device.id,
        user_id=user.id,
        actor_name=user.username,
        payload={"name": device.name, "credential_revoked": needs_repair},
        now=moment,
    )
    return device, needs_repair


def _count_rows(db: Session, model: Any, device_id: str) -> int:
    """统计某张子表中属于该设备的行数。"""
    return int(
        db.execute(
            select(func.count()).select_from(model).where(model.device_id == device_id)
        ).scalar_one()
        or 0
    )


def get_delete_impact(db: Session, device_id: str) -> DeviceDeleteImpactOut:
    """删除影响预览（V5 §4.3），供 L3 弹窗展示真实条数。

    Args:
        db: 数据库会话。
        device_id: 设备 id。

    Returns:
        各子表条数与数据覆盖区间。

    Raises:
        NotFoundError: 设备不存在。
    """
    device = get_device_or_404(db, device_id)
    bounds = db.execute(
        select(func.min(EventLog.occurred_at), func.max(EventLog.occurred_at)).where(
            EventLog.device_id == device.id
        )
    ).one()
    first_at, last_at = bounds[0], bounds[1]
    return DeviceDeleteImpactOut(
        device_id=device.id,
        device_name=device.name,
        status=device.status,  # type: ignore[arg-type]
        usage_days=_count_rows(db, DailyUsage, device.id),
        event_count=_count_rows(db, EventLog, device.id),
        extension_count=_count_rows(db, ExtensionRequest, device.id),
        command_count=_count_rows(db, RemoteCommand, device.id),
        rule_profile_count=_count_rows(db, RuleProfile, device.id),
        pairing_code_count=_count_rows(db, PairingCode, device.id),
        credential_count=_count_rows(db, DeviceCredential, device.id),
        first_record_at=first_at or device.paired_at,
        last_record_at=last_at or device.last_seen_at,
    )


@retry_on_busy
def delete_device(
    db: Session,
    device_id: str,
    user: User,
    confirm_name: str,
    *,
    now: datetime | None = None,
) -> None:
    """硬删除设备（V5 §5.1，决策 D：只依赖数据库 CASCADE）。

    执行顺序**不可调换**：
        1. 三项校验（存在 / 已停用 / 名称完全匹配）；
        2. 统计各子表条数生成 payload 快照；
        3. 写 `DEVICE_DELETED` 事件，**`device_id` 必须为 NULL**；
        4. `DELETE FROM devices`，由数据库 CASCADE 清理全部子表。

    若第 3 步带上 `device_id`，该条留痕会在第 4 步被
    `fk_event_logs_device_id_devices ON DELETE CASCADE` 一并删除（共享知识 2）。

    Args:
        db: 数据库会话。
        device_id: 设备 id。
        user: 执行操作的管理员。
        confirm_name: L3 逐字确认输入的设备名，服务端二次校验。
        now: 注入的当前时间（G8）。

    Raises:
        NotFoundError: 设备不存在。
        ConflictError: 设备仍在管（必须先停用）。
        BadRequestError: `confirm_name` 与设备名不符。
    """
    device = get_device_or_404(db, device_id)
    if device.status != DeviceStatus.RETIRED.value:
        raise ConflictError(
            "请先停用该设备后再删除", code=ErrorCode.DEVICE_NOT_RETIRED
        )
    if confirm_name != device.name:
        raise BadRequestError(
            "输入的设备名称与实际不符", code=ErrorCode.DEVICE_NAME_MISMATCH
        )

    moment = now or utcnow()
    snapshot = {
        "device_id": device.id,
        "name": device.name,
        "paired_at": format_utc(device.paired_at) if device.paired_at else None,
        "usage_days": _count_rows(db, DailyUsage, device.id),
        "event_count": _count_rows(db, EventLog, device.id),
        "extension_count": _count_rows(db, ExtensionRequest, device.id),
        "command_count": _count_rows(db, RemoteCommand, device.id),
    }

    # 🔴 顺序关键：device_id 必须为 None，且必须先落库再删设备（同一事务）
    event_service.record(
        db,
        EventType.DEVICE_DELETED,
        severity=EventSeverity.WARNING,
        device_id=None,
        user_id=user.id,
        actor_name=user.username,
        payload=snapshot,
        now=moment,
    )
    db.flush()

    # 决策 D：不写逐表 DELETE，全部交给数据库 CASCADE（PRAGMA foreign_keys=ON
    # 已在 app/db/session.py 的连接事件中开启）。Device 的 6 个关系均标注了
    # passive_deletes=True，ORM 不会预加载子表、只发一条 DELETE devices。
    # `pairing_codes` 没有 ORM 关系，纯靠数据库外键 CASCADE 清理。
    db.execute(sa_delete(Device).where(Device.id == device.id))
    db.expunge(device)
    db.flush()


@retry_on_busy
def revoke_credentials(
    db: Session,
    device_id: str,
    user: User,
    reason: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[Device, int, datetime]:
    """立即撤销设备全部有效凭证。

    Returns:
        ``(设备, 撤销条数, 撤销时刻)``。
    """
    device = get_device_or_404(db, device_id)
    moment = now or utcnow()
    reason_value = (reason or CredentialRevokeReason.MANUAL_REVOKE.value)[:64]
    count = revoke_active_credentials(db, device.id, reason_value, now=moment)
    event_service.record(
        db,
        EventType.CREDENTIAL_REVOKED,
        severity=EventSeverity.WARNING,
        device_id=device.id,
        user_id=user.id,
        actor_name=user.username,
        payload={"reason": reason_value, "revoked_count": count},
        now=moment,
    )
    return device, count, moment


def pending_extension_count(db: Session, device_id: str, target: date) -> int:
    """该设备指定日期的待审批申请数。"""
    stmt = (
        select(func.count())
        .select_from(ExtensionRequest)
        .where(
            ExtensionRequest.device_id == device_id,
            ExtensionRequest.target_date == target,
            ExtensionRequest.status == ExtensionStatus.PENDING.value,
        )
    )
    return int(db.execute(stmt).scalar_one() or 0)


def build_device_out(
    db: Session, device: Device, *, now: datetime | None = None
) -> DeviceOut:
    """组装 `DeviceOut`（含设备时区今日的用量与配额，D13/D32）。"""
    moment = now or utcnow()
    today = usage_service.device_today(device, moment)
    rule = rule_service.get_rule(db, device.id)
    facts = usage_service.read_usage_facts(db, device, today, rule)

    return DeviceOut(
        id=device.id,
        name=device.name,
        timezone=device.timezone,
        status=device.status,  # type: ignore[arg-type]
        online_status=compute_online_status(device.last_seen_at, moment),  # type: ignore[arg-type]
        client_version=device.client_version,
        os_info=device.os_info,
        last_seen_at=device.last_seen_at,
        last_state=device.last_state,
        last_state_at=device.last_state_at,
        today_date=today,
        today_used_minutes=facts.used_minutes,
        today_bonus_minutes=facts.bonus_minutes,
        today_parent_minutes=facts.parent_minutes,
        today_base_quota_minutes=facts.base_quota_minutes,
        today_effective_quota_minutes=facts.effective_quota_minutes,
        today_remaining_minutes=facts.remaining_minutes,
        enforcement_enabled=rule.enforcement_enabled if rule else True,
        pending_extension_count=pending_extension_count(db, device.id, today),
        paired_at=device.paired_at,
        created_at=device.created_at,
    )


def build_device_detail(
    db: Session, device: Device, *, now: datetime | None = None
) -> DeviceDetailOut:
    """组装 `DeviceDetailOut`（API.md §4.3）。"""
    moment = now or utcnow()
    base = build_device_out(db, device, now=moment)
    rule = rule_service.get_rule_or_404(db, device.id)
    credential = active_credential(db, device.id)
    credential_count = int(
        db.execute(
            select(func.count())
            .select_from(DeviceCredential)
            .where(DeviceCredential.device_id == device.id)
        ).scalar_one()
        or 0
    )
    recent = [usage_service.to_out(row) for row in usage_service.list_recent(db, device.id, 7)]
    return DeviceDetailOut(
        **base.model_dump(),
        rules=rule_service.to_out(rule),
        recent_usage=recent,
        active_credential_issued_at=credential.issued_at if credential else None,
        credential_count=credential_count,
    )


def build_device_out_list(
    db: Session, devices: Sequence[Device], *, now: datetime | None = None
) -> list[DeviceOut]:
    """批量组装 `DeviceOut`。"""
    moment = now or utcnow()
    return [build_device_out(db, device, now=moment) for device in devices]


@retry_on_busy
def touch_heartbeat(
    db: Session,
    device: Device,
    *,
    client_version: str,
    timezone_name: str,
    os_info: str | None,
    state: str,
    state_changed_at: datetime,
    now: datetime | None = None,
) -> None:
    """同步第 3 步：更新设备心跳信息（覆盖写，天然幂等）。"""
    moment = now or utcnow()
    device.last_seen_at = moment
    device.last_state = state
    device.last_state_at = state_changed_at
    device.client_version = client_version
    device.timezone = normalize_timezone(timezone_name)
    if os_info is not None:
        device.os_info = os_info
    device.updated_at = moment
    db.flush()
