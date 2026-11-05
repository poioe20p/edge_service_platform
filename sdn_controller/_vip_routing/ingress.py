"""Ingress handling: ARP snooping, VIP packet dispatch, ARP reply,
VIP server/data handling, and punt-rule installation.
"""

from os_ken.lib.packet import arp as arp_lib
from os_ken.lib.packet import ethernet as eth_lib
from os_ken.lib.packet import ether_types, ipv4, packet, tcp as tcp_lib

from .config import (
    _ROUTER_MAC, _ROUTER_OVS_PORT, _VIP_DATA_PER_CONNECTION, logger,
)
from . import flows, selection
from ..scaling_config import _VIP_SERVER_PER_CONNECTION


def _iter_vip_bindings(controller):
    """Yield the (vip_ip, vip_mac) pairs this controller answers for."""
    yield controller.vip_server_ip, controller.vip_server_mac
    yield controller.vip_server_n2_ip, controller.vip_server_n2_mac
    yield controller.vip_data_n1_ip, controller.vip_data_n1_mac
    yield controller.vip_data_n2_ip, controller.vip_data_n2_mac


def _find_vip_binding(controller, vip_ip):
    """Return the (vip_ip, vip_mac) pair matching *vip_ip*, or None."""
    for binding in _iter_vip_bindings(controller):
        if binding[0] == vip_ip:
            return binding
    return None


def snoop_arp(controller, pkt) -> None:
    """Record sender IP ↔ MAC from any ARP packet that reaches the controller."""
    arp_pkt = pkt.get_protocol(arp_lib.arp)
    if arp_pkt is None:
        return
    src_ip, src_mac = arp_pkt.src_ip, arp_pkt.src_mac
    if src_ip and src_mac and src_ip != "0.0.0.0":
        if controller._ip_to_mac.get(src_ip) != src_mac:
            logger.info("arp learned: %s -> %s", src_ip, src_mac)
        controller._ip_to_mac[src_ip] = src_mac
        controller._mac_to_ip[src_mac] = src_ip


def handle_vip_packet_in(controller, datapath, in_port, pkt, eth) -> bool:
    """Intercept ARP and IP packets destined for VIP addresses.

    Returns True if the packet was handled here — caller should return
    immediately without running L2 learning.
    Returns False to let normal packet processing continue.
    """

    if eth.ethertype == ether_types.ETH_TYPE_ARP:
        arp_pkt = pkt.get_protocol(arp_lib.arp)
        if (
            arp_pkt is not None
            and arp_pkt.opcode == arp_lib.ARP_REQUEST
            and _find_vip_binding(controller, arp_pkt.dst_ip) is not None
        ):
            logger.debug("vip ARP request: dpid=%s in_port=%s arp=%s", datapath.id, in_port, arp_pkt)
            return _reply_vip_arp(controller, datapath, in_port, arp_pkt)
        return False

    ip_pkt = pkt.get_protocol(ipv4.ipv4)
    if ip_pkt is None:
        return False

    dst_ip   = ip_pkt.dst
    src_ip   = ip_pkt.src
    src_mac  = eth.src
    ip_proto = ip_pkt.proto

    # Only ICMP (1), TCP (6), UDP (17): other protocols (ESP, GRE, ...) are not
    # valid OFP ip_proto match values here and would raise when passed to
    # OFPMatch.
    if ip_proto not in (1, 6, 17):
        return False

    if dst_ip in (controller.vip_server_ip, controller.vip_server_n2_ip):
        logger.debug("vip server packet-in: dpid=%s in_port=%s ip=%s",
                     datapath.id, in_port, ip_pkt)
        return _handle_vip_server(
            controller, datapath, in_port, pkt, src_mac, src_ip, ip_proto)

    for domain, data_vip in (("n1", controller.vip_data_n1_ip),
                             ("n2", controller.vip_data_n2_ip)):
        if dst_ip == data_vip:
            logger.debug("vip data %s packet-in: dpid=%s in_port=%s ip=%s",
                         domain, datapath.id, in_port, ip_pkt)
            return _handle_vip_data(
                controller, datapath, in_port, pkt, src_mac, src_ip,
                ip_proto, domain=domain)

    return False


def _reply_vip_arp(controller, datapath, in_port, arp_req) -> bool:
    """Craft and send an ARP reply for a VIP address request."""
    binding = _find_vip_binding(controller, arp_req.dst_ip)
    if binding is None:
        return False
    vip_ip, vip_mac = binding

    reply_pkt = packet.Packet()
    reply_pkt.add_protocol(eth_lib.ethernet(
        dst=arp_req.src_mac,
        src=vip_mac,
        ethertype=0x0806,
    ))
    reply_pkt.add_protocol(arp_lib.arp(
        opcode=arp_lib.ARP_REPLY,
        src_mac=vip_mac,
        src_ip=vip_ip,
        dst_mac=arp_req.src_mac,
        dst_ip=arp_req.src_ip,
    ))
    reply_pkt.serialize()

    ofproto = datapath.ofproto
    parser  = datapath.ofproto_parser
    out = parser.OFPPacketOut(
        datapath=datapath,
        buffer_id=ofproto.OFP_NO_BUFFER,
        in_port=ofproto.OFPP_CONTROLLER,
        actions=[parser.OFPActionOutput(in_port)],
        data=reply_pkt.data,
    )
    datapath.send_msg(out)
    logger.info(
        "vip arp reply: %s is-at %s  (requester ip=%s mac=%s)",
        vip_ip, vip_mac, arp_req.src_ip, arp_req.src_mac,
    )
    return True


def _handle_vip_server(controller, datapath, in_port, pkt, src_mac, src_ip, ip_proto) -> bool:
    ip_pkt = pkt.get_protocol(ipv4.ipv4)
    dst_ip = ip_pkt.dst
    if dst_ip == controller.vip_server_n2_ip:
        vip_ip, vip_mac = controller.vip_server_n2_ip, controller.vip_server_n2_mac
    else:
        vip_ip, vip_mac = controller.vip_server_ip, controller.vip_server_mac

    server = selection.select_server(controller, src_mac)
    if server is None:
        logger.warning("vip_server: pool empty, packet dropped")
        return True

    server_mac = server["mac"]
    server_ip  = controller._mac_to_ip.get(server_mac)
    if server_ip is None:
        logger.warning(
            "vip_server: IP unknown for mac=%s — awaiting ARP from backend",
            server_mac,
        )
        return True

    # Per-connection mode: scope the flow to this connection's client source
    # port so each fresh request connection gets its own DNAT/SNAT pair.
    # Per-client mode (default) passes client_port=None and keeps the original
    # behaviour.
    tcp_pkt = pkt.get_protocol(tcp_lib.tcp) if ip_proto == 6 else None
    client_port = (tcp_pkt.src_port
                   if (_VIP_SERVER_PER_CONNECTION and tcp_pkt is not None) else None)
    flows.install_vip_dnat_snat(
        controller,
        datapath, in_port, pkt,
        client_mac=src_mac,
        client_ip=src_ip,
        ip_proto=ip_proto,
        vip_ip=vip_ip,
        vip_mac=vip_mac,
        real_backend_ip=server_ip,
        real_backend_mac=server_mac,
        client_port=client_port,
    )

    logger.info(
        "vip_server: client=%s -> vip=%s -> real=%s",
        src_ip, vip_ip, server_ip,
    )

    return True


def _handle_vip_data(controller, datapath, in_port, pkt, src_mac, src_ip, ip_proto, *, domain) -> bool:
    # Per-connection mode: a pooled edge client opens N connections, and keying
    # forward rules on tcp_src fans them out to N storage backends.
    # INVARIANT: a connection is bound to one backend on its first SYN and is
    # NEVER re-selected afterwards (SYN retransmits and established packets
    # reuse the binding-map backend).  If that backend is gone — unregister
    # purges the map entry first — a non-SYN packet is dropped so the client
    # reconnects with a fresh SYN, never a mid-connection DNAT change, which
    # would break the established conntrack NAT.
    per_conn = _VIP_DATA_PER_CONNECTION and ip_proto == 6
    tcp_pkt = pkt.get_protocol(tcp_lib.tcp) if ip_proto == 6 else None
    tcp_src_port = tcp_pkt.src_port if tcp_pkt is not None else None

    if _VIP_DATA_PER_CONNECTION and not per_conn:
        # Only TCP packet-ins are eligible.  Falling through to the per-client
        # install path would create a rule with a wildcard tcp_src that matches
        # ANY source port and shadows per-connection selection for every later
        # connection from this client (observed: boot-time per-client rules
        # absorbed all episode traffic, zero per-connection rules installed).
        logger.debug(
            "vip_data(%s): per-conn mode, non-TCP packet from %s dropped",
            domain, src_ip,
        )
        return True

    if per_conn and tcp_src_port is None:
        logger.warning(
            "vip_data(%s): per-conn mode but no TCP src port for client=%s — dropping",
            domain, src_ip,
        )
        return True
    conn_key = (domain, src_mac, src_ip, tcp_src_port) if per_conn else None
    is_syn = bool(tcp_pkt is not None and (tcp_pkt.bits & 0x02))

    if per_conn:
        with controller._vip_data_conn_lock:
            pinned = controller._vip_data_conn_map.get(conn_key)
        if pinned is not None:
            # Bound already (first-SYN retransmit, established packet, or
            # post-unregister re-install): reuse the recorded backend, never
            # re-select — a different backend changes the DNAT mid-stream.
            pool = (controller.vip_storage_pool_n1 if domain == "n1"
                    else controller.vip_storage_pool_n2)
            if pinned in pool:
                storage = {"mac": pinned}
            else:
                with controller._vip_data_conn_lock:
                    controller._vip_data_conn_map.pop(conn_key, None)
                logger.info(
                    "vip_data(%s): pinned backend %s gone for client=%s:%s — "
                    "dropping (client will reconnect with a new SYN)",
                    domain, pinned, src_ip, tcp_src_port,
                )
                return True
        elif is_syn:
            storage = selection.select_storage(controller, domain, src_mac)
            if storage is not None:
                with controller._vip_data_conn_lock:
                    controller._vip_data_conn_map[conn_key] = storage["mac"]
        else:
            # Established packet with no binding (backend removed, map entry
            # purged): never re-select mid-stream.  Drop it — the client
            # reconnects with a new SYN and gets a fresh backend.
            logger.info(
                "vip_data(%s): no binding for established client=%s:%s — dropping",
                domain, src_ip, tcp_src_port,
            )
            return True
    else:
        storage = selection.select_storage(controller, domain, src_mac)

    if storage is None:
        logger.warning("vip_data(%s): pool empty, packet dropped", domain)
        return True

    storage_mac = storage["mac"]
    storage_ip  = controller._mac_to_ip.get(storage_mac)
    if storage_ip is None:
        logger.warning(
            "vip_data(%s): IP unknown for mac=%s — awaiting ARP from backend",
            domain, storage_mac,
        )
        return True

    if domain == "n1":
        vip_ip, vip_mac = controller.vip_data_n1_ip, controller.vip_data_n1_mac
    else:
        vip_ip, vip_mac = controller.vip_data_n2_ip, controller.vip_data_n2_mac

    parser = datapath.ofproto_parser
    ofproto = datapath.ofproto
    backend_port = controller.get_next_hop_port(datapath.id, src_mac, storage_mac)
    is_cross_network = False
    if backend_port is None:
        backend_loc = controller.host_attachment.get(storage_mac)
        if backend_loc is not None:
            _, backend_port = backend_loc
        elif storage_mac in controller.peer_hosts and _ROUTER_OVS_PORT > 0:
            backend_port = _ROUTER_OVS_PORT
            is_cross_network = True
            logger.info(
                "vip_data(%s): cross-network mac=%s -> router port %d",
                domain, storage_mac, _ROUTER_OVS_PORT,
            )
        else:
            logger.warning(
                "vip_data(%s): mac=%s not reachable from dpid=%s, skipping",
                domain, storage_mac, datapath.id,
            )
            return True

    flows.install_vip_data_forward_rule(
        controller, datapath,
        vip_ip=vip_ip, vip_mac=vip_mac, domain=domain,
        client_mac=src_mac, client_ip=src_ip,
        backend_ip=storage_ip,
        backend_mac=storage_mac,
        backend_port=backend_port,
        is_cross_network=is_cross_network,
        client_src_port=tcp_src_port if per_conn else None,
    )

    # Idempotent: an identical match means OVS replaces the rule in place.
    flows.install_vip_data_reply_rule(
        controller, datapath,
        client_mac=src_mac, client_ip=src_ip,
        vip_mac=vip_mac, in_port=in_port, domain=domain,
    )

    # First packet out now, before the rules above take effect.  It must carry
    # ct(commit, nat(dst=...)) so the SYN seeds the conntrack entry that the
    # reply rule's ct(zone=N,nat) then reverse-NATs.
    dnat_eth_dst = (_ROUTER_MAC if is_cross_network and _ROUTER_MAC
                    else storage_mac)
    ct_action = parser.NXActionCT(
        flags=1,
        zone_src=None,
        zone_ofs_nbits=flows._CT_ZONE[domain],
        recirc_table=ofproto.OFPTT_ALL,
        alg=0,
        actions=[
            parser.NXActionNAT(
                flags=2,            # 2 = NX_NAT_F_DST (destination NAT)
                range_ipv4_min=storage_ip,
                range_ipv4_max=storage_ip,
            ),
        ],
    )
    dnat_actions = [
        ct_action,
        parser.OFPActionSetField(eth_dst=dnat_eth_dst),
        parser.OFPActionOutput(backend_port),
    ]
    out = parser.OFPPacketOut(
        datapath=datapath,
        buffer_id=ofproto.OFP_NO_BUFFER,
        in_port=in_port,
        actions=dnat_actions,
        data=pkt.data,
    )
    datapath.send_msg(out)

    logger.info(
        "vip_data(%s): client=%s -> vip=%s -> real=%s",
        domain, src_ip, vip_ip, storage_ip,
    )

    return True


def install_vip_arp_punt_rules(controller, datapath) -> None:
    """Punt ARP requests for VIP IPs to the controller (priority 100).

    Overrides the ARP flood rule (priority 1) installed by the topology
    mixin.  The controller replies with a crafted ARP reply packet
    (see _reply_vip_arp).  Pure OFP 1.3 is used throughout — no Nicira
    extensions required.
    """
    parser  = datapath.ofproto_parser
    ofproto = datapath.ofproto

    for vip_ip, _ in _iter_vip_bindings(controller):
        match   = parser.OFPMatch(eth_type=0x0806, arp_tpa=vip_ip)
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        controller._install_flow(datapath, priority=100, match=match, actions=actions)

    logger.info("vip arp punt rules installed: dpid=%s", datapath.id)


def install_vip_punt_rules(controller, datapath) -> None:
    """Punt IPv4 packets destined for VIP addresses to the controller (priority 100).

    Once DNAT rules (priority 200) are installed they take precedence and
    subsequent packets bypass the controller entirely.  When the DNAT rule
    expires (idle_timeout or hard_timeout) the punt rule resumes and
    triggers fresh backend selection.
    """
    parser  = datapath.ofproto_parser
    ofproto = datapath.ofproto

    for vip_ip, _ in _iter_vip_bindings(controller):
        match   = parser.OFPMatch(eth_type=0x0800, ipv4_dst=vip_ip)
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        controller._install_flow(datapath, priority=100, match=match, actions=actions)

    logger.info("vip ip punt rules installed: dpid=%s", datapath.id)
