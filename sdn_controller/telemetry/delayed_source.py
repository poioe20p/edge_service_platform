"""Delayed event-preserving telemetry source (RQ1 arm 2).

Same reliable in-order delivery as :class:`EventPreservingTelemetrySource`,
but every window is held in a FIFO hold queue and released only when
``now >= window_end + DELAY_S`` — a fixed, pre-registered delay applied per
window (never a one-time stream offset).

"No burst replay" semantics (precise):
- The delay is a per-window lower bound from each window's own ``window_end``.
  Windows are never batch-released together; steady-state cadence is one
  delivery per ``WINDOW_S``.
- No backward fetch / no backfill: the source only ever advances ``last_seq``
  by one and never replays from the log after a stall.
- Defensive gap policy: pulls are in-order from the durable log so gaps cannot
  occur by construction; if an ``aged_out`` (410) is observed, the gap is
  recorded and the source continues — it does NOT replay.

If the consumer's ``on_update`` is the bottleneck, due windows back up and
drain one-per-step at their due times; the measured ``delay_s`` exceeds
``DELAY_S`` by the backlog — that excess is measured, not masked.
"""

import logging
import time
from collections import deque

import requests
from os_ken.lib import hub

from .delivery_log import DeliveryLog, send_ack
from .models import TelemetrySummary
from .source import TelemetryEventSource

logger = logging.getLogger("os_ken.telemetry.delayed_source")


class DelayedEventPreservingTelemetrySource(TelemetryEventSource):
    """In-order pull + per-window FIFO hold queue with fixed release delay."""

    def __init__(self, endpoints: list[str], on_update=None, on_topology_update=None,
                 delay_s: float = 30.0, poll_interval_s: float = 0.5,
                 ack: bool = True) -> None:
        self._endpoints = endpoints
        self._delay_s = delay_s
        self._poll_interval_s = poll_interval_s
        self._on_update = on_update
        self._on_topology_update = on_topology_update   # accepted for ABC compat
        self._latest: dict[str, TelemetrySummary] = {}
        # URL-keyed state (each aggregator serves exactly one network).
        self._last_seq_by_url: dict[str, int] = {}
        self._network_id_by_url: dict[str, str] = {}
        self._pending_by_url: dict[str, deque] = {}
        self._delivery_log = DeliveryLog(mode="delayed_event_preserving")
        self._ack = ack

    def start(self) -> None:
        logger.info("delayed source starting: endpoints=%s delay=%.1fs",
                    self._endpoints, self._delay_s)
        hub.spawn(self._pull_loop)
        hub.spawn(self._release_loop)

    def get_latest(self, network_id: str) -> TelemetrySummary | None:
        return self._latest.get(network_id)

    def _record_gap(self, url: str, gap_from: int, gap_to: int) -> None:
        """Record one gap_recovery row per missed seq (window_id None marks a gap)."""
        _now = time.time()
        for _seq in range(gap_from, gap_to + 1):
            self._delivery_log.record(
                self._network_id_by_url.get(url, ""),
                _seq, None, _now, _now, mode="gap_recovery",
            )
        logger.warning("window gap url=%s seqs=%d..%d (aged out)", url, gap_from, gap_to)

    def _pull_loop(self) -> None:
        while True:
            for url in self._endpoints:
                self._poll_one(url)
            hub.sleep(self._poll_interval_s)

    def _poll_one(self, url: str) -> None:
        last_seq = self._last_seq_by_url.get(url, 0)
        try:
            resp = requests.get(
                f"{url}/windows",
                params={"after_seq": last_seq, "limit": 1},
                timeout=5,
            )
        except requests.RequestException as exc:
            logger.debug("pull failed for %s: %s", url, exc)
            return

        if resp.status_code == 410:
            # Defensive only — see EventPreservingTelemetrySource._poll_one.
            try:
                first = resp.json().get("first_available_seq")
                if isinstance(first, int) and first > last_seq + 1:
                    self._record_gap(url, last_seq + 1, first - 1)
                    self._last_seq_by_url[url] = first - 1
            except Exception as exc:
                logger.warning("gap handling failed for %s: %s", url, exc)
            return

        if resp.status_code != 200:
            return

        # Guarded per-window processing — see EventPreservingTelemetrySource.
        try:
            data = resp.json()
            aged_out_from = data.get("aged_out_from")
            if isinstance(aged_out_from, int) and aged_out_from > last_seq + 1:
                self._record_gap(url, last_seq + 1, aged_out_from - 1)
            for win in data.get("windows") or []:
                seq = win.get("window_seq")
                if seq is None:
                    continue
                self._last_seq_by_url[url] = seq
                self._network_id_by_url[url] = win.get("network_id", "")
                summary = TelemetrySummary.model_validate(win)
                self._pending_by_url.setdefault(url, deque()).append(summary)
        except Exception as exc:
            logger.exception("window pull processing failed for %s — continuing", url)

    def _release_loop(self) -> None:
        while True:
            try:
                released = False
                now = time.time()
                for url in list(self._pending_by_url.keys()):
                    q = self._pending_by_url[url]
                    if not q:
                        continue
                    head = q[0]
                    due = head.window_end + self._delay_s
                    if due <= now:
                        q.popleft()
                        self._deliver(url, head)
                        released = True
                if released:
                    # Yield the hub so the pull loop and ZMQ control channel can
                    # run; each window still releases at its own due time.
                    hub.sleep(0)
                    continue
                sleep_s = None
                now = time.time()
                for q in self._pending_by_url.values():
                    if q:
                        due = q[0].window_end + self._delay_s
                        if sleep_s is None or due < sleep_s:
                            sleep_s = due
                if sleep_s is not None:
                    hub.sleep(max(0.0, sleep_s - now))
                else:
                    hub.sleep(self._poll_interval_s)
            except Exception as exc:
                logger.exception("release tick failed — continuing: %s", exc)
                hub.sleep(self._poll_interval_s)

    def _deliver(self, url: str, summary: TelemetrySummary) -> None:
        release_ts = time.time()
        if summary.servers or summary.storage_servers:
            self._latest[summary.network_id] = summary
        self._delivery_log.record(
            summary.network_id, summary.window_seq, summary.window_id,
            summary.window_end, release_ts, mode="delayed_event_preserving",
            release_ts=release_ts,
        )
        if self._ack:
            send_ack(url, summary.window_id, summary.window_seq, release_ts)
        if self._on_update is not None:
            try:
                self._on_update(summary)
            except Exception:
                # Delivery already recorded; mark the consumer-side processing
                # failure explicitly (see EventPreservingTelemetrySource).
                self._delivery_log.record(
                    summary.network_id, summary.window_seq, None,
                    summary.window_end, release_ts, mode="processing_error",
                )
                logger.exception(
                    "on_update failed for %s seq=%s — continuing",
                    url, summary.window_seq,
                )
