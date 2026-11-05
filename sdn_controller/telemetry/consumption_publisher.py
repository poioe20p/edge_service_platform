"""ZMQ publisher for the telemetry-consumption instant of each window.

Mirrors the topology PUB pattern in :mod:`sdn_controller.topology.topology`:
each controller binds one PUB socket on a per-container port (configured via
``COORDINATOR_STATE_PUB_PORT``) and emits one frame per telemetry window,
after the window has been evaluated.

Frames are JSON-encoded with shape::

    {
        "network_id":  "lan1",
        "window_end":  1777064910.123,
        "consumed_at": 1777064910.456
    }

Consumed by ``scripts/testing/collect_resource_stats.py`` to stamp
``consumed_at`` on the ``resource_stats.csv`` row that the same window
produced, which is how the analysis separates the instant a summary was
consumed from the instant the window closed.
"""
from __future__ import annotations

import json
import logging
import os

import zmq

logger = logging.getLogger("os_ken.telemetry.consumption_publisher")


class ConsumptionPublisher:
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
                "consumption PUB disabled (COORDINATOR_STATE_PUB_PORT=%d)",
                self._port,
            )
            return

        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.PUB)
        self._socket.bind(f"tcp://*:{self._port}")
        logger.info("consumption PUB bound on tcp://*:%d", self._port)

    def publish(self, network_id: str, window_end: float,
                consumed_at: float) -> None:
        """Emit one frame. No-op if the publisher is disabled."""
        if self._socket is None:
            return
        payload = {
            "network_id": network_id,
            "window_end": window_end,
            "consumed_at": consumed_at,
        }
        try:
            self._socket.send_string(json.dumps(payload))
        except zmq.ZMQError:
            logger.exception("consumption PUB send failed")
