"""Stateless reducers over :class:`TelemetrySummary` for Tier 1 promotion.

No Mongo, no I/O. Pure functions the consumer-side ``PromotionCoordinator``
composes on every telemetry window to decide whether a given
``(owner_lan, collection)`` warrants spawning a Tier 1 selective-sync node
in the local LAN.
"""
from __future__ import annotations

import os
from collections import Counter

from sdn_controller.telemetry.models import TelemetrySummary


# Duplicated in platform_cache.py and aggregator.py (separately-deployed images).
_WRITE_OPS: frozenset[str] = frozenset({
    "insert_one", "insert_many",
    "update_one", "update_many", "replace_one",
    "delete_one", "delete_many",
    "bulk_write",
    "find_one_and_update", "find_one_and_replace", "find_one_and_delete",
})


# TAU_DADOS_MS is an env-var knob on the edge server (app.py); the controller reads
# the same env var so one deployment value drives both sides.
TAU_DADOS_MS: float = float(os.environ.get("TAU_DADOS_MS", "65"))


def merge_edge_access(summary: TelemetrySummary,
                      top_n: int) -> dict[tuple[str, str], dict]:
    """Fold every edge server's ``access`` list into one (owner_lan, coll) view.

    Scoping: by the time this runs the ``summary`` has already been filtered
    to this controller's consumer LAN by ``_on_telemetry_update``, so every
    entry in ``summary.servers`` is a local consumer-side edge. The
    ``owner_lan`` on each ``access`` entry still matters — it's the *owning*
    LAN of the data, which may be remote.
    """
    out: dict[tuple[str, str], dict] = {}
    for server in summary.servers.values():
        for stat in server.access:
            key = (stat.owner_lan, stat.collection)
            agg = out.setdefault(
                key, {"total": 0, "xregion": 0, "docs": Counter()},
            )
            agg["total"] += stat.total_hits
            agg["xregion"] += stat.cross_region_hits
            agg["docs"].update(dict(stat.top_docs))
    for agg in out.values():
        agg["top_docs"] = agg["docs"].most_common(top_n)
        del agg["docs"]
    return out


def breach_this_window(summary: TelemetrySummary, owner_lan: str) -> bool:
    """Single-window observation: True if **any** edge in this LAN has
    ``t_db_p95_ms_per_lan[owner_lan] > TAU_DADOS_MS``.

    Intentionally stateless — the M-of-N sliding window that turns this
    into a promotion decision lives on ``PromotionCoordinator._Entry``.
    Mirrors how compute / storage scale-up observe per-window overload and
    debounce M-of-N on the ``ScalingPolicy`` state rather than from the raw
    signal.
    """
    return any(
        srv.t_db_p95_ms_per_lan.get(owner_lan, 0.0) > TAU_DADOS_MS
        for srv in summary.servers.values()
    )


def write_ratio(summary: TelemetrySummary, owner_lan: str, coll: str) -> float:
    """Fraction of ops on ``(owner_lan, coll)`` that are writes this window.

    Backs the coordinator's write-heavy guard (``SS_WRITE_RATIO_MAX``):
    Tier 1 replicates reads only, so promoting a write-heavy collection
    pays full cost for little benefit.
    """
    reads = writes = 0
    for srv in summary.servers.values():
        by_coll = srv.op_counters.get(owner_lan, {}).get(coll, {})
        for op, n in by_coll.items():
            if op in _WRITE_OPS:
                writes += n
            else:
                reads += n
    total = reads + writes
    return (writes / total) if total else 0.0


def total_reads(summary: TelemetrySummary, owner_lan: str, coll: str) -> int:
    """Absolute read count across all edges this window — floor for the
    cross-region-ratio sanity check (``SS_MIN_READS_PER_WINDOW``)."""
    return sum(
        n
        for srv in summary.servers.values()
        for op, n in srv.op_counters.get(owner_lan, {}).get(coll, {}).items()
        if op not in _WRITE_OPS
    )
