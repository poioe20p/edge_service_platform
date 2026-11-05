import logging
import os

import networkx as nx
from .models import TopologyHostEntry, TopologyNetworkSection, TopologySnapshot
import zmq
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.topology.api import get_all_link, get_host

logger = logging.getLogger('os_ken.topology_mixin')


class TopologyMixin:
    """Mixin that adds local topology discovery, proactive flow rules, and ZMQ PUB/SUB peer topology sharing."""

    REQUIRED_APP = ['os_ken.topology.switches']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # VIP_SERVER — one per LAN; the n2_* variants belong to LAN2.
        self.vip_server_ip  = os.environ.get("VIP_SERVER_IP",  "10.0.0.253")
        self.vip_server_mac = os.environ.get("VIP_SERVER_MAC", "aa:bb:cc:dd:ee:01")
        self.vip_server_n2_ip  = os.environ.get("VIP_SERVER_N2_IP",  "10.0.1.253")
        self.vip_server_n2_mac = os.environ.get("VIP_SERVER_N2_MAC", "aa:bb:cc:dd:ee:04")

        self.vip_data_n1_ip  = os.environ.get("VIP_DATA_N1_IP",  "10.0.0.254")
        self.vip_data_n1_mac = os.environ.get("VIP_DATA_N1_MAC", "aa:bb:cc:dd:ee:02")
        self.vip_data_n2_ip  = os.environ.get("VIP_DATA_N2_IP",  "10.0.1.254")
        self.vip_data_n2_mac = os.environ.get("VIP_DATA_N2_MAC", "aa:bb:cc:dd:ee:03")

        self._network_id           = os.environ.get("LAN_ID", "lan1")
        self._topology_interval    = max(1, int(os.environ.get("TOPOLOGY_INTERVAL", "1")))
        self._heartbeat_ticks      = max(30, int(os.environ.get("TOPOLOGY_HEARTBEAT_TICKS", "30")))
        self._topology_pub_port    = int(os.environ.get("TOPOLOGY_PUB_PORT", "5557"))

        self._local_server_macs: set[str] = {
            m.strip() for m in os.environ.get("SERVER_MACS", "").split(",") if m.strip()
        }
        self._local_storage_macs_n1: set[str] = {
            m.strip() for m in os.environ.get("STORAGE_MACS_N1", "").split(",") if m.strip()
        }
        self._local_storage_macs_n2: set[str] = {
            m.strip() for m in os.environ.get("STORAGE_MACS_N2", "").split(",") if m.strip()
        }

        self._peer_server_macs:     set[str] = set()
        self._peer_storage_macs_n1: set[str] = set()
        self._peer_storage_macs_n2: set[str] = set()

        logger.info(
            "topology mixin init: network=%s interval=%ds heartbeat=%d ticks "
            "server_macs=%s storage_macs_n1=%s storage_macs_n2=%s",
            self._network_id, self._topology_interval, self._heartbeat_ticks,
            list(self._local_server_macs), list(self._local_storage_macs_n1), list(self._local_storage_macs_n2),
        )

        self.vip_server_pool:     dict[str, dict] = {}   # mac -> {mac, dpid, port_no}
        self.vip_storage_pool_n1: dict[str, dict] = {}
        self.vip_storage_pool_n2: dict[str, dict] = {}

        self.net             = nx.DiGraph()
        self.sws:    list    = []
        self.links:  list    = []
        self.hosts:  list    = []
        self._sws_prev:   list = []
        self._links_prev: list = []
        self._hosts_prev: list = []
        self.host_attachment: dict = {}   # mac -> (dpid, port_no)
        self.hop_cache:       dict = {}   # host_mac -> {server_mac: hops}
        self._hop_cache_max:  int  = 1
        self._installed_flow_keys: set = set()
        self._arp_rules_installed:  set = set()
        self._topology_api_app           = None
        self._topology_api_lookup_warned = False
        self._topology_initialized       = False
        self._topology_tick:     int     = 0

        self._router_mac_blocklist = {
            "00:00:00:00:00:aa", "00:00:00:00:00:bb",
            "00:00:00:00:00:cc", "00:00:00:00:00:dd",
            "00:00:00:00:00:AA", "00:00:00:00:00:BB",
            "00:00:00:00:00:CC", "00:00:00:00:00:DD",
        }

        # Peer topology — written by on_topology_update() from ZmqTelemetrySource
        self.peer_hosts:       dict = {}   # mac -> {"mac", "dpid", "port_no", "ip"}
        self._peer_network_id: str  = ""
        self._topo_correction_needed: bool = False

        self._avg_hop_count: float = 0.0
        self._peer_avg_hop_count: float = 0.0

        self._topo_pub_ctx    = zmq.Context()
        self._topo_pub_socket = self._topo_pub_ctx.socket(zmq.PUB)
        self._topo_pub_socket.bind(f"tcp://*:{self._topology_pub_port}")
        logger.info("topology PUB bound on tcp://*:%d", self._topology_pub_port)

        hub.spawn(self._topology_worker)

    @property
    def _server_macs(self) -> set[str]:
        return self._local_server_macs | self._peer_server_macs
    @property
    def _storage_macs_n1(self) -> set[str]:
        return self._local_storage_macs_n1 | self._peer_storage_macs_n1

    @property
    def _storage_macs_n2(self) -> set[str]:
        return self._local_storage_macs_n2 | self._peer_storage_macs_n2

    def add_server_mac(self, mac: str) -> None:
        self._local_server_macs.add(mac)
        self._rebuild_hop_cache()

    def remove_server_mac(self, mac: str) -> None:
        self._local_server_macs.discard(mac)
        self._rebuild_hop_cache()

    def add_storage_mac(self, mac: str, domain: str = "n1") -> None:
        if domain == "n2":
            self._local_storage_macs_n2.add(mac)
        else:
            self._local_storage_macs_n1.add(mac)
        self._rebuild_hop_cache()

    def remove_storage_mac(self, mac: str, domain: str = "n1") -> None:
        if domain == "n2":
            self._local_storage_macs_n2.discard(mac)
        else:
            self._local_storage_macs_n1.discard(mac)
        self._rebuild_hop_cache()

    def get_next_hop_port(self, edge_dpid: int, client_mac: str, server_mac: str):
        try:
            path = nx.shortest_path(self.net, client_mac, server_mac)
            idx  = path.index(edge_dpid)
            return self.net[edge_dpid][path[idx + 1]]["port"]
        except (nx.NetworkXNoPath, nx.NodeNotFound, ValueError, IndexError, KeyError):
            return None

    # Callback invoked by ZmqTelemetrySource._receive_loop.
    def on_topology_update(self, data: dict) -> None:
        try:
            snapshot = TopologySnapshot.model_validate(data)
        except Exception as exc:
            logger.warning("Ignoring malformed topology message: %s", exc)
            return

        local_section = snapshot.networks.get(self._network_id)
        if local_section is not None and self.host_attachment:
            peer_known_macs = {h.mac for h in local_section.hosts}
            local_macs      = set(self.host_attachment.keys())
            if peer_known_macs != local_macs:
                logger.info(
                    "peer has stale view of %s (%d vs %d hosts) — triggering immediate republish",
                    self._network_id, len(peer_known_macs), len(local_macs),
                )
                self._topo_correction_needed = True

        peer_nid = snapshot.network_id
        peer_net = snapshot.networks.get(peer_nid)
        self._peer_network_id = peer_nid
        # Replace wholesale so removals propagate.
        self.peer_hosts = (
            {h.mac: h.model_dump() for h in peer_net.hosts}
            if peer_net is not None else {}
        )

        # Replace wholesale so removals propagate
        self._peer_server_macs     = set(snapshot.server_macs)
        self._peer_storage_macs_n1 = set(snapshot.storage_macs_n1)
        self._peer_storage_macs_n2 = set(snapshot.storage_macs_n2)
        self._peer_avg_hop_count = snapshot.avg_hop_count
        logger.debug(
            "peer topology updated from %s: %d hosts; peer server_macs=%s storage_n1=%s storage_n2=%s peer_avg_hops=%.2f",
            snapshot.network_id, len(self.peer_hosts),
            list(self._peer_server_macs),
            list(self._peer_storage_macs_n1),
            list(self._peer_storage_macs_n2),
            self._peer_avg_hop_count,
        )

        # Seed _mac_to_ip for peer hosts: local ARP snooping never fires for
        # cross-network hosts, so VIP routing would otherwise wait forever.
        for h in self.peer_hosts.values():
            ip = h.get("ip")
            if ip:
                self.register_backend_ip(h["mac"], ip)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            ofproto = datapath.ofproto
            parser  = datapath.ofproto_parser

            # Delete all existing flows so stale entries from a previous controller
            # run don't suppress PacketIn events needed for host re-discovery.
            match = parser.OFPMatch()
            datapath.send_msg(parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=match,
            ))

            self._install_flow(
                datapath,
                priority=0,
                match=match,
                actions=[parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                                ofproto.OFPCML_NO_BUFFER)],
            )
            logger.info("datapath connected: dpid=%s — flushed stale flows, reinstalled table-miss", datapath.id)

            # Only now may mixins install their own rules: flows are flushed and
            # the table-miss is back in place.
            self._on_datapath_connected(datapath)

            entry = (datapath, datapath.id)
            if entry not in self.sws:
                self.sws.append(entry)
            self._datapath_by_id[datapath.id] = entry
        elif ev.state == DEAD_DISPATCHER:
            entry = (datapath, datapath.id)
            if entry in self.sws:
                self.sws.remove(entry)
            self._datapath_by_id.pop(datapath.id, None)
            logger.info("datapath disconnected: dpid=%s", datapath.id)

    def _on_datapath_connected(self, datapath) -> None:
        """Called after a switch reconnects and stale flows are flushed.

        Override in subclasses or mixins (calling super()) to install
        additional rules that must survive the flow-table wipe on reconnect.
        """
        pass

    def _check_link(self, link_in, links_list):
        """True when *link_in* is not already represented in *links_list* (deduplication)."""
        return not any(
            link[0] == link_in[0] and link[2] == link_in[2] for link in links_list
        )

    def _get_topology_api_app(self):
        """Lazily resolve the OS-Ken topology switches service."""
        if self._topology_api_app is None:
            self._topology_api_app = app_manager.lookup_service_brick('switches')
            if self._topology_api_app is None:
                if not self._topology_api_lookup_warned:
                    logger.debug("Topology API service not ready yet")
                    self._topology_api_lookup_warned = True
            else:
                self._topology_api_lookup_warned = False
        return self._topology_api_app

    def get_sws_links_hosts(self):
        """Rebuild net, hosts, links, and host_attachment from OS-Ken topology API."""
        self.links = []
        self.hosts = []
        self.net.clear()
        self.host_attachment = {}

        topo_api_app = self._get_topology_api_app()
        if topo_api_app is None:
            return

        host_list = get_host(topo_api_app, None) or []
        self.hosts = [
            (host.mac, host.port.dpid, host.port.port_no)
            for host in host_list
            if getattr(host, "port", None) is not None
            and host.mac not in self._router_mac_blocklist
        ]

        for mac, dpid, port_no in self.hosts:
            self.host_attachment[mac] = (dpid, port_no)

        for host in self.hosts:
            self.net.add_edge(host[0], host[1], weight=1, port=1)
            self.net.add_edge(host[1], host[0], weight=1, port=host[2])

        links_list = get_all_link(topo_api_app) or []
        raw_links = [(link.src.dpid, link.dst.dpid, link.src.port_no) for link in links_list]
        for link in raw_links:
            if self._check_link(link, self.links):
                self.links.append(link)

        for link in self.links:
            self.net.add_edge(link[0], link[1], weight=1, port=link[2])

        logger.debug(
            "topology poll: %d switches %d links %d hosts",
            len(self.sws), len(self.links), len(self.hosts),
        )


    def _rebuild_hop_cache(self):
        """Recompute hop counts from every host to every server/storage backend."""
        self.hop_cache = {}
        self._hop_cache_max = 1

        if not self.net or not self.host_attachment:
            return

        # Only LOCAL backends get hop-cache paths: OVS learns peer MACs on the router
        # port, so a naive path makes cross-LAN look local; peer selection is router-aware.
        backend_macs = (
            self._local_server_macs
            | self._local_storage_macs_n1
            | self._local_storage_macs_n2
        ) & set(self.host_attachment.keys())
        if not backend_macs:
            return

        for host_mac in list(self.host_attachment.keys()):
            per_host: dict = {}
            for server_mac in backend_macs:
                if host_mac == server_mac:
                    continue
                try:
                    path = nx.shortest_path(self.net, host_mac, server_mac)
                    hops = max(len(path) - 1, 0)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    hops = None
                per_host[server_mac] = hops
                if isinstance(hops, int) and hops > self._hop_cache_max:
                    self._hop_cache_max = hops
            self.hop_cache[host_mac] = per_host

        all_hops = [
            v for per_host in self.hop_cache.values()
            for v in per_host.values()
            if v is not None
        ]
        logger.debug("hop cache rebuilt: %d hosts, %d resolved paths",
                     len(self.hop_cache), len(all_hops))
        self._avg_hop_count = sum(all_hops) / len(all_hops) if all_hops else 0.0
        logger.debug("hop cache avg hops: %.2f", self._avg_hop_count)


    def _rebuild_vip_pools(self) -> None:
        """Merge local host_attachment and peer_hosts, then filter by MAC membership sets."""
        combined: dict[str, dict] = {
            mac: {"mac": mac, "dpid": dpid, "port_no": port_no}
            for mac, (dpid, port_no) in self.host_attachment.items()
        }
        combined |= self.peer_hosts

        self.vip_server_pool     = {mac: combined[mac] for mac in self._server_macs     if mac in combined}
        self.vip_storage_pool_n1 = {mac: combined[mac] for mac in self._storage_macs_n1 if mac in combined}
        self.vip_storage_pool_n2 = {mac: combined[mac] for mac in self._storage_macs_n2 if mac in combined}
        logger.debug(
            "vip_server_pool=%s vip_storage_pool_n1=%s vip_storage_pool_n2=%s",
            list(self.vip_server_pool),
            list(self.vip_storage_pool_n1),
            list(self.vip_storage_pool_n2),
        )

    def _topology_worker(self):
        while True:
            try:
                hub.sleep(self._topology_interval)
                self._topology_tick += 1
                logger.debug("topology tick #%d", self._topology_tick)

                self.get_sws_links_hosts()
                self._rebuild_hop_cache()
                self._rebuild_vip_pools()

                changed = (
                    self.hosts != self._hosts_prev
                    or self.links != self._links_prev
                    or self.sws   != self._sws_prev
                )
                first_valid = (
                    not self._topology_initialized
                    and bool(self.sws)
                    and bool(self.links)
                    and bool(self.hosts)
                )

                if first_valid or changed:
                    self._install_local_topology_flows()
                    self._topology_initialized = True
                    logger.info(
                        "topology installed: %d sw %d links %d hosts",
                        len(self.sws), len(self.links), len(self.hosts),
                    )

                self._sws_prev   = self.sws.copy()
                self._links_prev = self.links.copy()
                self._hosts_prev = self.hosts.copy()

                should_publish = (
                    first_valid
                    or changed
                    or self._topo_correction_needed
                    or self._topology_tick % self._heartbeat_ticks == 0
                )
                if should_publish and self.sws:
                    if first_valid:
                        publish_reason = "first_valid"
                    elif changed:
                        publish_reason = "changed"
                    elif self._topo_correction_needed:
                        publish_reason = "correction"
                    else:
                        publish_reason = f"heartbeat (tick {self._topology_tick})"
                    logger.debug("publishing topology: reason=%s", publish_reason)
                    self._publish_topology()
                    self._topo_correction_needed = False

            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.error("Error in topology worker: %s", exc)

    def _publish_topology(self) -> None:
        local_section = TopologyNetworkSection(
            hosts=[
                TopologyHostEntry(mac=mac, dpid=dpid, port_no=port_no, ip=self._mac_to_ip.get(mac))
                for mac, (dpid, port_no) in self.host_attachment.items()
            ],
        )
        networks: dict[str, TopologyNetworkSection] = {self._network_id: local_section}
        if self._peer_network_id:
            # Re-advertise the peer's hosts so a third listener sees both networks.
            networks[self._peer_network_id] = TopologyNetworkSection(
                hosts=[TopologyHostEntry(**h) for h in self.peer_hosts.values()],
            )
        snapshot = TopologySnapshot(
            network_id=self._network_id,
            networks=networks,
            # Advertise this controller's locally-registered backend MACs
            server_macs=list(self._local_server_macs),
            storage_macs_n1=list(self._local_storage_macs_n1),
            storage_macs_n2=list(self._local_storage_macs_n2),
            avg_hop_count=self._avg_hop_count,
        )
        try:
            self._topo_pub_socket.send_string(snapshot.model_dump_json(), zmq.NOBLOCK)
            logger.info(
                "Published topology snapshot: network=%s sw=%d links=%d hosts=%d "
                "peer_network=%s peer_hosts=%d",
                self._network_id, len(self.sws), len(self.links), len(self.hosts),
                self._peer_network_id, len(self.peer_hosts),
            )
        except zmq.Again:
            pass  # no subscriber yet — normal at startup

    def _install_local_topology_flows(self):
        if not (self.sws and self.hosts):
            return
        logger.info(
            "reinstalling all flows (clearing %d existing keys)",
            len(self._installed_flow_keys),
        )
        self._installed_flow_keys.clear()
        self._arp_rules_installed.clear()
        self.mac_to_port.clear()
        self.send_all_flow_rules_proactively()

    def proactive_flow_rule_install(self, sw, p):
        dp   = sw[0]
        dpid = sw[1]
        src_mac = p[0]
        dst_mac = p[-1]

        self.mac_to_port.setdefault(dpid, {})
        parser  = dp.ofproto_parser
        ofproto = dp.ofproto

        try:
            index_current_dpid = p.index(dpid)
            prev_node = p[index_current_dpid - 1]
            next_node = p[index_current_dpid + 1]
        except (ValueError, IndexError):
            logger.warning("Switch %s not fully present in calculated path %s", dpid, p)
            return

        try:
            in_port  = self.net[dpid][prev_node]["port"]
            out_port = self.net[dpid][next_node]["port"]
        except KeyError:
            logger.warning("Missing port data for %s in path %s", dpid, p)
            return

        forward_key = (dpid, src_mac, dst_mac)
        reverse_key = (dpid, dst_mac, src_mac)

        if forward_key not in self._installed_flow_keys:
            match   = parser.OFPMatch(in_port=in_port,  eth_dst=dst_mac, eth_src=src_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self._install_flow(dp, priority=5, match=match, actions=actions)
            self._installed_flow_keys.add(forward_key)
            logger.debug("flow fwd: dpid=%s src=%s dst=%s in_port=%s out_port=%s", dpid, src_mac, dst_mac, in_port, out_port)

        if reverse_key not in self._installed_flow_keys:
            match   = parser.OFPMatch(in_port=out_port, eth_dst=src_mac, eth_src=dst_mac)
            actions = [parser.OFPActionOutput(in_port)]
            self._install_flow(dp, priority=5, match=match, actions=actions)
            self._installed_flow_keys.add(reverse_key)
            logger.debug("flow rev: dpid=%s src=%s dst=%s in_port=%s out_port=%s", dpid, dst_mac, src_mac, out_port, in_port)

        if dpid not in self._arp_rules_installed:
            match   = parser.OFPMatch(eth_type=0x806)
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            self._install_flow(dp, priority=1, match=match, actions=actions)
            self._arp_rules_installed.add(dpid)

        self.mac_to_port[dpid][src_mac] = in_port
        self.mac_to_port[dpid][dst_mac] = out_port

    def send_all_flow_rules_proactively(self):
        if not self.hosts or not self._datapath_by_id:
            return
        pair_count = len(self.hosts) * (len(self.hosts) - 1) // 2
        logger.debug("proactive flow install: %d hosts, %d pairs", len(self.hosts), pair_count)
        for idx, host1 in enumerate(self.hosts):
            for host2 in self.hosts[idx + 1:]:
                try:
                    path = nx.shortest_path(self.net, host1[0], host2[0])
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                self._install_path_flows(path)

    def _install_path_flows(self, path):
        for node in path:
            sw = self._datapath_by_id.get(node)
            if sw:
                self.proactive_flow_rule_install(sw, path)
