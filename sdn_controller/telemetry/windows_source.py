"""Shared in-order pull loop for the HTTP window-log telemetry sources.

The aggregator keeps every completed window in a durable JSONL log and serves
it over ``GET /windows?after_seq=<n>&limit=1``.  The three RQ1 delivery arms
that read that log differ only in *when* a pulled window reaches the
controller:

    event_preserving   deliver immediately          (fresh + complete)
    sampled_push       deliver every Nth window     (fresh + lossy)
    delayed            hold, release at window_end + DELAY_S
                                                    (stale + complete)

``WindowsTelemetrySource`` owns the parts they share: the pull loop, sequence
tracking, defensive gap recovery, delivery-log recording, the acknowledgement
and the optional ``on_update`` callback.  A subclass decides delivery by
overriding ``_accepts_window`` (sampling) and/or ``_on_window`` (deferral).

Sequence validation + defensive gap recovery: the log is the single source of
truth, so in-order pulls cannot lose windows.  If the log reports a requested
``after_seq`` has aged out (``410``), the gap is recorded in the delivery log
and the source advances to the first available window — it never blocks.
"""

import logging
import time

import requests
from os_ken.lib import hub

from .delivery_log import DeliveryLog, send_ack
from .models import TelemetrySummary
from .source import TelemetryEventSource

logger = logging.getLogger("os_ken.telemetry.windows_source")


class WindowsTelemetrySource(TelemetryEventSource):
    """Pulls completed windows in order and hands each one to the delivery hook."""

    def __init__(
        self,
        endpoints: list[str],
        on_update=None,
        poll_interval_s: float = 0.5,
        delivery_mode: str = "event_preserving",
    ) -> None:
        """
        endpoints:       list of HTTP base URLs to aggregator window-log endpoints.
                         Example: ["http://10.0.0.5:5558", "http://10.0.1.5:5558"]
        poll_interval_s: seconds to sleep between pull cycles.
        on_update:       optional callable(summary: TelemetrySummary).
        delivery_mode:   mode tag written to the delivery log for this arm.
        """
        self._endpoints = endpoints
        self._poll_interval_s = poll_interval_s
        self._on_update = on_update
        self._latest: dict[str, TelemetrySummary] = {}
        # URL-keyed: each aggregator serves exactly one network.
        self._last_seq_by_url: dict[str, int] = {}
        self._network_id_by_url: dict[str, str] = {}
        self._delivery_log = DeliveryLog(mode=delivery_mode)

    def start(self) -> None:
        logger.info("%s starting: endpoints=%s",
                    type(self).__name__, self._endpoints)
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

    def _accepts_window(self, url: str) -> bool:
        """Sampling hook: every observed window is delivered by default."""
        return True

    def _on_window(self, url: str, seq: int, win: dict) -> None:
        """Delivery hook: hand the window to the consumer now."""
        self._deliver_summary(url, TelemetrySummary.model_validate(win), time.time())

    def _deliver_summary(self, url: str, summary: TelemetrySummary,
                         delivered_at: float,
                         release_ts: float | None = None) -> None:
        """Cache, log, acknowledge and dispatch one summary."""
        network_id = summary.network_id
        # Cache only real (non-empty) summaries — empty windows would corrupt
        # WSM cost inputs.
        if summary.servers or summary.storage_servers:
            self._latest[network_id] = summary
        self._delivery_log.record(
            network_id, summary.window_seq, summary.window_id,
            summary.window_end, delivered_at, release_ts=release_ts,
        )
        send_ack(url, summary.window_id, summary.window_seq, delivered_at)
        if self._on_update is not None:
            try:
                self._on_update(summary)
            except Exception:
                # Delivery already recorded; mark the consumer-side failure so
                # the analyzer does not count the window as consumed.
                self._delivery_log.record(
                    network_id, summary.window_seq, None, summary.window_end,
                    delivered_at, mode="processing_error",
                )
                logger.exception("on_update failed for %s seq=%s — continuing",
                                 url, summary.window_seq)

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
                # last_seq ALWAYS advances — the window has left the durable
                # log, so it is never re-pulled, delivered or not.
                self._last_seq_by_url[url] = seq
                self._network_id_by_url[url] = win.get("network_id", "")
                if not self._accepts_window(url):
                    logger.debug("%s dropping %s seq=%s",
                                 type(self).__name__, url, seq)
                    continue
                self._on_window(url, seq, win)
        except Exception:
            logger.exception("window processing failed for %s — continuing", url)
