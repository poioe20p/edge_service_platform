"""Delayed event-preserving telemetry source (RQ1 arm 2).

Same reliable in-order delivery as :class:`EventPreservingTelemetrySource`,
but every window is held in a FIFO hold queue and released only when
``now >= window_end + DELAY_S`` ? a fixed, pre-registered delay applied per
window (never a one-time stream offset).

"No burst replay" semantics (precise):
- The delay is a per-window lower bound from each window's own ``window_end``.
  Windows are never batch-released together; steady-state cadence is one
  delivery per ``WINDOW_S``.
- No backward fetch / no backfill: the source only ever advances ``last_seq``
  by one and never replays from the log after a stall.
- Defensive gap policy: pulls are in-order from the durable log so gaps cannot
  occur by construction; if an ``aged_out`` (410) is observed, the gap is
  recorded and the source continues ? it does NOT replay.

If the consumer's ``on_update`` is the bottleneck, due windows back up and
drain one-per-step at their due times; the measured ``delay_s`` exceeds
``DELAY_S`` by the backlog ? that excess is measured, not masked.

The pull loop is inherited; this arm overrides only the delivery hook (hold
instead of deliver) and adds the release loop.
"""

import logging
import time
from collections import deque

from os_ken.lib import hub

from .models import TelemetrySummary
from .windows_source import WindowsTelemetrySource

logger = logging.getLogger("os_ken.telemetry.delayed_source")


class DelayedEventPreservingTelemetrySource(WindowsTelemetrySource):
    """In-order pull + per-window FIFO hold queue with fixed release delay."""

    def __init__(self, endpoints: list[str], on_update=None,
                 delay_s: float = 30.0, poll_interval_s: float = 0.5) -> None:
        super().__init__(
            endpoints,
            on_update=on_update,
            poll_interval_s=poll_interval_s,
            delivery_mode="delayed_event_preserving",
        )
        self._delay_s = delay_s
        self._pending_by_url: dict[str, deque] = {}

    def start(self) -> None:
        logger.info("delayed source starting: endpoints=%s delay=%.1fs",
                    self._endpoints, self._delay_s)
        hub.spawn(self._loop)
        hub.spawn(self._release_loop)

    def _on_window(self, url: str, seq: int, win: dict) -> None:
        """Hold the window; the release loop delivers it at its due time."""
        self._pending_by_url.setdefault(url, deque()).append(
            TelemetrySummary.model_validate(win))

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
                logger.exception("release tick failed ? continuing: %s", exc)
                hub.sleep(self._poll_interval_s)

    def _deliver(self, url: str, summary: TelemetrySummary) -> None:
        """Release one held window: delivery_ts and release_ts are the same."""
        release_ts = time.time()
        self._deliver_summary(url, summary, release_ts, release_ts=release_ts)
