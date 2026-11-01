"""Delivery log + acknowledgement client (RQ1 telemetry delivery semantics).

Shared by ``EventPreservingTelemetrySource``, ``DelayedEventPreservingTelemetrySource``
and ``PollingTelemetrySource``. Writes one CSV row per delivered window so the
RQ1 analyzer can compute delivered/missed windows, delivery delay, and
information age. Also provides the best-effort producer-side acknowledgement
via ``POST /ack`` (the thesis "delivery acknowledgement" requirement).

``release_ts`` equals ``delivery_ts`` for non-delayed modes and holds the actual
release time for the delayed arm.

Analyzer conventions:
- A row with a non-empty ``window_id`` is a real delivered window; dedup by
  ``window_id``.
- A row with ``window_id=None`` is NOT a delivered window. ``mode``
  distinguishes ``gap_recovery`` (window aged out / never delivered) from
  ``processing_error`` (delivered from the durable log but the controller's
  ``on_update`` raised before consuming it).
"""

import csv
import logging
import os
import threading

import requests

logger = logging.getLogger("os_ken.telemetry.delivery_log")

_FIELDS = [
    "network_id",
    "window_seq",
    "window_id",
    "window_end",
    "delivery_ts",
    "delay_s",
    "mode",
    "release_ts",
]


class DeliveryLog:
    """Thread-safe append-only CSV log of delivered telemetry windows."""

    def __init__(self, path: str | None = None, mode: str = "unknown") -> None:
        self._path = path or os.environ.get(
            "DELIVERY_LOG_PATH", "/tmp/telemetry_delivery_log.csv")
        self._mode = mode
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self._open()

    def _open(self) -> None:
        try:
            self._file = open(self._path, "a", newline="")
            self._writer = csv.DictWriter(self._file, fieldnames=_FIELDS)
            if self._file.tell() == 0:
                self._writer.writeheader()
                self._file.flush()
        except OSError as exc:
            logger.warning("delivery log unavailable at %s: %s", self._path, exc)
            self._file = None
            self._writer = None

    def record(self, network_id: str, window_seq: int | None,
               window_id: str | None, window_end: float,
               delivery_ts: float, mode: str | None = None,
               release_ts: float | None = None) -> None:
        row = {
            "network_id": network_id,
            "window_seq": window_seq if window_seq is not None else "",
            "window_id": window_id or "",
            "window_end": f"{window_end:.3f}",
            "delivery_ts": f"{delivery_ts:.3f}",
            "delay_s": f"{max(0.0, delivery_ts - window_end):.3f}",
            "mode": mode or self._mode,
            "release_ts": (f"{release_ts:.3f}" if release_ts is not None
                           else f"{delivery_ts:.3f}"),
        }
        with self._lock:
            if self._writer is None:
                self._open()
            if self._writer is not None:
                try:
                    self._writer.writerow(row)
                    self._file.flush()
                except OSError as exc:
                    logger.warning("delivery log write failed: %s", exc)


def send_ack(base_url: str, window_id: str | None, window_seq: int | None,
             delivered_at: float) -> None:
    """Best-effort POST /ack to the aggregator that produced the window."""
    if not window_id or window_seq is None:
        return
    try:
        requests.post(
            f"{base_url}/ack",
            json={
                "window_id": window_id,
                "window_seq": window_seq,
                "delivered_at": delivered_at,
            },
            timeout=2.0,
        )
    except requests.RequestException as exc:
        logger.debug("ack failed for %s seq=%s: %s", base_url, window_seq, exc)
