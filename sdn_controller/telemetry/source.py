from abc import ABC, abstractmethod

from .models import TelemetrySummary


class TelemetryEventSource(ABC):
    """Transport-agnostic interface for telemetry state access.

    All transport-specific configuration (endpoints, credentials, etc.) is
    handled in each implementation's __init__. The controller only ever
    calls start() once at startup, then get_latest() on demand.
    """

    @abstractmethod
    def start(self) -> None:
        """Begin retrieving summaries in the background.

        For ZMQ (push): spawns the daemon receive thread.
        For HTTP (poll): spawns the greenthread that polls aggregator endpoints.
        Called once at controller startup.
        """

    @abstractmethod
    def get_latest(self, network_id: str) -> TelemetrySummary | None:
        """Return the most recently retrieved summary for network_id, or None
        if no summary has been retrieved yet.
        """
