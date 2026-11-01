"""Bounded in-memory support state for local edge request activity."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import threading
from typing import Any


@dataclass(frozen=True)
class LocalRequestEvent:
    timestamp_epoch: float
    request_kind: str
    content_id: str | None
    user_id: str
    latency_ms: float
    served_from_tier: int
    tier1_hit_ratio: float
    tier1_eligible_reads: int
    relevance: str
    status: str
    tags: tuple[str, ...]


class LocalRequestState:
    """Thread-safe bounded request activity store for local support analytics."""

    def __init__(self, max_events: int, per_content_window: int) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if per_content_window < 1:
            raise ValueError("per_content_window must be >= 1")

        self._max_events = max_events
        self._events: deque[LocalRequestEvent] = deque()
        self._by_content: defaultdict[str, deque[LocalRequestEvent]] = defaultdict(
            lambda: deque(maxlen=per_content_window)
        )
        self._lock = threading.Lock()

    def record(self, event: LocalRequestEvent) -> None:
        with self._lock:
            if len(self._events) == self._max_events:
                oldest = self._events.popleft()
                if oldest.content_id:
                    per_content = self._by_content.get(oldest.content_id)
                    if per_content and per_content[0] is oldest:
                        per_content.popleft()
                        if not per_content:
                            del self._by_content[oldest.content_id]

            self._events.append(event)
            if event.content_id:
                self._by_content[event.content_id].append(event)

    def recent_for_content(self, content_id: str, limit: int) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        with self._lock:
            snapshot = list(self._by_content.get(content_id, ()))
        return [self._snapshot(event) for event in snapshot[-limit:]]

    def events_since(self, cutoff_epoch: float) -> list[dict[str, Any]]:
        return self.events_since_with_truncation(cutoff_epoch)[0]

    def events_since_with_truncation(self, cutoff_epoch: float) -> tuple[list[dict[str, Any]], bool]:
        with self._lock:
            truncated = (
                len(self._events) == self._max_events
                and bool(self._events)
                and self._events[0].timestamp_epoch >= cutoff_epoch
            )
            snapshot = [event for event in self._events if event.timestamp_epoch >= cutoff_epoch]
        return [self._snapshot(event) for event in snapshot], truncated

    @staticmethod
    def _snapshot(event: LocalRequestEvent) -> dict[str, Any]:
        return {
            "timestamp": event.timestamp_epoch,
            "request_kind": event.request_kind,
            "content_id": event.content_id,
            "user_id": event.user_id,
            "latency_ms": event.latency_ms,
            "served_from_tier": event.served_from_tier,
            "tier1_hit_ratio": event.tier1_hit_ratio,
            "tier1_eligible_reads": event.tier1_eligible_reads,
            "relevance": event.relevance,
            "status": event.status,
            "tags": list(event.tags),
        }