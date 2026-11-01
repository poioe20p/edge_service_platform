import logging

import eventlet.tpool
import zmq
from os_ken.lib import hub

from .models import TelemetrySummary
from .source import TelemetryEventSource

logger = logging.getLogger('os_ken.telemetry.zmq_source')


class ZmqTelemetrySource(TelemetryEventSource):
    """Push-based telemetry source — receives summaries via ZMQ PUB/SUB.

    The aggregator binds a PUB socket on port 5556 and publishes each
    TelemetrySummary at window close. This source connects a SUB socket
    to each aggregator endpoint and receives JSON summaries in a
    background greenthread via zmq.green (eventlet-compatible ZMQ).

    For the poll-based alternative, see PollingTelemetrySource.
    """

    def __init__(self, endpoints: list[str], on_update=None, on_topology_update=None) -> None:
        """
        endpoints:           list of PUB addresses to subscribe to (aggregators + peer controllers).
                             Example: ["tcp://192.168.1.1:5556", "tcp://10.0.1.X:5557"]
        on_update:           optional callable(summary: TelemetrySummary) for telemetry messages.
        on_topology_update:  optional callable(data: dict) for topology snapshot messages.
        """
        self._ctx = zmq.Context()
        self._socket = self._ctx.socket(zmq.SUB)
        for ep in endpoints:
            self._socket.connect(ep)
            logger.info("telemetry: will subscribe to %s", ep)
        self._socket.setsockopt(zmq.SUBSCRIBE, b"")
        self._latest: dict[str, TelemetrySummary] = {}
        self._on_update = on_update
        self._on_topology_update = on_topology_update

    def start(self) -> None:
        """Spawn the background greenthread that receives ZMQ-pushed summaries."""
        logger.info("telemetry receive loop starting")
        hub.spawn(self._receive_loop)

    def get_latest(self, network_id: str) -> TelemetrySummary | None:
        return self._latest.get(network_id)

    def _receive_loop(self) -> None:
        while True:
            try:
                data = eventlet.tpool.execute(self._socket.recv_json)
                logger.debug("message received: type=%s", data.get("type", "telemetry") if isinstance(data, dict) else "unknown")
                if isinstance(data, dict) and data.get("type") == "topology":
                    if self._on_topology_update is not None:
                        self._on_topology_update(data)
                    logger.info("telemetry topology update: %s", data)
                else:
                    summary = TelemetrySummary.model_validate(data)
                    # Only cache real summaries — mini-summaries (drain_complete
                    # pass-throughs) are empty and would corrupt WSM cost inputs.
                    if summary.servers or summary.storage_servers:
                        self._latest[summary.network_id] = summary
                    if self._on_update is not None:
                        self._on_update(summary)
                    if summary.domain_summary is not None:
                        logger.info(
                            "telemetry update network=%s avg_proc_time_ms=%.1f avg_db_access_ms=%.1f",
                            summary.network_id,
                            summary.domain_summary.avg_time_proc_ms,
                            summary.domain_summary.avg_time_db_ms,
                        )
                    else:
                        logger.info("telemetry mini-summary network=%s control_events=%s",
                                    summary.network_id, summary.control_events)
                    logger.debug(
                        "telemetry full summary: %s",
                        summary.model_dump_json(indent=2)
                    )
            except Exception as exc:  
                logger.warning("telemetry receive error: %s", exc)
