"""Map window timestamps to phase names using run phases or phases_snapshot.json."""
from __future__ import annotations

from .loader import PhaseSpec


def phase_for_ts(ts: float, t0: float, phases: list[PhaseSpec]) -> str:
    """Return the phase name for a given window_end timestamp.

    Falls back to ``"unknown"`` when *phases* is empty or *ts* is before
    *t0*.
    """
    if not phases:
        return "unknown"
    offset = ts - t0
    if offset < 0:
        return phases[0].name
    cumulative = 0.0
    for p in phases:
        cumulative += p.duration_s
        if offset <= cumulative:
            return p.name
    return phases[-1].name


def phase_boundaries(t0: float, phases: list[PhaseSpec]) -> list[tuple[str, float, float]]:
    """Return list of (phase_name, start_ts, end_ts) tuples.

    Useful for drawing shaded regions on a time-axis plot.
    """
    result: list[tuple[str, float, float]] = []
    current = t0
    for p in phases:
        result.append((p.name, current, current + p.duration_s))
        current += p.duration_s
    return result
