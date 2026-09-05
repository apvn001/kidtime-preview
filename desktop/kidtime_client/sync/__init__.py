"""Backend synchronisation layer.

Everything in this package is designed around hard constraint #2
(Single-Writer): the objects defined here are either

* frozen dataclasses (:mod:`kidtime_client.sync.payloads`) that can safely be
  handed across the GUI thread / :class:`SyncWorker` thread boundary via
  ``Qt.QueuedConnection``, or
* pure HTTP code (:mod:`kidtime_client.sync.api_client`) that never touches
  SQLite.

The only component allowed to write to the local database is the GUI main
thread, which receives :class:`~kidtime_client.sync.payloads.SyncResponse`
objects through :class:`~kidtime_client.sync.sync_engine.SyncEngine` signals.
"""

from __future__ import annotations

from kidtime_client.sync.api_client import ApiClient, ApiError
from kidtime_client.sync.command_handler import CommandHandler, CommandOutcome
from kidtime_client.sync.payloads import (
    AcceptedCounts,
    CommandAck,
    CommandDeliverItem,
    CreditItem,
    DeviceSyncInfo,
    EventUploadItem,
    ExtensionResultItem,
    ExtensionUploadItem,
    PairResponse,
    SyncRequest,
    SyncResponse,
    UsageUploadItem,
)
from kidtime_client.sync.sync_engine import SyncEngine, SyncWorker

__all__ = [
    "AcceptedCounts",
    "ApiClient",
    "ApiError",
    "CommandAck",
    "CommandDeliverItem",
    "CommandHandler",
    "CommandOutcome",
    "CreditItem",
    "DeviceSyncInfo",
    "EventUploadItem",
    "ExtensionResultItem",
    "ExtensionUploadItem",
    "PairResponse",
    "SyncEngine",
    "SyncRequest",
    "SyncResponse",
    "SyncWorker",
    "UsageUploadItem",
]
