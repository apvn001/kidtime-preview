"""Frozen dataclasses mirroring the ``/client/sync`` wire contract.

Hard constraint #2 (Single-Writer) requires that anything crossing the
GUI-thread / worker-thread boundary is an **immutable** value object.  Every
type in this module is therefore ``@dataclass(frozen=True, slots=True)`` and
carries no reference to a database connection, a Qt widget, or a repository.

Field names are copied verbatim from ``backend/app/schemas/client_sync.py``.
Renaming any of them breaks the contract between client and backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Final, Mapping, Sequence

#: ISO-8601 format used on the wire. The backend normalises everything to UTC.
_ISO_Z: Final[str] = "%Y-%m-%dT%H:%M:%S.%fZ"


# ----------------------------------------------------------------------
# Serialisation helpers
# ----------------------------------------------------------------------
def to_utc_iso(value: datetime) -> str:
    """Render ``value`` as an RFC3339 UTC timestamp string.

    Args:
        value: Timezone-aware or naive datetime. Naive values are assumed UTC.

    Returns:
        A string like ``2025-05-01T09:30:00.000000Z``.
    """
    if value.tzinfo is None:
        aware = value.replace(tzinfo=timezone.utc)
    else:
        aware = value.astimezone(timezone.utc)
    return aware.strftime(_ISO_Z)


def parse_utc(value: str | datetime | None) -> datetime | None:
    """Parse a backend timestamp into an aware UTC :class:`datetime`.

    Args:
        value: ISO-8601 string (``Z`` or offset suffix), a datetime, or ``None``.

    Returns:
        An aware UTC datetime, or ``None`` when ``value`` is ``None``/empty.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_date(value: str | date | None) -> date | None:
    """Parse a ``YYYY-MM-DD`` string into a :class:`datetime.date`.

    Args:
        value: Date string, date object, or ``None``.

    Returns:
        The parsed date, or ``None`` when the input is empty.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


# ----------------------------------------------------------------------
# Uplink items
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class UsageUploadItem:
    """One day of usage to upload (``SyncRequest.usage``)."""

    usage_date: date
    used_minutes: int
    bonus_minutes: int
    parent_minutes: int
    break_count: int
    base_quota_minutes: int

    def to_json(self) -> dict[str, Any]:
        """Return the JSON-serialisable representation."""
        return {
            "usage_date": self.usage_date.isoformat(),
            "used_minutes": max(0, int(self.used_minutes)),
            "bonus_minutes": max(0, int(self.bonus_minutes)),
            "parent_minutes": max(0, int(self.parent_minutes)),
            "break_count": max(0, int(self.break_count)),
            "base_quota_minutes": max(0, int(self.base_quota_minutes)),
        }


@dataclass(frozen=True, slots=True)
class EventUploadItem:
    """One local event to upload (``SyncRequest.events``)."""

    client_event_id: str
    event_type: str
    severity: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> dict[str, Any]:
        """Return the JSON-serialisable representation."""
        return {
            "client_event_id": self.client_event_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "payload": dict(self.payload or {}),
            "occurred_at": to_utc_iso(self.occurred_at),
        }


@dataclass(frozen=True, slots=True)
class ExtensionUploadItem:
    """One extension request to upload (``SyncRequest.extension_requests``)."""

    id: str
    target_date: date
    requested_minutes: int
    reason: str = ""
    client_created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> dict[str, Any]:
        """Return the JSON-serialisable representation."""
        return {
            "id": self.id,
            "target_date": self.target_date.isoformat(),
            "requested_minutes": int(self.requested_minutes),
            "reason": self.reason or "",
            "client_created_at": to_utc_iso(self.client_created_at),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ExtensionUploadItem":
        """Rebuild an item from an outbox payload dict."""
        target = parse_date(data.get("target_date"))
        created = parse_utc(data.get("client_created_at"))
        return cls(
            id=str(data["id"]),
            target_date=target or datetime.now(timezone.utc).date(),
            requested_minutes=int(data.get("requested_minutes", 0)),
            reason=str(data.get("reason") or ""),
            client_created_at=created or datetime.now(timezone.utc),
        )


@dataclass(frozen=True, slots=True)
class CommandAck:
    """One command acknowledgement (``SyncRequest.command_acks``)."""

    command_id: str
    status: str
    executed_at: datetime
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the JSON-serialisable representation."""
        return {
            "command_id": self.command_id,
            "status": self.status,
            "error": self.error,
            "executed_at": to_utc_iso(self.executed_at),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CommandAck":
        """Rebuild an ack from an outbox payload dict."""
        executed = parse_utc(data.get("executed_at"))
        return cls(
            command_id=str(data["command_id"]),
            status=str(data.get("status") or "success"),
            executed_at=executed or datetime.now(timezone.utc),
            error=data.get("error"),
        )


@dataclass(frozen=True, slots=True)
class SyncRequest:
    """Full ``POST /client/sync`` request body.

    Uplink data is always sent **before** the response is applied downlink
    (steps 1-6 server side, steps 7-9 downlink) so the server never issues a
    credit for an extension the client has not yet reported.
    """

    client_version: str
    timezone: str
    device_time: datetime
    effective_date: date
    state: str
    state_changed_at: datetime
    rule_version: int
    usage: tuple[UsageUploadItem, ...] = ()
    events: tuple[EventUploadItem, ...] = ()
    extension_requests: tuple[ExtensionUploadItem, ...] = ()
    command_acks: tuple[CommandAck, ...] = ()
    credit_confirms: tuple[str, ...] = ()
    #: Local bookkeeping so the main thread can mark rows as sent afterwards.
    outbox_row_ids: tuple[int, ...] = ()
    usage_dates: tuple[date, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Return the JSON body exactly as the backend schema expects it."""
        return {
            "client_version": self.client_version,
            "timezone": self.timezone,
            "device_time": to_utc_iso(self.device_time),
            "effective_date": self.effective_date.isoformat(),
            "state": self.state,
            "state_changed_at": to_utc_iso(self.state_changed_at),
            "rule_version": int(self.rule_version),
            "usage": [item.to_json() for item in self.usage],
            "events": [item.to_json() for item in self.events],
            "extension_requests": [item.to_json() for item in self.extension_requests],
            "command_acks": [item.to_json() for item in self.command_acks],
            "credit_confirms": list(self.credit_confirms),
        }


# ----------------------------------------------------------------------
# Downlink items
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeviceSyncInfo:
    """Device echo block from the sync response."""

    id: str
    name: str
    status: str
    timezone: str

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "DeviceSyncInfo":
        """Build from the backend JSON payload."""
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            status=str(data.get("status") or "active"),
            timezone=str(data.get("timezone") or "Asia/Shanghai"),
        )


@dataclass(frozen=True, slots=True)
class CommandDeliverItem:
    """A remote command delivered by the backend."""

    command_id: str
    type: str
    payload: Mapping[str, Any]
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CommandDeliverItem":
        """Build from the backend JSON payload."""
        now = datetime.now(timezone.utc)
        return cls(
            command_id=str(data["command_id"]),
            type=str(data["type"]),
            payload=dict(data.get("payload") or {}),
            created_at=parse_utc(data.get("created_at")) or now,
            expires_at=parse_utc(data.get("expires_at")) or now,
        )


@dataclass(frozen=True, slots=True)
class CreditItem:
    """An approved extension awaiting local crediting."""

    request_id: str
    target_date: date
    approved_minutes: int
    requested_minutes: int
    decided_at: datetime

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CreditItem":
        """Build from the backend JSON payload."""
        return cls(
            request_id=str(data["request_id"]),
            target_date=parse_date(data.get("target_date")) or date.today(),
            approved_minutes=int(data.get("approved_minutes") or 0),
            requested_minutes=int(data.get("requested_minutes") or 0),
            decided_at=parse_utc(data.get("decided_at")) or datetime.now(timezone.utc),
        )


@dataclass(frozen=True, slots=True)
class ExtensionResultItem:
    """Latest status of an extension request previously submitted."""

    id: str
    status: str
    approved_minutes: int | None = None
    decided_at: datetime | None = None
    reject_reason: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ExtensionResultItem":
        """Build from the backend JSON payload."""
        approved = data.get("approved_minutes")
        return cls(
            id=str(data["id"]),
            status=str(data.get("status") or "pending"),
            approved_minutes=int(approved) if approved is not None else None,
            decided_at=parse_utc(data.get("decided_at")),
            reject_reason=data.get("reject_reason"),
        )


@dataclass(frozen=True, slots=True)
class AcceptedCounts:
    """How many uplink rows the backend actually accepted."""

    usage: int = 0
    events: int = 0
    extension_requests: int = 0
    command_acks: int = 0
    credit_confirms: int = 0

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "AcceptedCounts":
        """Build from the backend JSON payload (``None`` yields all zeros)."""
        data = data or {}
        return cls(
            usage=int(data.get("usage") or 0),
            events=int(data.get("events") or 0),
            extension_requests=int(data.get("extension_requests") or 0),
            command_acks=int(data.get("command_acks") or 0),
            credit_confirms=int(data.get("credit_confirms") or 0),
        )


@dataclass(frozen=True, slots=True)
class SyncResponse:
    """Full ``POST /client/sync`` response body.

    ``rules`` is ``None`` when the server-side rule version equals the version
    the client reported, meaning the client keeps its cached snapshot.
    """

    server_time: datetime
    device: DeviceSyncInfo
    accepted: AcceptedCounts
    next_sync_seconds: int
    rules: Mapping[str, Any] | None = None
    commands: tuple[CommandDeliverItem, ...] = ()
    credits: tuple[CreditItem, ...] = ()
    extension_results: tuple[ExtensionResultItem, ...] = ()
    #: Echo of the request bookkeeping so the main thread can mark rows sent.
    request: SyncRequest | None = None

    @classmethod
    def from_json(
        cls, data: Mapping[str, Any], request: SyncRequest | None = None
    ) -> "SyncResponse":
        """Build a response object from the decoded backend JSON.

        Args:
            data: Decoded JSON body.
            request: The originating request, attached for bookkeeping.

        Returns:
            An immutable :class:`SyncResponse`.
        """
        return cls(
            server_time=parse_utc(data.get("server_time")) or datetime.now(timezone.utc),
            device=DeviceSyncInfo.from_json(data.get("device") or {}),
            accepted=AcceptedCounts.from_json(data.get("accepted")),
            next_sync_seconds=int(data.get("next_sync_seconds") or 45),
            rules=dict(data["rules"]) if data.get("rules") else None,
            commands=tuple(
                CommandDeliverItem.from_json(item) for item in (data.get("commands") or [])
            ),
            credits=tuple(CreditItem.from_json(item) for item in (data.get("credits") or [])),
            extension_results=tuple(
                ExtensionResultItem.from_json(item)
                for item in (data.get("extension_results") or [])
            ),
            request=request,
        )


@dataclass(frozen=True, slots=True)
class PairResponse:
    """Result of ``POST /client/pair`` (``device_secret`` returned once)."""

    device_id: str
    device_secret: str
    device_name: str
    timezone: str
    rules: Mapping[str, Any]
    server_time: datetime
    paired_at: datetime

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "PairResponse":
        """Build from the backend JSON payload."""
        now = datetime.now(timezone.utc)
        return cls(
            device_id=str(data["device_id"]),
            device_secret=str(data["device_secret"]),
            device_name=str(data.get("device_name") or ""),
            timezone=str(data.get("timezone") or "Asia/Shanghai"),
            rules=dict(data.get("rules") or {}),
            server_time=parse_utc(data.get("server_time")) or now,
            paired_at=parse_utc(data.get("paired_at")) or now,
        )

    def masked(self) -> str:
        """Log-safe representation (secret redacted)."""
        return f"PairResponse(device_id={self.device_id!r}, device_name={self.device_name!r})"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return self.masked()


def truncate(items: Sequence[Any], limit: int) -> tuple[Any, ...]:
    """Return at most ``limit`` items as a tuple.

    Args:
        items: Source sequence.
        limit: Maximum number of items to keep.

    Returns:
        A tuple with the first ``limit`` entries.
    """
    if limit <= 0:
        return ()
    return tuple(items[:limit])


__all__ = [
    "AcceptedCounts",
    "CommandAck",
    "CommandDeliverItem",
    "CreditItem",
    "DeviceSyncInfo",
    "EventUploadItem",
    "ExtensionResultItem",
    "ExtensionUploadItem",
    "PairResponse",
    "SyncRequest",
    "SyncResponse",
    "UsageUploadItem",
    "parse_date",
    "parse_utc",
    "to_utc_iso",
    "truncate",
]
