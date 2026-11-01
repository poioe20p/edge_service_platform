"""ZMQ publisher for Tier 1 coordinator-state snapshots.

Mirrors the topology PUB pattern in :mod:`sdn_controller.topology.topology`:
each controller binds one PUB socket on a per-container port (configured via
``COORDINATOR_STATE_PUB_PORT`` env var) and emits one frame per telemetry
window, after :meth:`PromotionCoordinator.evaluate` has run.

Frames are JSON-encoded with shape::

    {
        "network_id":  "lan1",
        "window_end":  1777064910.123,
        "owners":      {
            "<owner_lan>": {
                "state":                 "NONE" | "SPAWNING" | "ACTIVE" | "DRAINING",
                "breach_ring_filled":    int,
                "breach_ring_capacity":  int,
                "cooldown_remaining_s":  float,
                "hot_collections":       [str, ...],
                "hot_doc_total":         int,
                "container":             str | null,
            },
            ...
        }
    }

Consumed by ``source/scripts/testing/collect_resource_stats.py`` to enrich
``resource_stats.csv`` with coordinator-state columns.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass

import zmq

logger = logging.getLogger("os_ken.selective_sync.state_publisher")


@dataclass
class Tier1OwnerState:
    """Snapshot of :class:`PromotionCoordinator` state for one ``owner_lan``.

    Emitted each telemetry window for observability. Absent on baseline runs
    (``SS_ENABLED=0``) since the coordinator never populates ``_by_owner``.
    """
    state: str
    breach_ring_filled: int
    breach_ring_capacity: int
    cooldown_remaining_s: float
    hot_collections: tuple[str, ...]
    hot_doc_total: int
    container: str | None


class CoordinatorStatePublisher:
    """Lazy ZMQ PUB wrapper. No-op when ``COORDINATOR_STATE_PUB_PORT=0``.

    Bound on ``tcp://*:<port>`` so the host-network collector can subscribe
    via ``tcp://127.0.0.1:<port>``.
    """

    def __init__(self, port: int | None = None) -> None:
        if port is None:
            port = int(os.environ.get("COORDINATOR_STATE_PUB_PORT", "0"))
        self._port = port
        self._ctx: zmq.Context | None = None
        self._socket: zmq.Socket | None = None

        if self._port <= 0:
            logger.info(
                "coordinator-state PUB disabled (COORDINATOR_STATE_PUB_PORT=%d)",
                self._port,
            )
            return

        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.PUB)
        self._socket.bind(f"tcp://*:{self._port}")
        logger.info("coordinator-state PUB bound on tcp://*:%d", self._port)

    def publish(self, network_id: str, window_end: float,
                snapshot: dict[str, Tier1OwnerState],
                consumed_at: float) -> None:
        """Emit one frame. No-op if the publisher is disabled."""
        if self._socket is None:
            return
        payload = {
            "network_id": network_id,
            "window_end": window_end,
            "consumed_at": consumed_at,
            "owners":     {k: asdict(v) for k, v in snapshot.items()},
        }
        try:
            self._socket.send_string(json.dumps(payload))
        except zmq.ZMQError:
            logger.exception("coordinator-state PUB send failed")
