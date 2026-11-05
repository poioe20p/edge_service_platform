"""Backend selection logic: warm-lease claiming, WSM cost functions,
and round-robin tie-breaking for server and storage VIPs.
"""

import hashlib
import math
import time

from .config import (
    _W_CPU, _W_RAM, _W_REQUESTS, _W_HOPS,
    _W_STORAGE_CPU, _W_STORAGE_RAM, _W_STORAGE_CONNECTIONS,
    _W_STORAGE_LAG, _W_STORAGE_HOPS,
    _BACKEND_SELECTION_POLICY,
    logger,
)
from ..scaling_config import _VIP_WARM_SERVER_SECONDS, _VIP_WARM_STORAGE_SECONDS
from ..scaling_config import _BACKEND_SELECTION_SPREAD_EPS
from ..scaling_config import _BACKEND_SELECTION_SPREAD_MIN_BAND
from ..scaling_config import _STORAGE_SELECTION_SPREAD_EPS
from ..scaling_config import _STORAGE_SELECTION_SPREAD_MIN_BAND


def _unknown_stats_default():
    """Return the unknown-stats normalised value for the current policy mode.

    topology_host:      0.0 (best-case  → cold-start herd)
    topology_slowstart: 0.0 (neutral    → penalty handles all deterrence)
    topology_lifecycle: 1.0 (worst-case → warm lease short-circuits WSM anyway;
                          also protects against preferring unmeasured peer backends)
    """
    if _BACKEND_SELECTION_POLICY == "topology_host":
        return 0.0
    if _BACKEND_SELECTION_POLICY == "topology_slowstart":
        return 0.0     # neutral — penalty owns all deterrence
    return 1.0          # topology_lifecycle (default)


def _slowstart_penalty(controller, mac: str, ttl_s: float) -> float:
    """Graduated cost penalty for discovery-time slow-start ramp (RQ2).

    Before discovery: returns 1.0 — the backend is worst-case, effectively
    invisible in practice (will only win traffic via round-robin tie-breaking
    if all other backends are equally penalised).

    After discovery: linear decay from 1.0 at discovery time to 0.0 at
    discovery_time + ttl_s.

    Returns 0.0 if the policy mode is not topology_slowstart, or if the
    ramp period has elapsed.

    Design note — penalty magnitude: the unweighted penalty (range 0–1)
    exceeds the maximum weighted WSM cost sum (0.88 for compute,
    0.90 for storage). This means the penalty is architecturally dominant —
    a backend under slowstart cannot win against any backend with real
    stats until the penalty has decayed substantially. This is intentional:
    the ramp IS the mechanism, not a subtle bias. In a separated system,
    the LB's slow-start weight dominates routing in the same way.
    """
    if _BACKEND_SELECTION_POLICY != "topology_slowstart":
        return 0.0
    discovered = controller._backend_discovery_ts.get(mac)
    if discovered is None:
        return 1.0          # undiscovered → maximum deterrence
    elapsed = time.monotonic() - discovered
    if elapsed >= ttl_s:
        return 0.0
    return 1.0 - (elapsed / ttl_s)


def _claim_warm_backend(
    controller,
    vip_name: str,
    leases: dict,
    pool: dict,
) -> dict | None:
    """Check for and claim any warm lease for a backend in the pool.

    When multiple warm leases are claimable simultaneously, round-robin
    across them: claiming strictly the oldest would starve its siblings."""
    now = time.monotonic()

    with controller._warm_lock:

        expired = [
            mac
            for mac, lease in leases.items()
            if lease.expires_ts <= now
        ]
        for mac in expired:
            leases.pop(mac, None)

        candidates = [
            (mac, lease)
            for mac, lease in leases.items()
            if mac in pool and mac in controller._mac_to_ip
        ]
        if not candidates:
            for mac in expired:
                logger.info("%s warm lease expired: mac=%s", vip_name, mac)
            return None

        mac, lease = candidates[controller._warm_rr_idx % len(candidates)]
        controller._warm_rr_idx += 1
        chosen = pool[mac]

    for expired_mac in expired:
        logger.info("%s warm lease expired: mac=%s", vip_name, expired_mac)

    logger.info(
        "%s warm lease claimed: mac=%s remaining=%.1fs",
        vip_name,
        mac,
        max(lease.expires_ts - now, 0.0),
    )
    return chosen


def select_server(controller, client_mac: str) -> dict | None:
    """Pick the web server with the lowest WSM cost from vip_server_pool.

    Cost_j = w_cpu·(CPU_j/CPU_max) + w_ram·(RAM_j/RAM_max)
           + w_req·(Req_j/Req_max) + w_hops·(Hops_j/Hops_max)

    Spread (`_BACKEND_SELECTION_SPREAD_EPS` > 0): candidates
    within eps of the pool minimum form a band and are distributed across
    clients via a stateless deterministic hash, so one selection burst does
    not funnel every client onto a single backend. A live warm claim joins
    the band instead of monopolising it, and
    `_BACKEND_SELECTION_SPREAD_MIN_BAND` floors the band at the k cheapest
    local eligible units when the eps-band alone is smaller — a single
    strict winner can no longer take a whole burst. With eps = 0 the legacy
    behaviour is preserved exactly (argmin + exact-tie round-robin +
    exclusive warm claim).
    """
    pool = controller.vip_server_pool
    if not pool:
        logger.warning("select_server: pool empty")
        return None

    # Warm lease: only in topology_lifecycle.  The other modes rely on the WSM
    # cost function instead, through the unknown-stats default.
    warm_entry = None
    if _BACKEND_SELECTION_POLICY == "topology_lifecycle":
        warm_entry = _claim_warm_backend(
            controller, "vip_server", controller._warm_server_leases, pool,
        )
        if warm_entry is not None and _BACKEND_SELECTION_SPREAD_EPS <= 0:
            # Warm-lease priority: the claim owns the selection outright.
            return warm_entry

    pool_stats = [controller._server_stats[m] for m in pool if m in controller._server_stats]
    cpu_max = max((s.avg_cpu_percent for s in pool_stats), default=0.0) or 1.0
    ram_max = max((s.avg_ram_used_mb  for s in pool_stats), default=0.0) or 1.0
    req_max = max((s.request_count    for s in pool_stats), default=0)   or 1
    hops_max = max(controller._hop_cache_max, 1)

    best_cost = float("inf")
    tied: list[dict] = []
    scored: list[tuple[float, dict]] = []

    for mac, entry in pool.items():
        stats = controller._server_stats.get(mac)

        _default = _unknown_stats_default()
        cpu_norm = (stats.avg_cpu_percent / cpu_max) if stats else _default
        ram_norm = (stats.avg_ram_used_mb  / ram_max) if stats else _default
        req_norm = (stats.request_count    / req_max) if stats else _default

        if mac in controller.peer_hosts:
            # Peer backend: excluded from the hop cache (see
            # topology._rebuild_hop_cache), so this branch always runs — local
            # hops to the router plus peer hops onward.  Router traversal
            # cannot be bypassed, so a peer-domain candidate is always
            # strictly farther than a local one (2 vs 4 in this topology).
            local_avg = max(controller._avg_hop_count, 1.0)
            peer_avg  = max(controller._peer_avg_hop_count, 1.0)
            hops = local_avg + peer_avg
        else:
            hops = (controller.hop_cache.get(client_mac) or {}).get(mac)
            if hops is None:
                if mac in controller.host_attachment:
                    hops = max(controller._avg_hop_count, 1.0)
                else:
                    hops = hops_max
        hop_norm = hops / hops_max

        cost = (_W_CPU * cpu_norm + _W_RAM * ram_norm
                + _W_REQUESTS * req_norm + _W_HOPS * hop_norm)
        # Slow-start ramp: 1.0 → 0.0 over _VIP_WARM_SERVER_SECONDS from
        # discovery; a no-op unless topology_slowstart.  The window is
        # deliberately the warm-lease TTL, so slowstart and lifecycle differ
        # only in when their 45 s starts (discovery vs. spawn).
        cost += _slowstart_penalty(controller, mac, _VIP_WARM_SERVER_SECONDS)
        scored.append((cost, entry))
        logger.debug(
            "select_server: mac=%s cpu=%.1f ram=%.1f req=%s hops=%s cost=%.4f",
            mac,
            stats.avg_cpu_percent if stats else 0.0,
            stats.avg_ram_used_mb if stats else 0.0,
            stats.request_count if stats else 0,
            hops, cost,
        )
        if cost < best_cost:
            best_cost = cost
            tied = [entry]
        elif cost == best_cost:
            tied.append(entry)

    if not scored:
        return None

    if _BACKEND_SELECTION_SPREAD_EPS > 0:
        # Spread: near-min candidates plus any live warm claim form a band and
        # each client picks one member by a stateless hash, so a synchronised
        # burst fans out across cold units instead of funnelling onto one
        # (observed as a 60 s one-hot epoch).
        cutoff = best_cost + _BACKEND_SELECTION_SPREAD_EPS
        band = [(c, e) for c, e in scored if c <= cutoff]
        if not band:
            # Every scored cost was NaN (malformed telemetry): there is no
            # comparable minimum, so bail out rather than spread or crash.
            return None
        if warm_entry is not None and all(e["mac"] != warm_entry["mac"] for _, e in band):
            band.append((None, warm_entry))
        # Floor: the eps-band collapses to a single member whenever a unit is
        # the strict minimum by more than eps — the common case under load — so
        # top it up to k members with the cheapest eligible LOCAL units.
        # Eligible = finite cost, known IP (no silent ingress drop), not peer,
        # slow-start penalty fully decayed (recomputed at fill time; a decay
        # crossing between scoring and fill is bounded by one selection).
        # Cost ties break on MAC, so the cut is deterministic across pool
        # rebuilds (the raw scored order follows set iteration).
        floor_k = _BACKEND_SELECTION_SPREAD_MIN_BAND
        if floor_k > 1 and len(band) < floor_k:
            in_band = {e["mac"] for _, e in band}
            local = sorted(
                ((c, e) for c, e in scored
                 if math.isfinite(c)
                 and e["mac"] not in controller.peer_hosts
                 and e["mac"] in controller._mac_to_ip
                 and _slowstart_penalty(controller, e["mac"],
                                        _VIP_WARM_SERVER_SECONDS) == 0.0),
                key=lambda ce: (ce[0], ce[1]["mac"]),
            )
            for c, e in local:
                if len(band) >= floor_k:
                    break
                if e["mac"] not in in_band:
                    band.append((c, e))
                    in_band.add(e["mac"])
        if len(band) > 1:
            band.sort(key=lambda ce: ce[1]["mac"])
            idx = int(
                hashlib.md5((client_mac or "").encode("utf-8")).hexdigest(), 16
            ) % len(band)
            chosen_cost, chosen = band[idx]
        else:
            chosen_cost, chosen = band[0]
        logger.info(
            "select_server: selected=%s cost=%s min=%.4f "
            "(spread band=%d eps=%.3f floor=%d warm=%s)",
            chosen["mac"],
            f"{chosen_cost:.4f}" if chosen_cost is not None else "warm",
            best_cost, len(band), _BACKEND_SELECTION_SPREAD_EPS,
            _BACKEND_SELECTION_SPREAD_MIN_BAND,
            warm_entry is not None,
        )
        return chosen

    if not tied:
        return None

    chosen = tied[controller._rr_server_idx % len(tied)]
    controller._rr_server_idx += 1
    logger.info(
        "select_server: selected=%s cost=%.4f (tied=%d rr_idx=%d)",
        chosen["mac"], best_cost, len(tied), controller._rr_server_idx - 1,
    )
    return chosen


def select_storage(
    controller,
    domain: str,
    client_mac: str,
) -> dict | None:
    """Pick the storage node with the lowest WSM cost from the domain's pool.

    Cost_j = w_cpu·(CPU_j/CPU_max) + w_ram·(RAM_j/RAM_max)
           + w_conn·(Conn_j/Conn_max) + w_lag·(Lag_j/Lag_max)
           + w_hops·(Hops_j/Hops_max)

    When multiple candidates share the lowest cost (typical during cold
    start when all resource dimensions are 0.0), round-robin across them
    to distribute traffic evenly.  Each domain has its own counter.

    Storage spread band (`_STORAGE_SELECTION_SPREAD_EPS` > 0):
    mirrors select_server's band construction — eligible
    candidates (finite cost, local member, known IP, slow-start penalty
    fully decayed) within eps of the minimum form a band, floored at
    `_STORAGE_SELECTION_SPREAD_MIN_BAND` with the next-cheapest eligible
    members — so a renewal or a new connection spreads across near-best
    storage members instead of re-concentrating on one. Three storage
    deviations from the compute band: the pick rule is round-robin within
    the band (reusing the per-domain counter, not an md5 hash); the warm
    claim above keeps its early return (no warm member is appended to the
    band); and the log token is `storage_band=`. With eps = 0 the legacy
    behaviour is preserved exactly (argmin + exact-tie round-robin).
    """
    pool = controller.vip_storage_pool_n1 if domain == "n1" else controller.vip_storage_pool_n2
    if not pool:
        logger.warning("select_storage(%s): pool empty", domain)
        return None

    warm = None
    if _BACKEND_SELECTION_POLICY == "topology_lifecycle":
        warm = _claim_warm_backend(
            controller,
            f"vip_data({domain})",
            controller._warm_storage_leases.setdefault(domain, {}),
            pool,
        )
        if warm is not None:
            return warm

    pool_stats = [controller._storage_stats[m] for m in pool if m in controller._storage_stats]
    cpu_max  = max((s.avg_cpu_percent        for s in pool_stats), default=0.0) or 1.0
    ram_max  = max((s.avg_ram_used_mb         for s in pool_stats), default=0.0) or 1.0
    conn_max = max((s.avg_connections          for s in pool_stats), default=0.0) or 1.0
    lag_max  = max((s.avg_repl_lag_s or 0      for s in pool_stats), default=0.0) or 1.0
    hops_max = max(controller._hop_cache_max, 1)

    best_cost = float("inf")
    tied: list[dict] = []
    scored: list[tuple[float, dict]] = []

    for mac, entry in pool.items():
        stats = controller._storage_stats.get(mac)

        _default = _unknown_stats_default()
        cpu_norm  = (stats.avg_cpu_percent        / cpu_max)  if stats else _default
        ram_norm  = (stats.avg_ram_used_mb         / ram_max)  if stats else _default
        conn_norm = (stats.avg_connections          / conn_max) if stats else _default
        lag_norm  = ((stats.avg_repl_lag_s or 0)   / lag_max)  if stats else _default

        if mac in controller.peer_hosts:
            # Peer member: excluded from the hop cache (see
            # topology._rebuild_hop_cache), so this branch always runs and
            # yields local + peer hops (2 vs 4 here) — a peer-domain candidate
            # is always strictly farther than a local one.
            local_avg = max(controller._avg_hop_count, 1.0)
            peer_avg  = max(controller._peer_avg_hop_count, 1.0)
            hops = local_avg + peer_avg
        else:
            hops = (controller.hop_cache.get(client_mac) or {}).get(mac)
            if hops is None:
                if mac in controller.host_attachment:
                    hops = max(controller._avg_hop_count, 1.0)
                else:
                    hops = hops_max
        hop_norm = hops / hops_max

        cost = (_W_STORAGE_CPU * cpu_norm + _W_STORAGE_RAM * ram_norm
                + _W_STORAGE_CONNECTIONS * conn_norm
                + _W_STORAGE_LAG * lag_norm + _W_STORAGE_HOPS * hop_norm)
        # Slow-start ramp, as in select_server; the window reuses
        # _VIP_WARM_STORAGE_SECONDS, so it differs from the warm lease only in
        # when it starts.
        cost += _slowstart_penalty(controller, mac, _VIP_WARM_STORAGE_SECONDS)
        scored.append((cost, entry))
        logger.debug(
            "select_storage(%s): mac=%s cpu=%.1f ram=%.1f conn=%.1f lag=%.2f hops=%s cost=%.4f",
            domain, mac,
            stats.avg_cpu_percent if stats else 0.0,
            stats.avg_ram_used_mb if stats else 0.0,
            stats.avg_connections if stats else 0.0,
            (stats.avg_repl_lag_s or 0) if stats else 0.0,
            hops, cost,
        )
        if cost < best_cost:
            best_cost = cost
            tied = [entry]
        elif cost == best_cost:
            tied.append(entry)

    if _STORAGE_SELECTION_SPREAD_EPS > 0:
        # Storage band.  Eligibility mirrors the compute floor filter: finite
        # cost, local member, known IP (no silent ingress drop), slow-start
        # penalty fully decayed (recomputed here; a decay crossing between
        # scoring and fill is bounded by one selection).  Cost ties break on
        # MAC, so the cut is deterministic across pool rebuilds (the raw
        # scored order follows set iteration).
        def _band_eligible(mac: str) -> bool:
            return (
                mac not in controller.peer_hosts
                and mac in controller._mac_to_ip
                and _slowstart_penalty(controller, mac, _VIP_WARM_STORAGE_SECONDS) == 0.0
            )

        cutoff = best_cost + _STORAGE_SELECTION_SPREAD_EPS
        band = [(c, e) for c, e in scored
                if math.isfinite(c) and c <= cutoff and _band_eligible(e["mac"])]
        # Floor: top the band up to MIN_BAND members with the next-cheapest
        # eligible members whenever it is short (e.g. a strict winner by more
        # than eps), clamped to the eligible set.
        floor_k = _STORAGE_SELECTION_SPREAD_MIN_BAND
        if floor_k > 1 and len(band) < floor_k:
            in_band = {e["mac"] for _, e in band}
            local = sorted(
                ((c, e) for c, e in scored
                 if math.isfinite(c) and _band_eligible(e["mac"])),
                key=lambda ce: (ce[0], ce[1]["mac"]),
            )
            for c, e in local:
                if len(band) >= floor_k:
                    break
                if e["mac"] not in in_band:
                    band.append((c, e))
                    in_band.add(e["mac"])
        if band:
            # Round-robin within the band, reusing the per-domain counter that
            # also serves exact ties: an md5 pick can concentrate a small
            # burst from few edges, round-robin cannot.
            if len(band) > 1:
                band.sort(key=lambda ce: ce[1]["mac"])
                rr_idx = controller._rr_storage_idx.get(domain, 0)
                chosen_cost, chosen = band[rr_idx % len(band)]
                controller._rr_storage_idx[domain] = rr_idx + 1
            else:
                chosen_cost, chosen = band[0]
            # Keep the `storage_band=` token: analysis tooling greps it, and it
            # is deliberately not compute's `spread band=`.
            logger.info(
                "select_storage(%s): storage_band=%d eps=%.3f floor=%d warm=%s "
                "chosen=%s cost=%.4f min=%.4f",
                domain, len(band), _STORAGE_SELECTION_SPREAD_EPS,
                _STORAGE_SELECTION_SPREAD_MIN_BAND, warm is not None,
                chosen["mac"], chosen_cost, best_cost,
            )
            return chosen

    if not tied:
        return None

    rr_idx = controller._rr_storage_idx.get(domain, 0)
    chosen = tied[rr_idx % len(tied)]
    controller._rr_storage_idx[domain] = rr_idx + 1
    logger.info(
        "select_storage(%s): selected=%s cost=%.4f (tied=%d rr_idx=%d)",
        domain, chosen["mac"], best_cost, len(tied), rr_idx,
    )
    return chosen
