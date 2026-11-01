"""Event-preserving telemetry source (RQ1 arm 1).

Pulls completed telemetry windows in strict sequence order from the
aggregator's durable window log over HTTP. Delivers every window exactly
once, in source order, as soon as it is available (the reference arm).

Sequence validation + defensive gap recovery: the log is the single source of
truth, so in-order pulls cannot lose windows. If the log reports a requested
``after_seq`` has aged out (``410``), the gap is recorded in the delivery log
and the source advances to the first available window — it never blocks.

Implements the same ``TelemetryEventSource`` ABC as the ZMQ/poll sources so the
controller is transport-agnostic.
"""

import logging
import time

import requests
from os_ken.lib import hub

from .delivery_log import DeliveryLog, send_ack
from .models import TelemetrySummary
from .source import TelemetryEventSource

logger = logging.getLogger("os_ken.telemetry.event_preserving_source")


class EventPreservingTelemetrySource(TelemetryEventSource):
    """Pulls every completed window in order over the aggregator window log."""

    def __init__(self, endpoints: list[str], on_update=None, on_topology_update=None,
                 poll_interval_s: float = 0.5, ack: bool = True) -> None:
        self._endpoints = endpoints            # ["http://<host>:5558", ...]
        self._poll_interval_s = poll_interval_s
        self._on_update = on_update
        self._on_topology_update = on_topology_update   # accepted for ABC compat
        self._latest: dict[str, TelemetrySummary] = {}
        # URL-keyed: each aggregator serves exactly one network.
        self._last_seq_by_url: dict[str, int] = {}
        self._network_id_by_url: dict[str, str] = {}
        self._delivery_log = DeliveryLog(mode="event_preserving")
        self._ack = ack

    def start(self) -> None:
        logger.info("event-preserving source starting: endpoints=%s", self._endpoints)
        hub.spawn(self._loop)

    def get_latest(self, network_id: str) -> TelemetrySummary | None:
        return self._latest.get(network_id)

    def _loop(self) -> None:
        while True:
            for url in self._endpoints:
                self._poll_one(url)
            hub.sleep(self._poll_interval_s)

    def _record_gap(self, url: str, gap_from: int, gap_to: int) -> None:
        """Record one gap_recovery row per missed seq (window_id None marks a gap)."""
        _now = time.time()
        for _seq in range(gap_from, gap_to + 1):
            self._delivery_log.record(
                self._network_id_by_url.get(url, ""),
                _seq, None, _now, _now, mode="gap_recovery",
            )
        logger.warning("window gap url=%s seqs=%d..%d (aged out)", url, gap_from, gap_to)

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
            # Defensive only — the aggregator reports aged-out via 200 +
            # aged_out_from, never a bare 410; never move last_seq backward.
            try:
                first = resp.json().get("first_available_seq")
                if isinstance(first, int) and first > last_seq + 1:
                    self._record_gap(url, last_seq + 1, first - 1)
                    self._last_seq_by_url[url] = first - 1
            except Exception as exc:
                logger.warning("gap handling failed for %s: %s", url, exc)
            return

        if resp.status_code != 200:
            logger.debug("unexpected status %s from %s", resp.status_code, url)
            return

        # Guarded: an exception must not kill the delivery greenthread (never
        # re-spawned); last_seq still advances so errors are logged, not replayed.
        try:
            data = resp.json()
            aged_out_from = data.get("aged_out_from")
            if isinstance(aged_out_from, int) and aged_out_from > last_seq + 1:
                # Consumer is behind the retained range: record the missed
                # (aged-out) range; returned windows advance last_seq naturally.
                self._record_gap(url, last_seq + 1, aged_out_from - 1)
            for win in data.get("windows") or []:
                seq = win.get("window_seq")
                if seq is None:
                    continue
                self._last_seq_by_url[url] = seq
                network_id = win.get("network_id", "")
                self._network_id_by_url[url] = network_id
                summary = TelemetrySummary.model_validate(win)
                # Cache only real (non-empty) summaries — empty windows would
                # corrupt WSM cost inputs (mirrors ZmqTelemetrySource).
                if summary.servers or summary.storage_servers:
                    self._latest[network_id] = summary
                self._delivery_log.record(
                    network_id, seq, win.get("window_id"),
                    summary.window_end, time.time(), mode="event_preserving",
                )
                if self._ack:
                    send_ack(url, win.get("window_id"), seq, time.time())
                if self._on_update is not None:
                    try:
                        self._on_update(summary)
                    except Exception:
                        # Delivery already recorded; mark the consumer-side
                        # failure so the analyzer does not count it as consumed.
                        self._delivery_log.record(
                            network_id, seq, None, summary.window_end,
                            time.time(), mode="processing_error",
                        )
                        logger.exception("on_update failed for %s seq=%s — continuing", url, seq)
        except Exception as exc:
            logger.exception("window processing failed for %s — continuing", url)
