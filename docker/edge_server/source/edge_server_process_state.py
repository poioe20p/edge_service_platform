from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from edge_server_config import EdgeServerConfig
from local_request_state import LocalRequestEvent, LocalRequestState
from telemetry import ZmqMetricSender, _get_server_mac

log = logging.getLogger(__name__)


SKIP_COUNTING_PATHS = frozenset({"/health", "/drain", "/ready", "/pool_renew"})


@dataclass
class EdgeServerProcessState:
    """Single owner for process-local serving state.

    This module intentionally keeps non-request, non-Mongo mutable state in one
    place so route modules and request hooks do not each keep their own copy of
    drain flags, counters, or the shared telemetry sender.
    """

    config: EdgeServerConfig
    local_request_state: LocalRequestState = field(init=False)
    metric_sender: ZmqMetricSender = field(default_factory=ZmqMetricSender)
    draining: bool = False
    active_requests: int = 0
    last_user_request_ts: float = field(default_factory=time.monotonic)
    active_requests_lock: threading.Lock = field(default_factory=threading.Lock)
    drain_monitor_thread: threading.Thread | None = None
    # False until a real MongoDB round-trip succeeds (app.py's probe); /ready is
    # 200 iff this is True.
    app_ready: bool = False

    def mark_app_ready(self) -> None:
        self.app_ready = True
        log.info("EdgeServerProcessState marked application-ready")
        # `app_ready` drives the controller's event-driven admission; gated on
        # EDGE_APP_READY_EVENT=1 (default off, so non-RQ3 runs emit nothing).
        if os.environ.get("EDGE_APP_READY_EVENT", "0") == "1":
            try:
                self.metric_sender.send({
                    "event_type": "app_ready",
                    "server_id": _get_server_mac(),
                    "ts": time.time(),
                })
                log.info("app_ready control event emitted")
            except Exception:
                log.exception("[app-ready] event emission failed")

    def __post_init__(self) -> None:
        self.local_request_state = LocalRequestState(
            max_events=self.config.local_request_buffer_max_events,
            per_content_window=self.config.local_request_per_content_window,
        )

    def get_drain_state(self) -> str:
        with self.active_requests_lock:
            return "draining" if self.draining else "active"

    def begin_counted_request(self, *, path: str) -> bool:
        if path in SKIP_COUNTING_PATHS:
            return False
        with self.active_requests_lock:
            self.active_requests += 1
            self.last_user_request_ts = time.monotonic()
        return True

    def end_counted_request(self, *, counted: bool) -> None:
        if not counted:
            return
        with self.active_requests_lock:
            self.active_requests -= 1

    def activate_drain(self) -> int:
        with self.active_requests_lock:
            self.draining = True
            self.last_user_request_ts = time.monotonic()
            return self.active_requests

    def cancel_drain(self) -> int:
        with self.active_requests_lock:
            self.draining = False
            return self.active_requests

    def ensure_drain_monitor(self) -> None:
        with self.active_requests_lock:
            if self.drain_monitor_thread is not None and self.drain_monitor_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._drain_monitor_loop,
                daemon=True,
                name="drain-monitor",
            )
            self.drain_monitor_thread = thread
        thread.start()

    def _drain_monitor_loop(self) -> None:
        while True:
            time.sleep(self.config.drain_poll_interval_s)
            with self.active_requests_lock:
                draining = self.draining
                remaining = self.active_requests
                quiet_for = time.monotonic() - self.last_user_request_ts
            if not draining:
                return
            if remaining <= 0 and quiet_for >= self.config.drain_quiet_period_s:
                log.info(
                    "Drain complete — all in-flight requests finished, sending drain_complete event"
                )
                self.metric_sender.send(
                    {
                        "event_type": "drain_complete",
                        "server_id": _get_server_mac(),
                        "ts": time.time(),
                    }
                )
                time.sleep(0.1)
                os._exit(0)

    @staticmethod
    def build_local_request_event(
        *,
        timestamp_epoch: float,
        request_kind: str,
        content_id: str | None,
        user_id: str,
        latency_ms: float,
        served_from_tier: int,
        tier1_hit_ratio: float,
        tier1_eligible_reads: int,
        relevance: str,
        status: str,
        tags: tuple[str, ...],
    ) -> LocalRequestEvent:
        return LocalRequestEvent(
            timestamp_epoch=timestamp_epoch,
            request_kind=request_kind,
            content_id=content_id,
            user_id=user_id,
            latency_ms=latency_ms,
            served_from_tier=served_from_tier,
            tier1_hit_ratio=tier1_hit_ratio,
            tier1_eligible_reads=tier1_eligible_reads,
            relevance=relevance,
            status=status,
            tags=tags,
        )