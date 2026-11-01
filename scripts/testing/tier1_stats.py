"""Tier 1 column extraction helpers for ``collect_resource_stats.py``.

Pure functions over the JSON payloads produced by the local_state_server
aggregator (``servers``/``storage_servers``) and the SDN controller's
coordinator-state PUB (``owners`` dict). Centralised here so the writer
loop in :mod:`collect_resource_stats` stays focused on I/O.

Schema references:
* :class:`source.sdn_controller.telemetry.models.ServerSummary` —
  ``access``, ``t_db_p95_ms_per_lan``, ``op_counters``.
* :class:`source.sdn_controller.telemetry.models.StorageServerSummary` —
  ``selective_sync_per_collection``.
* :class:`source.sdn_controller.selective_sync.state_publisher.Tier1OwnerState`.
"""
from __future__ import annotations

TIER1_TELEMETRY_COLUMNS = [
    "total_reads",
    "cross_region_reads",
    "cross_region_ratio",
    "max_per_owner_coll_xratio",
    "t_db_p95_ms_owner_lan",
    "t_db_p95_ms_peer_lan",
    "top_hot_doc_hits",
    "tier1_reporting_count",
    "tier1_active_count",
    "avg_tier1_lag_s",
    "max_tier1_resume_token_age_s",
    "tier1_hot_doc_total",
]

TIER1_COORD_COLUMNS = [
    "coord_state_owner_lan",
    "coord_breach_fill_pct",
    "coord_cooldown_remaining_s",
    "coord_hot_doc_total",
    "tier1_lifecycle_active_count",
]

TIER1_ALL_COLUMNS = TIER1_TELEMETRY_COLUMNS + TIER1_COORD_COLUMNS


def peer_lan(my_lan: str) -> str:
    """``"lan1"`` → ``"lan2"`` and vice versa."""
    return "lan2" if my_lan == "lan1" else "lan1"


def sum_total_reads(servers: dict) -> int:
    """Sum of ``find`` + ``find_one`` across all servers, owner_lans, and
    collections. Reflects the read-side workload feeding the footprint gate."""
    total = 0
    for srv in servers.values():
        op_counters = srv.get("op_counters") or {}
        for by_owner in op_counters.values():
            for by_coll in by_owner.values():
                for op_type, count in by_coll.items():
                    if op_type in ("find", "find_one"):
                        total += int(count or 0)
    return total


def sum_cross_region_reads(servers: dict) -> int:
    """Sum of ``cross_region_hits`` across all ``access`` entries on all servers."""
    total = 0
    for srv in servers.values():
        for entry in srv.get("access") or []:
            total += int(entry.get("cross_region_hits", 0) or 0)
    return total


def max_per_owner_coll_xratio(servers: dict) -> float:
    """Maximum ``cross_region_hits / total_hits`` across all
    ``(owner_lan, collection)`` access entries reported this window.

    This mirrors the actual Tier 1 promotion gate (per-collection ratio vs
    ``_SS_PROMOTION_CROSS_REGION_THRESHOLD``), so it is the correct
    diagnostic for whether the gate would pass. The aggregate
    ``cross_region_ratio`` column is diluted by same-region collections
    (e.g. ``user_profiles``) and underestimates the real signal. Returns
    0.0 when no access entry has been reported.
    """
    best = 0.0
    for srv in servers.values():
        for entry in srv.get("access") or []:
            total = int(entry.get("total_hits", 0) or 0)
            if total <= 0:
                continue
            x = int(entry.get("cross_region_hits", 0) or 0)
            ratio = x / total
            if ratio > best:
                best = ratio
    return best


def max_p95_for_lan(servers: dict, lan: str) -> float:
    """Max p95 (ms) across all servers for the given owner_lan key.

    The breach gate fires on *any* server's p95 > τ, so max is the right
    aggregator. Returns 0.0 when no server has reported the key yet.
    """
    best = 0.0
    for srv in servers.values():
        p95 = (srv.get("t_db_p95_ms_per_lan") or {}).get(lan)
        if p95 is None:
            continue
        try:
            best = max(best, float(p95))
        except (TypeError, ValueError):
            continue
    return best


def top_hot_doc_hits(servers: dict) -> int:
    """Hottest single ``(owner_lan, collection, doc)`` count this window."""
    best = 0
    for srv in servers.values():
        for entry in srv.get("access") or []:
            for pair in entry.get("top_docs") or []:
                # tolerate both [doc, hits] and {"doc": ..., "hits": ...}
                hits = pair[1] if isinstance(pair, list) and len(pair) >= 2 else 0
                try:
                    best = max(best, int(hits or 0))
                except (TypeError, ValueError):
                    continue
    return best


def tier1_storage_aggregate(storage_servers: dict) -> tuple[int, float, float, int]:
    """Aggregate Tier 1 container health across storage_servers.

    Returns ``(reporting_count, avg_lag_s, max_resume_token_age_s, hot_doc_total)``.
    A storage server counts toward the reporting-side Tier 1 supply metric iff its
    ``selective_sync_per_collection`` is a non-empty dict. Compute / full-replica
    storage carries ``null`` here and is ignored. ``tier1_active_count`` is kept
    as a deprecated CSV alias for backward compatibility and receives the same
    value as ``tier1_reporting_count``.
    """
    reporting = 0
    lags: list[float] = []
    max_token_age = 0.0
    hot_total = 0

    for s in storage_servers.values():
        per_coll = s.get("selective_sync_per_collection")
        if not per_coll:
            continue
        reporting += 1
        for coll_stats in per_coll.values():
            try:
                lags.append(float(coll_stats.get("lag_s", 0.0) or 0.0))
            except (TypeError, ValueError):
                pass
            try:
                max_token_age = max(
                    max_token_age,
                    float(coll_stats.get("resume_token_age_s", 0.0) or 0.0),
                )
            except (TypeError, ValueError):
                pass
            try:
                hot_total += int(coll_stats.get("hot_doc_count", 0) or 0)
            except (TypeError, ValueError):
                pass

    avg_lag = (sum(lags) / len(lags)) if lags else 0.0
    return reporting, avg_lag, max_token_age, hot_total


def coord_row_fields(
    coord_state_by_lan: dict,
    my_lan: str,
) -> tuple[str, float, float, int]:
    """Extract ``(state, breach_fill_pct, cooldown_remaining_s, hot_doc_total)``
    from the latest coordinator frame for ``my_lan``.

    The frame is keyed by the **publisher** LAN (= consumer-side controller
    that owns the coordinator). Within ``owners`` we surface the entry for
    ``my_lan`` if present — i.e. *this LAN as owner* viewed by the peer
    consumer. Returns sensible defaults (``"NONE"``, 0.0, 0.0, 0) when no
    snapshot has been received or the owner_lan is absent.
    """
    if not coord_state_by_lan:
        return "NONE", 0.0, 0.0, 0
    owners = coord_state_by_lan.get("owners") or {}
    entry = owners.get(my_lan)
    if not entry:
        return "NONE", 0.0, 0.0, 0
    capacity = int(entry.get("breach_ring_capacity", 0) or 0)
    filled   = int(entry.get("breach_ring_filled",   0) or 0)
    fill_pct = (100.0 * filled / capacity) if capacity > 0 else 0.0
    return (
        str(entry.get("state", "NONE")),
        fill_pct,
        float(entry.get("cooldown_remaining_s", 0.0) or 0.0),
        int(entry.get("hot_doc_total", 0) or 0),
    )


def build_tier1_row(summary: dict, coord_state_by_lan: dict) -> dict:
    """Return a dict containing every column in :data:`TIER1_ALL_COLUMNS`.

    ``summary`` is the per-window aggregator payload. ``coord_state_by_lan``
    is the most recent peer-controller frame for this summary's owner LAN (or
    an empty dict if none received yet).
    """
    servers = summary.get("servers", {}) or {}
    storage = summary.get("storage_servers", {}) or {}
    my_lan  = summary.get("network_id", "")
    other   = peer_lan(my_lan) if my_lan else ""

    total_reads        = sum_total_reads(servers)
    cross_region_reads = sum_cross_region_reads(servers)
    cross_region_ratio = (cross_region_reads / total_reads) if total_reads else 0.0
    per_coll_xratio    = max_per_owner_coll_xratio(servers)

    p95_owner = max_p95_for_lan(servers, my_lan) if my_lan else 0.0
    p95_peer  = max_p95_for_lan(servers, other)  if other  else 0.0

    reporting_count, avg_lag, max_token_age, hot_total = tier1_storage_aggregate(storage)

    coord_state, fill_pct, cooldown_s, coord_hot = coord_row_fields(
        coord_state_by_lan, my_lan,
    )
    lifecycle_active = 1 if coord_state == "ACTIVE" else 0

    return {
        "total_reads":                  total_reads,
        "cross_region_reads":           cross_region_reads,
        "cross_region_ratio":           round(cross_region_ratio, 4),
        "max_per_owner_coll_xratio":    round(per_coll_xratio, 4),
        "t_db_p95_ms_owner_lan":        round(p95_owner, 2),
        "t_db_p95_ms_peer_lan":         round(p95_peer, 2),
        "top_hot_doc_hits":             top_hot_doc_hits(servers),
        "tier1_reporting_count":        reporting_count,
        "tier1_active_count":           reporting_count,
        "avg_tier1_lag_s":              round(avg_lag, 4),
        "max_tier1_resume_token_age_s": round(max_token_age, 4),
        "tier1_hot_doc_total":          hot_total,
        "coord_state_owner_lan":        coord_state,
        "coord_breach_fill_pct":        round(fill_pct, 2),
        "coord_cooldown_remaining_s":   round(cooldown_s, 2),
        "coord_hot_doc_total":          coord_hot,
        "tier1_lifecycle_active_count": lifecycle_active,
    }
