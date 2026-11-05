"""Controller-owned mutable VIP-routing state and lifecycle helpers.

All functions operate on ``controller`` (the VipRoutingMixin instance) and
read/write its instance attributes directly.  No state is moved off the
controller object.
"""

import threading
import time

from .config import (
    WarmLease, ClientVipBinding, logger,
    _W_CPU, _W_RAM, _W_REQUESTS, _W_HOPS,
    _VIP_IDLE_TIMEOUT, _VIP_HARD_TIMEOUT,
    _ROUTER_OVS_PORT,
)
from . import flows
from ..scaling_config import (
    _VIP_WARM_SERVER_SECONDS,
    _VIP_WARM_STORAGE_SECONDS,
    _VIP_FLOW_ISOLATION,
    _VIP_SERVER_PER_CONNECTION,
)

__all__ = [
    "init_vip_routing_state",
    "register_backend_ip",
    "mark_server_backend_warm",
    "mark_storage_backend_warm",
    "clear_server_backend_warm",
    "clear_storage_backend_warm",
    "register_new_server_backend",
    "unregister_server_backend",
    "unregister_storage_backend",
    "drop_backend_caches",
    "delete_vip_server_client_flows",
    "update_server_stats",
    "update_storage_stats",
]


def init_vip_routing_state(controller) -> None:
    """Initialise all mutable VIP-routing attributes on *controller*."""
    # Seeded by snooping ARP that reaches the controller: backends ARP for the
    # VIPs at startup, which bootstraps this table.
    controller._ip_to_mac: dict[str, str] = {}
    controller._mac_to_ip: dict[str, str] = {}

    # Telemetry stats keyed by MAC: each container discovers its own MAC and
    # reports it, so the aggregator forwards it as the key.  Written by
    # Thread 2, read by Thread 1's cost functions.
    controller._server_stats:  dict = {}  # mac -> ServerSummary
    controller._storage_stats: dict = {}  # mac -> StorageServerSummary

    # Round-robin counters: during cold start every resource dimension is 0.0,
    # so many backends share the lowest WSM cost and ties break round-robin
    # instead of always hitting the first entry.
    controller._rr_server_idx: int = 0
    controller._rr_storage_idx: dict[str, int] = {}
    controller._warm_rr_idx: int = 0

    controller._warm_lock = threading.Lock()
    controller._warm_server_leases: dict[str, WarmLease] = {}
    controller._warm_storage_leases: dict[str, dict[str, WarmLease]] = {
        "n1": {},
        "n2": {},
    }

    # First telemetry timestamp per MAC — the slow-start ramp's zero point.
    controller._backend_discovery_ts: dict[str, float] = {}

    # VIP_SERVER client→backend bindings (flow isolation).  Guarded by
    # _warm_lock: Thread 1 writes on selection, Thread 2 deletes on
    # request_complete.
    controller._vip_server_client_map: dict[str, ClientVipBinding] = {}

    # Per-connection VIP_DATA bindings, keyed by
    # (domain, client_mac, client_ip, tcp_src); only populated in
    # per-connection mode.  They let an established connection be re-installed
    # to the SAME backend after a bulk flow delete (backend unregister) —
    # never re-selected mid-stream.  Guarded by _vip_data_conn_lock (Thread 1
    # writes on selection/packet-in, Thread 3 prunes on unregister).
    # _vip_data_conn_lock is never nested with _warm_lock: no ordering hazard.
    controller._vip_data_conn_map: dict[tuple[str, str, str, int], str] = {}
    controller._vip_data_conn_lock = threading.Lock()

    logger.debug(
        "vip routing mixin: w_cpu=%.2f w_ram=%.2f w_req=%.2f w_hops=%.2f "
        "idle_timeout=%ds hard_timeout=%ds router_port=%d warm_storage=%.1fs warm_server=%.1fs",
        _W_CPU, _W_RAM, _W_REQUESTS, _W_HOPS,
        _VIP_IDLE_TIMEOUT, _VIP_HARD_TIMEOUT,
        _ROUTER_OVS_PORT,
        _VIP_WARM_STORAGE_SECONDS,
        _VIP_WARM_SERVER_SECONDS,
    )


def register_backend_ip(controller, mac: str, ip: str) -> None:
    """Seed _mac_to_ip/_ip_to_mac from a known IP returned by NodeAdder.

    Called by Thread 3 (ElasticityManager) immediately after adding a new
    MAC to the VIP pool, so Thread 1 can route the very first packet to
    the new backend without waiting for an ARP to arrive at the controller.
    snoop_arp() remains authoritative — it will overwrite this entry if
    the container's IP ever changes.
    """
    controller._ip_to_mac[ip]  = mac
    controller._mac_to_ip[mac] = ip
    logger.info("backend ip registered (static seed): %s -> %s", mac, ip)


# Warm-lease management (called from Thread 2 / Thread 3).

def mark_server_backend_warm(controller, mac: str) -> None:
    now = time.monotonic()
    with controller._warm_lock:
        controller._warm_server_leases[mac] = WarmLease(
            started_ts=now,
            expires_ts=now + _VIP_WARM_SERVER_SECONDS,
        )
    logger.info(
        "vip_server warm lease created: mac=%s ttl=%.1fs",
        mac,
        _VIP_WARM_SERVER_SECONDS,
    )


def mark_storage_backend_warm(controller, mac: str, domain: str) -> None:
    now = time.monotonic()
    with controller._warm_lock:
        domain_leases = controller._warm_storage_leases.setdefault(domain, {})
        domain_leases[mac] = WarmLease(
            started_ts=now,
            expires_ts=now + _VIP_WARM_STORAGE_SECONDS,
        )
    logger.info(
        "vip_data(%s) warm lease created: mac=%s ttl=%.1fs",
        domain,
        mac,
        _VIP_WARM_STORAGE_SECONDS,
    )


def clear_server_backend_warm(controller, mac: str) -> None:
    with controller._warm_lock:
        cleared = controller._warm_server_leases.pop(mac, None) is not None
    if cleared:
        logger.info("vip_server warm lease cleared: mac=%s", mac)


def clear_storage_backend_warm(controller, mac: str, domain: str) -> None:
    with controller._warm_lock:
        domain_leases = controller._warm_storage_leases.setdefault(domain, {})
        cleared = domain_leases.pop(mac, None) is not None
    if cleared:
        logger.info("vip_data(%s) warm lease cleared: mac=%s", domain, mac)


# Backend lifecycle hooks (public API, called by the elasticity manager).

def register_new_server_backend(controller, mac: str, ip: str) -> None:
    controller.add_server_mac(mac)
    register_backend_ip(controller, mac, ip)
    mark_server_backend_warm(controller, mac)


def unregister_server_backend(controller, mac: str) -> None:
    controller.remove_server_mac(mac)
    clear_server_backend_warm(controller, mac)
    controller._backend_discovery_ts.pop(mac, None)
    # The MAC is allocator-recycled: drop the cached summary so a recycled
    # container never inherits the previous one's stats (a heartbeat would
    # otherwise refresh foreign measurements as "fresh").
    controller._server_stats.pop(mac, None)
    # Flow isolation: drop bindings that pointed at this backend, whose MAC/IP
    # identity is recycled as well.
    if _VIP_FLOW_ISOLATION:
        with controller._warm_lock:
            stale = [c for c, b in controller._vip_server_client_map.items()
                     if b.backend_mac == mac]
            for c in stale:
                controller._vip_server_client_map.pop(c, None)


def delete_vip_server_client_flows(controller, client_ip: str,
                                   client_port: int = 0) -> None:
    """Delete a client's VIP_SERVER DNAT+SNAT flows (RQ3 flow isolation).

    Resolves the client MAC from ARP-learned state and deletes the exact
    recorded DNAT/SNAT pair (from ``_vip_server_client_map``) so the next
    request from that client triggers a fresh backend selection. In
    per-connection mode (``VIP_SERVER_PER_CONNECTION_FLOWS=1``) the binding is
    keyed by ``(client_mac, client_port)`` and only that connection's flow
    pair is deleted; otherwise (default) the per-client binding is used.
    Handles this controller's own per-LAN ``VIP_SERVER`` only (cross-network
    server flows are out of scope for RQ3).
    """
    client_mac = controller._ip_to_mac.get(client_ip)
    if client_mac is None:
        logger.debug(
            "vip_server: flow delete for unknown client ip=%s — skipping", client_ip,
        )
        return
    key = ((client_mac, client_port)
           if (_VIP_SERVER_PER_CONNECTION and client_port) else client_mac)
    with controller._warm_lock:
        binding = controller._vip_server_client_map.get(key)
    if binding is None:
        logger.debug(
            "vip_server: no binding for client ip=%s:%s — skipping",
            client_ip, client_port,
        )
        return
    for datapath in controller.datapaths:
        try:
            flows.delete_vip_server_client_flows(controller, datapath, binding)
        except Exception:
            logger.exception(
                "vip_server: failed to delete flows for client=%s", client_ip,
            )
    # Distinct token — the analyzer matches request_complete deletes on it.
    logger.info("vip_server: request_complete: client flows deleted — client=%s",
                client_ip)
    # Pop only if it is still the binding we deleted: a concurrent Thread-1
    # re-selection may have installed a newer one meanwhile.
    with controller._warm_lock:
        if controller._vip_server_client_map.get(key) is binding:
            controller._vip_server_client_map.pop(key, None)


def unregister_storage_backend(controller, mac: str, domain: str) -> None:
    controller.remove_storage_mac(mac, domain)
    clear_storage_backend_warm(controller, mac, domain)
    controller._backend_discovery_ts.pop(mac, None)
    # Same recycled-MAC hygiene as the compute side.
    controller._storage_stats.pop(mac, None)

    # A connection bound to the removed backend must NOT be re-installed to it.
    # The bulk flow delete below drops every forward rule of the domain, which
    # is safe for the others: their established connections are re-installed
    # from this map on the next packet-in (same backend, never re-selected).
    with controller._vip_data_conn_lock:
        stale = [
            k for k, b in controller._vip_data_conn_map.items()
            if k[0] == domain and b == mac
        ]
        for k in stale:
            controller._vip_data_conn_map.pop(k, None)

    # Delete the forward rule so new connections get a fresh backend.  The
    # reply rule is NOT deleted — it is shared and serves established
    # connections from conntrack state.
    for datapath in controller.datapaths:
        dp_id = datapath.id
        try:
            flows.delete_vip_data_forward_rule(controller, datapath, domain)
        except Exception:
            logger.exception(
                "vip_data(%s): failed to delete forward rule on dp=%s",
                domain, dp_id,
            )


def drop_backend_caches(controller, mac: str) -> None:
    """Drop the cached telemetry summary + discovery stamp for a removed node.

    For removal paths that never unregistered through the VIP helpers
    (standby-reserve loss): the MAC is
    allocator-recycled, so the previous container's stats must not survive.
    """
    controller._server_stats.pop(mac, None)
    controller._storage_stats.pop(mac, None)
    controller._backend_discovery_ts.pop(mac, None)


# Telemetry cache updates (called from Thread 2).

def update_server_stats(controller, servers: dict) -> None:
    """Store per-server telemetry stats, keyed by MAC.

    ``servers`` is keyed by the container's MAC address (each container
    discovers its own MAC from eth0 and includes it in telemetry events).
    Thread 1 reads _server_stats in select_server() for the WSM cost
    function.
    """
    for mac, summary in servers.items():
        if mac not in controller._backend_discovery_ts:
            # Stamp every node the controller hears from, including
            # liveness-only heartbeats: the slow-start clock starts at spawn,
            # not at first assignment.
            controller._backend_discovery_ts[mac] = time.monotonic()
        if summary.heartbeat_only:
            # Liveness-only sample: its zeroed latency fields must never
            # overwrite real routing stats.  Refresh freshness and drain state
            # on an existing entry; a node with no entry yet keeps a flagged
            # placeholder, so it stays a graceful scale-down candidate.
            entry = controller._server_stats.get(mac)
            if entry is None:
                controller._server_stats[mac] = summary
            elif summary.last_report_ts > entry.last_report_ts:
                entry.last_report_ts = summary.last_report_ts
                entry.state = summary.state
            continue
        controller._server_stats[mac] = summary
        logger.debug(
            "server stats updated: mac=%s cpu=%.1f%% ram=%.1fMB req=%d",
            mac, summary.avg_cpu_percent,
            summary.avg_ram_used_mb, summary.request_count,
        )


def update_storage_stats(controller, storage_servers: dict) -> None:
    """Store per-storage telemetry stats, keyed by MAC.

    ``storage_servers`` is keyed by the container's MAC address.
    Thread 1 reads _storage_stats in select_storage() for the storage
    WSM cost function.
    """
    for mac, summary in storage_servers.items():
        controller._storage_stats[mac] = summary
        if mac not in controller._backend_discovery_ts:
            controller._backend_discovery_ts[mac] = time.monotonic()
        logger.debug(
            "storage stats updated: mac=%s cpu=%.1f%% ram=%.1fMB conn=%.1f lag=%s",
            mac, summary.avg_cpu_percent,
            summary.avg_ram_used_mb, summary.avg_connections,
            summary.avg_repl_lag_s,
        )
