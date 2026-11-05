"""Sampled-push telemetry source (RQ1 arm D ? the missing 2x2 cell).

Delivers every ``SAMPLE_EVERY``-th completed window immediately (sub-second
delay) from the aggregator's durable window log, dropping the intermediate
windows. This completes the RQ1 completeness x info-age factorial:

    Arm A  event_preserving      fresh + complete
    Arm B  delayed               stale  + complete
    Arm C  poll (latest_state)   stale  + lossy
    Arm D  sampled_push          fresh  + lossy   <-- this source

Semantics (inherits the pull loop from :class:`EventPreservingTelemetrySource`,
adds a sampling gate):
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
poll (newest-of-trio) ? that difference is intentional and pre-registered.
"""

import logging

from .event_preserving_source import EventPreservingTelemetrySource

logger = logging.getLogger("os_ken.telemetry.sampled_push_source")


class SampledPushTelemetrySource(EventPreservingTelemetrySource):
    """In-order pull + immediate delivery of every Nth window (fresh+lossy)."""

    def __init__(self, endpoints: list[str], on_update=None,
                 poll_interval_s: float = 0.5, sample_every: int = 3) -> None:
        super().__init__(
            endpoints,
            on_update=on_update,
            poll_interval_s=poll_interval_s,
            delivery_mode="sampled_push",
        )
        self._sample_every = max(1, int(sample_every))
        self._seen_by_url: dict[str, int] = {}
        logger.info("sampled_push: sample_every=%d", self._sample_every)

    def _accepts_window(self, url: str) -> bool:
        """Increment the per-URL observed-window counter and decide delivery.

        Pure function of internal state ? unit-testable without the network.
        Delivers the 1st, (SAMPLE_EVERY+1)-th, (2*SAMPLE_EVERY+1)-th, ... window.
        """
        seen = self._seen_by_url.get(url, 0) + 1
        self._seen_by_url[url] = seen
        return (seen - 1) % self._sample_every == 0
