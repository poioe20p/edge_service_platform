"""Polling telemetry source (RQ1 latest-state arm) — polls aggregator HTTP cache.

Deduplicates by ``window_seq`` and records every observed window (including
empty ones) in the delivery log. Implements the same TelemetryEventSource ABC
as the ZMQ/event-preserving/delayed sources so the controller is
transport-agnostic.
"""

import logging
import time

import requests
from os_ken.lib import hub

from .delivery_log import DeliveryLog
from .models import TelemetrySummary
from .source import TelemetryEventSource

logger = logging.getLogger("os_ken.telemetry.polling_source")


class PollingTelemetrySource(TelemetryEventSource):
    """Polls aggregator /latest_summary endpoints at a fixed interval.

    Deduplicates by window_seq so the controller's _on_telemetry_update is
    only called when a genuinely new summary is available — not on every
    poll iteration. Every observed window (incl. empty) is passed to
    on_update and recorded in the delivery log; only non-empty windows are
    cached (empty ones would corrupt WSM cost inputs).
    """

    def __init__(
        self,
        endpoints: list[str],
        interval_s: float = 10.0,
        on_update=None,
    ) -> None:
        """
        endpoints:   list of HTTP base URLs to aggregator cache endpoints.
                     Example: ["http://10.0.0.5:5558", "http://10.0.1.5:5558"]
        interval_s:  seconds between poll cycles.
        on_update:   optional callable(summary: TelemetrySummary).
        """
        self._endpoints = endpoints
        self._interval_s = interval_s
        self._on_update = on_update
        self._latest: dict[str, TelemetrySummary] = {}
        # Dedup: fire on_update only when window_seq advances — robust to an
        # aggregator restart resuming window_seq from its durable JSONL.
        self._last_window_seq: dict[str, int] = {}
        self._delivery_log = DeliveryLog(mode="latest_state")

    def start(self) -> None:
        """Spawn the background greenthread that polls aggregators."""
        logger.info(
            "polling telemetry source starting: endpoints=%s interval=%.1fs",
            self._endpoints,
            self._interval_s,
        )
        hub.spawn(self._poll_loop)

    def get_latest(self, network_id: str) -> TelemetrySummary | None:
        return self._latest.get(network_id)

    def _poll_loop(self) -> None:
        while True:
            # Poll all aggregators concurrently so summaries arrive at nearly
            # the same instant — minimises skew between LAN1/LAN2 data views.
            tasks = [hub.spawn(self._poll_one, url) for url in self._endpoints]
            for t in tasks:
                t.wait()
            hub.sleep(self._interval_s)

    def _poll_one(self, url: str) -> None:
        try:
            resp = requests.get(
                f"{url}/latest_summary",
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or not data:
                return
            summary = TelemetrySummary.model_validate(data)
        except Exception:
            logger.exception("poll failed for %s", url)
            return

        # Control mini-summaries (window_seq None) are delivered over the ZMQ
        # control channel in all modes, not through the polling source.
        try:
            if summary.window_seq is None:
                logger.debug("skipping control mini-summary from %s", url)
                return

            network_id = summary.network_id
            prev_seq = self._last_window_seq.get(network_id, 0)
            if summary.window_seq <= prev_seq:
                logger.debug(
                    "duplicate summary network=%s seq=%s (last=%s), skipping",
                    network_id,
                    summary.window_seq,
                    prev_seq,
                )
                return
            self._last_window_seq[network_id] = summary.window_seq

            # Every observed window (incl. empty) counts as delivered here; the
            # ANALYZER derives misses from the window universe vs this log.
            self._delivery_log.record(
                network_id, summary.window_seq, summary.window_id,
                summary.window_end, time.time(), mode="latest_state",
            )

            # Cache only non-empty windows (empties would corrupt WSM inputs);
            # on_update still receives them to keep the Design-B ticker consistent.
            if summary.servers or summary.storage_servers:
                self._latest[network_id] = summary
            if self._on_update is not None:
                self._on_update(summary)
            logger.debug(
                "new summary network=%s seq=%s window_end=%.3f",
                network_id,
                summary.window_seq,
                summary.window_end,
            )
        except Exception:
            logger.exception("observed-window processing failed for %s — continuing", url)
