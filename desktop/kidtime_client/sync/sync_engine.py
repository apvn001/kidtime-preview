"""Background synchronisation driver.

Thread model (hard constraint #2)::

    GUI main thread                    SyncWorker thread (QThread)
    ---------------                    ---------------------------
    SyncEngine (QObject)   --submit-->  do_sync(SyncRequest)   [HTTP only]
        ^                                     |
        |<---- succeeded(SyncResponse) -------+   (Qt.QueuedConnection)
        |<---- failed(str) -------------------+

Only frozen dataclasses cross the boundary. The worker never opens a SQLite
connection; the main thread is the sole writer.

Failure handling follows D46: the retry interval walks 45 -> 90 -> 180 -> 300
seconds and stays capped at 300 until a sync succeeds. A sync failure must
never disturb the 1-second tick loop (D50/G1).
"""

from __future__ import annotations

import logging
from typing import Callable, Final

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from kidtime_client.core.clock import Clock
from kidtime_client.sync.api_client import ApiClient, ApiError
from kidtime_client.sync.payloads import SyncRequest, SyncResponse

logger = logging.getLogger(__name__)

#: Minimum / maximum sync interval accepted from the rule snapshot.
MIN_INTERVAL_SECONDS: Final[int] = 15
MAX_INTERVAL_SECONDS: Final[int] = 300


class SyncWorker(QObject):
    """Executes the HTTP round trip inside a :class:`QThread`.

    The worker owns one lazily created :class:`ApiClient`. It performs no
    database access whatsoever.

    Args:
        api_factory: Callable returning a ready-to-use :class:`ApiClient`, or
            ``None`` when the device is not paired yet.
    """

    succeeded = Signal(object)  # SyncResponse
    failed = Signal(str)  # human-readable error message

    def __init__(self, api_factory: Callable[[], ApiClient | None]) -> None:
        super().__init__()
        self._api_factory = api_factory
        self._api: ApiClient | None = None

    @Slot(object)
    def do_sync(self, req: object) -> None:
        """Run one sync request. Never raises.

        Args:
            req: The :class:`SyncRequest` produced on the GUI thread. Typed as
                ``object`` because Qt queued connections marshal ``PyObject``.
        """
        if not isinstance(req, SyncRequest):
            self.failed.emit("内部错误：同步请求类型不正确")
            return
        try:
            api = self._ensure_api()
            if api is None:
                self.failed.emit("设备尚未配对，无法同步")
                return
            response = api.sync(req)
        except ApiError as exc:
            if exc.is_auth_error:
                # Credentials may have been rotated; drop the client so the
                # next attempt rebuilds it from the credential store.
                self._reset_api()
            logger.warning("Sync failed: %s", exc.message)
            self.failed.emit(exc.message)
            return
        except Exception as exc:  # pragma: no cover - defensive catch-all
            logger.exception("Unexpected error during sync")
            self._reset_api()
            self.failed.emit(f"同步异常：{exc}")
            return
        self.succeeded.emit(response)

    @Slot()
    def shutdown(self) -> None:
        """Release the HTTP client. Called before the thread quits."""
        self._reset_api()

    def _ensure_api(self) -> ApiClient | None:
        """Return the cached API client, creating it on first use."""
        if self._api is None:
            self._api = self._api_factory()
        return self._api

    def _reset_api(self) -> None:
        """Close and forget the cached API client."""
        if self._api is not None:
            self._api.close()
            self._api = None


class SyncEngine(QObject):
    """Main-thread scheduler owning the worker thread.

    Signal flow for one cycle:

    1. The interval timer fires (or :meth:`trigger_now` is called).
    2. :attr:`requestBuildNeeded` is emitted; the engine's owner builds a
       :class:`SyncRequest` from the database and calls :meth:`submit`.
    3. :meth:`submit` hands the frozen request to the worker thread.
    4. :attr:`syncSucceeded` / :attr:`syncFailed` come back on the main thread.

    Args:
        api_factory: Callable creating an :class:`ApiClient` (invoked in the
            worker thread).
        clock: Injected clock, used for logging and testability.
        interval_provider: Callable returning the current
            ``rules.sync_interval_seconds``.
    """

    requestBuildNeeded = Signal()
    syncStarted = Signal()
    syncSucceeded = Signal(object)  # SyncResponse
    syncFailed = Signal(str)

    #: Internal signal used to marshal the request onto the worker thread.
    _submitRequested = Signal(object)

    #: Retry backoff after consecutive failures (D46).
    BACKOFF_SECONDS: Final[tuple[int, ...]] = (45, 90, 180, 300)

    def __init__(
        self,
        api_factory: Callable[[], ApiClient | None],
        clock: Clock,
        interval_provider: Callable[[], int],
    ) -> None:
        super().__init__()
        self._clock = clock
        self._interval_provider = interval_provider
        self._failure_streak = 0
        self._in_flight = False
        self._pending_trigger = False
        self._running = False

        self._thread = QThread()
        self._thread.setObjectName("KidTimeSyncWorker")
        self._worker = SyncWorker(api_factory)
        self._worker.moveToThread(self._thread)

        self._worker.succeeded.connect(self._on_worker_succeeded, Qt.QueuedConnection)
        self._worker.failed.connect(self._on_worker_failed, Qt.QueuedConnection)
        self._submitRequested.connect(self._worker.do_sync, Qt.QueuedConnection)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.timeout.connect(self._on_timer)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the worker thread and schedule the first sync."""
        if self._running:
            return
        self._running = True
        self._thread.start()
        # First sync fires quickly so a freshly launched client picks up rules.
        self._timer.start(2000)
        logger.info("SyncEngine started")

    def stop(self) -> None:
        """Stop the timer and shut the worker thread down (waits up to 3s)."""
        if not self._running:
            return
        self._running = False
        self._timer.stop()
        try:
            self._worker.shutdown()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Worker shutdown raised", exc_info=True)
        self._thread.quit()
        if not self._thread.wait(3000):  # pragma: no cover - slow shutdown
            logger.warning("Sync worker thread did not stop within 3s; terminating")
            self._thread.terminate()
            self._thread.wait(1000)
        logger.info("SyncEngine stopped")

    def rebind(self, api_factory: Callable[[], ApiClient | None]) -> None:
        """🔴 1.2 重配对：停止旧 worker，换上新服务器的 API 工厂，再启动。

        调用方（``RepairCoordinator``）必须**先**完成凭据切换（写 DPAPI +
        ``SETTING_BASE_URL`` + 内存 ``app._credentials``），再调用本方法——
        因为新工厂闭包读取的正是已经换好的凭据。

        旧的 ``SyncWorker`` 持有旧凭据的 ``ApiClient`` 快照（api_factory 闭包），
        直接复用会让「新旧凭据交叉发往新旧服务器」。因此这里**整个重建**
        worker 对象：旧 worker 在线程停止时已 ``shutdown()``（``_reset_api()``
        关闭旧连接），新 worker 只认识新工厂。

        幂等：已停止时等效于 start；已运行时先 stop 再起。
        """
        self.stop()
        self._failure_streak = 0
        self._in_flight = False
        self._pending_trigger = False

        self._worker = SyncWorker(api_factory)
        self._worker.moveToThread(self._thread)
        self._worker.succeeded.connect(self._on_worker_succeeded, Qt.QueuedConnection)
        self._worker.failed.connect(self._on_worker_failed, Qt.QueuedConnection)
        self._submitRequested.connect(self._worker.do_sync, Qt.QueuedConnection)

        self.start()
        logger.info("SyncEngine rebound to a new server")

    @property
    def running(self) -> bool:
        """``True`` between :meth:`start` and :meth:`stop`."""
        return self._running

    @property
    def in_flight(self) -> bool:
        """``True`` while a request is being processed by the worker."""
        return self._in_flight

    @property
    def failure_streak(self) -> int:
        """Number of consecutive failures (reset to 0 on success)."""
        return self._failure_streak

    # ------------------------------------------------------------------
    # Triggering
    # ------------------------------------------------------------------
    def trigger_now(self) -> None:
        """Request an immediate sync (tray menu or ``SYNC_NOW`` command)."""
        if not self._running:
            return
        if self._in_flight:
            self._pending_trigger = True
            return
        self._timer.stop()
        self._begin_cycle()

    def submit(self, req: SyncRequest) -> None:
        """Hand a freshly built request to the worker thread.

        Args:
            req: Immutable request built on the GUI thread. Passing ``None``
                (nothing to sync) simply reschedules the next attempt.
        """
        if req is None:
            self._in_flight = False
            self._schedule_next(ok=True)
            return
        self._in_flight = True
        self.syncStarted.emit()
        self._submitRequested.emit(req)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @Slot()
    def _on_timer(self) -> None:
        """Interval timer callback."""
        self._begin_cycle()

    def _begin_cycle(self) -> None:
        """Ask the owner to build a request; guarded against re-entry."""
        if not self._running or self._in_flight:
            return
        try:
            self.requestBuildNeeded.emit()
        except Exception:  # pragma: no cover - defensive
            logger.exception("requestBuildNeeded handler raised")
            self._in_flight = False
            self._schedule_next(ok=False)

    @Slot(object)
    def _on_worker_succeeded(self, response: object) -> None:
        """Worker reported success (executed on the main thread)."""
        self._in_flight = False
        self._failure_streak = 0
        if isinstance(response, SyncResponse):
            try:
                self.syncSucceeded.emit(response)
            except Exception:  # pragma: no cover - defensive
                logger.exception("syncSucceeded handler raised")
        self._schedule_next(ok=True)

    @Slot(str)
    def _on_worker_failed(self, message: str) -> None:
        """Worker reported failure (executed on the main thread)."""
        self._in_flight = False
        self._failure_streak += 1
        try:
            self.syncFailed.emit(message)
        except Exception:  # pragma: no cover - defensive
            logger.exception("syncFailed handler raised")
        self._schedule_next(ok=False)

    def _schedule_next(self, ok: bool) -> None:
        """Schedule the next sync attempt.

        Args:
            ok: ``True`` after a successful round trip (use the configured
                interval), ``False`` to walk the backoff ladder.
        """
        if not self._running:
            return
        if self._pending_trigger and ok:
            self._pending_trigger = False
            self._timer.start(0)
            return
        self._pending_trigger = False
        self._timer.start(self.next_delay_seconds(ok) * 1000)

    def next_delay_seconds(self, ok: bool) -> int:
        """Compute the delay until the next attempt.

        Args:
            ok: Whether the previous attempt succeeded.

        Returns:
            Delay in seconds, clamped to ``[15, 300]`` on success and taken
            from :attr:`BACKOFF_SECONDS` on failure.
        """
        if ok:
            try:
                interval = int(self._interval_provider())
            except Exception:  # pragma: no cover - defensive
                logger.debug("interval_provider raised; using 45s", exc_info=True)
                interval = 45
            return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))
        index = min(self._failure_streak, len(self.BACKOFF_SECONDS)) - 1
        index = max(0, index)
        return self.BACKOFF_SECONDS[index]


__all__ = ["MAX_INTERVAL_SECONDS", "MIN_INTERVAL_SECONDS", "SyncEngine", "SyncWorker"]
