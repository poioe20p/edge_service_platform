"""Tier 1 selective-sync subsystem (controller side).

Consumes the per-edge access / op-mix / p95-per-LAN fields on
:class:`ServerSummary` (populated by the local_state_server aggregator from
per-request events enriched by the edge-server's ``platform_cache`` wrapper)
and provides stateless reducers that the ``PromotionCoordinator`` uses to
decide whether a given ``(owner_lan, collection)`` should be promoted to
Tier 1.

See ``docs/operation/elasticy_manager/implementation/tier1_selective_sync/``.
"""
from .hotness import (
    TAU_DADOS_MS,
    _WRITE_OPS,
    breach_this_window,
    merge_edge_access,
    total_reads,
    write_ratio,
)

__all__ = [
    "TAU_DADOS_MS",
    "_WRITE_OPS",
    "breach_this_window",
    "merge_edge_access",
    "total_reads",
    "write_ratio",
]
