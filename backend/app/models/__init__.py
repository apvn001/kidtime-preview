"""全部 ORM 模型汇总（Alembic autogenerate 依赖本文件）。"""

from __future__ import annotations

from app.models.client_version import ClientVersion
from app.models.command import RemoteCommand
from app.models.device import Device, DeviceCredential, PairingCode
from app.models.event import EventLog
from app.models.extension import ExtensionRequest
from app.models.grant import TimeGrant
from app.models.idempotency import IdempotencyKey
from app.models.rule import RuleProfile
from app.models.rule_template import RuleTemplate
from app.models.usage import DailyUsage
from app.models.user import RefreshToken, User, UserPreference

__all__ = [
    "ClientVersion",
    "DailyUsage",
    "Device",
    "DeviceCredential",
    "EventLog",
    "ExtensionRequest",
    "IdempotencyKey",
    "TimeGrant",
    "PairingCode",
    "RefreshToken",
    "RemoteCommand",
    "RuleProfile",
    "RuleTemplate",
    "User",
    "UserPreference",
]
