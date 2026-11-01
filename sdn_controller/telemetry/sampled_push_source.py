"""Sampled-push telemetry source (RQ1 arm D — the missing 2x2 cell).

Delivers every ``SAMPLE_EVERY``-th completed window immediately (sub-second
delay) from the aggregator's durable window log, dropping the intermediate
windows. This completes the RQ1 completeness x info-age factorial:

    Arm A  event_preserving      fresh + complete
    Arm B  delayed               stale  + complete
    Arm C  poll (latest_state)   stale  + lossy
    Arm D  sampled_push          fresh  + lossy   <-- this source

Semantics (mirrors :class:`EventPreservingTelemetrySource`, adds a sampling
gate):
- In-order pull from the durable log; ``last_seq`` advances for EVERY window so
  the source never falls behind and never blocks.
- A per-URL counter counts every observed window; the counter-th window with
  ``(seen - 1) % SAMPLE_EVERY == 0`` is delivered (cache + delivery log + ack +
  ``on_update``). All other windows are dropped: they are NOT recorded in the
  delivery log, and the analyzer computes misses from the window-log universe
  (identical semantics to the poll arm).
- Delivery delay is sub-second (``EVENT_POLL_INTERVAL_S=0.5``), matching Arm A.
- Gap handling is unchanged from the base (aged-out ranges recorded, never
  blocks). ``SAMPLE_EVERY=1`` is byte-identical in behavior to the
  event-preserving reference (regression property).

Limitation (documented): the sampling is deterministic periodic (every Nth
window). The RQ1 workload is constant-rate, so periodicity-aliasing risk with
the demand process is low; the delivered subset differs in pattern from Arm C's
poll (newest-of-trio) — that difference is intentional and pre-registered.
"""

import logging
import time

import requests
from os_ken.lib import hub

from .delivery_log import DeliveryLog, send_ack
from .event_preserving_source import EventPreservingTelemetrySource
from .models import TelemetrySummary
from .source import TelemetryEventSource

logger = logging.getLogger("os_ken.telemetry.sampled_push_source")


class SampledPushTelemetrySource(EventPreservingTelemetrySource):
    """In-order pull + immediate delivery of every Nth window (fresh+lossy)."""

    def __init__(self, endpoints: list[str], on_update=None, on_topology_update=None,
                 poll_interval_s: float = 0.5, sample_every: int = 3,
                 ack: bool = True) -> None:
        super().__init__(
            endpoints,
            on_update=on_update,
            on_topology_update=on_topology_update,
            poll_interval_s=poll_interval_s,
            ack=ack,
        )
        self._sample_every = max(1, int(sample_every))
        self._seen_by_url: dict[str, int] = {}
        # Reuse the base DeliveryLog (avoids a double-open); only the mode tag
        # changes so the analyzer recognizes this arm.
        self._delivery_log._mode = "sampled_push"

    def start(self) -> None:
        logger.info(
            "sampled-push source starting: endpoints=%s sample_every=%d",
            self._endpoints, self._sample_every,
        )
        hub.spawn(self._loop)

    def get_latest(self, network_id: str) -> TelemetrySummary | None:
        return self._latest.get(network_id)

    def _loop(self) -> None:
        while True:
            for url in self._endpoints:
                self._poll_one(url)
            hub.sleep(self._poll_interval_s)

    def _should_deliver(self, url: str) -> bool:
        """Increment the per-URL observed-window counter and decide delivery.

        Pure function of internal state — unit-testable without the network.
        Delivers the 1st, (SAMPLE_EVERY+1)-th, (2*SAMPLE_EVERY+1)-th, ... window.
        """
        seen = self._seen_by_url.get(url, 0) + 1
        self._seen_by_url[url] = seen
        return (seen - 1) % self._sample_every == 0

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
            logger.debug("unexpected status %s from %s", resp.status_code, url)
            return

        # Guarded per-window processing — see EventPreservingTelemetrySource.
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
                # last_seq ALWAYS advances — the window left the durable log, so
                # delivery is authoritative and the source never re-pulls it.
                self._last_seq_by_url[url] = seq
                network_id = win.get("network_id", "")
                self._network_id_by_url[url] = network_id
                if not self._should_deliver(url):
                    # Dropped window: not delivered, not recorded, not acked.
                    # The analyzer computes the miss from the window universe.
                    logger.debug("sampled_push dropping %s seq=%s", url, seq)
                    continue
                summary = TelemetrySummary.model_validate(win)
                # Cache only real (non-empty) summaries — empty windows would
                # corrupt WSM cost inputs (mirrors ZmqTelemetrySource).
                if summary.servers or summary.storage_servers:
                    self._latest[network_id] = summary
                self._delivery_log.record(
                    network_id, seq, win.get("window_id"),
                    summary.window_end, time.time(), mode="sampled_push",
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
