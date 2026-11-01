"""VIP routing mixin for the OS-Ken SDN controller.

Responsibilities:
  - ARP interception for VIP addresses (reactive, controller-generated replies)
  - IP packet interception for VIP_SERVER and VIP_DATA
  - DNAT/SNAT flow rule installation (priority 200, with timeouts)
  - Multi-dimensional WSM cost-based server selection (CPU, RAM, requests, hops)
  - Multi-dimensional WSM cost-based storage selection (CPU, RAM, connections,
    replication lag, hops)
  - Per-domain VIP_DATA: VIP_DATA_N1 routes to LAN1 storage,
    VIP_DATA_N2 routes to LAN2 storage

Cross-network support: when pools contain backends on the peer LAN (learned
via TopologyMixin.peer_hosts), select_server/select_storage can pick them.
DNAT'd packets are output to ROUTER_OVS_PORT so the inter-LAN router
forwards them to the remote switch, which delivers them via normal L2
forwarding (the packet is already rewritten — no second VIP PacketIn).

This is NOT a new thread.  All methods are called inline by Thread 1's
packet_in_handler.  State written by Thread 2 (_server_stats, _storage_stats)
is read here without additional locking because eventlet uses cooperative
switching and these dicts are only mutated between yield points. Warm-lease
state is the exception: native Thread 3 writes it too, so that slice is
guarded by a small threading.Lock.

Usage (class MRO in main_n*.py):
    class KenLearnAndLog(VipRoutingMixin, TopologyMixin, OSKenApp):
        ...

Implementation is delegated to the private _vip_routing package:
  - config      shared constants, types, logger
  - state       controller-owned mutable state and lifecycle helpers
  - selection   server/storage selection (warm-lease, WSM, round-robin)
  - flows       DNAT/SNAT flow-rule construction and PacketOut
  - ingress     ARP snooping, VIP packet dispatch, punt rules
"""

import subprocess

from ._vip_routing import config, flows, ingress, selection, state


def _verify_conntrack_available():
    """Verify OVS conntrack is available (ovs-appctl is inside the OVS container)."""
    try:
        result = subprocess.run(
            ["docker", "exec", "ovs", "ovs-appctl", "dpctl/dump-conntrack"],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            config.logger.warning(
                "OVS conntrack check failed (rc=%d) — "
                "VIP_DATA routing may not work correctly. "
                "Ensure the kernel datapath has conntrack support enabled.",
                result.returncode,
            )
            return False
    except FileNotFoundError:
        config.logger.warning(
            "docker not found — cannot verify OVS conntrack availability"
        )
        return False
    except subprocess.TimeoutExpired:
        config.logger.warning(
            "ovs-appctl dpctl/dump-conntrack timed out — "
            "conntrack may not be functional"
        )
        return False
    config.logger.info("OVS conntrack available — VIP_DATA routing ready")
    return True


class VipRoutingMixin:
    """Mixin that intercepts traffic for VIP_SERVER (HTTP) and VIP_DATA (MongoDB).

    Must sit *before* TopologyMixin in the class MRO so that the
    _on_datapath_connected hook is called in the correct cooperative order.

    Depends on TopologyMixin attributes (set at __init__ time):
        vip_server_ip, vip_server_mac,
        vip_server_n2_ip, vip_server_n2_mac,
        vip_data_n1_ip, vip_data_n1_mac, vip_data_n2_ip, vip_data_n2_mac,
        vip_server_pool, vip_storage_pool_n1, vip_storage_pool_n2,
        hop_cache, _hop_cache_max, host_attachment

    Depends on the concrete class (_install_flow defined in main_n*.py):
        _install_flow(dp, priority, match, actions, *, idle_timeout, hard_timeout)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        state.init_vip_routing_state(self)

    def _on_datapath_connected(self, datapath) -> None:
        """Install VIP rules after the switch reconnects and stale flows are flushed."""
        super()._on_datapath_connected(datapath)
        self.install_vip_arp_punt_rules(datapath)
        self.install_vip_punt_rules(datapath)

        if not getattr(self, "_conntrack_verified", False):
            _verify_conntrack_available()
            self._conntrack_verified = True

    def snoop_arp(self, pkt) -> None:
        """Record sender IP ↔ MAC from any ARP packet that reaches the controller."""
        return ingress.snoop_arp(self, pkt)

    def handle_vip_packet_in(self, datapath, in_port, pkt, eth) -> bool:
        """Intercept ARP and IP packets destined for VIP addresses.

        Returns True if the packet was handled here — caller should return
        immediately without running L2 learning.
        Returns False to let normal packet processing continue.
        """
        return ingress.handle_vip_packet_in(self, datapath, in_port, pkt, eth)

    def register_backend_ip(self, mac: str, ip: str) -> None:
        """Seed _mac_to_ip/_ip_to_mac from a known IP returned by NodeAdder."""
        return state.register_backend_ip(self, mac, ip)

    def register_new_server_backend(self, mac: str, ip: str) -> None:
        """Add a new server backend to the VIP pool with warm lease."""
        return state.register_new_server_backend(self, mac, ip)

    def unregister_server_backend(self, mac: str) -> None:
        """Remove a server backend from the VIP pool."""
        return state.unregister_server_backend(self, mac)

    def unregister_storage_backend(self, mac: str, domain: str) -> None:
        """Remove a storage backend from the VIP pool."""
        return state.unregister_storage_backend(self, mac, domain)

    def drop_backend_caches(self, mac: str) -> None:
        """Drop cached telemetry stats + discovery for a removed node that never unregistered (reserve/selective)."""
        return state.drop_backend_caches(self, mac)

    def mark_server_backend_warm(self, mac: str) -> None:
        """Create a warm lease for a server backend (bypasses cold-start penalty)."""
        return state.mark_server_backend_warm(self, mac)

    def mark_storage_backend_warm(self, mac: str, domain: str) -> None:
        """Create a warm lease for a storage backend (bypasses cold-start penalty)."""
        return state.mark_storage_backend_warm(self, mac, domain)

    def clear_server_backend_warm(self, mac: str) -> None:
        """Remove the warm lease for a server backend."""
        return state.clear_server_backend_warm(self, mac)

    def clear_storage_backend_warm(self, mac: str, domain: str) -> None:
        """Remove the warm lease for a storage backend."""
        return state.clear_storage_backend_warm(self, mac, domain)

    def select_server(self, client_mac: str) -> dict | None:
        """Pick the web server with the lowest WSM cost from vip_server_pool."""
        return selection.select_server(self, client_mac)

    def select_storage(self, domain: str, client_mac: str) -> dict | None:
        """Pick the storage node with the lowest WSM cost from the domain's pool."""
        return selection.select_storage(self, domain, client_mac)

    def delete_vip_server_client_flows(self, client_ip: str,
                                       client_port: int = 0) -> None:
        """Delete a client's VIP_SERVER DNAT+SNAT flows (RQ3 flow isolation).

        Forces one fresh backend-selection event per measured request. In
        per-connection mode (``VIP_SERVER_PER_CONNECTION_FLOWS=1``)
        ``client_port`` scopes the delete to the exact connection's flow pair;
        otherwise (default) the per-client binding is used. Handles this
        controller's own per-LAN ``VIP_SERVER`` only (cross-network server
        flows are out of RQ3 scope — see D8).
        """
        return state.delete_vip_server_client_flows(self, client_ip, client_port)

    def update_server_stats(self, servers: dict) -> None:
        """Store per-server telemetry stats, keyed by MAC."""
        return state.update_server_stats(self, servers)

    def update_storage_stats(self, storage_servers: dict) -> None:
        """Store per-storage telemetry stats, keyed by MAC."""
        return state.update_storage_stats(self, storage_servers)

    def install_vip_arp_punt_rules(self, datapath) -> None:
        """Punt ARP requests for VIP IPs to the controller (priority 100)."""
        return ingress.install_vip_arp_punt_rules(self, datapath)

    def install_vip_punt_rules(self, datapath) -> None:
        """Punt IPv4 packets destined for VIP addresses to the controller (priority 100)."""
        return ingress.install_vip_punt_rules(self, datapath)

    def _install_vip_dnat_snat(self, datapath, in_port, pkt, **kwargs):
        """Install a DNAT + SNAT flow rule pair and Packet-Out the first packet."""
        return flows.install_vip_dnat_snat(self, datapath, in_port, pkt, **kwargs)

