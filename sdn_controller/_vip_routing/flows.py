"""DNAT/SNAT flow-rule construction and PacketOut for VIP routing."""

from .config import (
    _VIP_IDLE_TIMEOUT, _VIP_HARD_TIMEOUT,
    _VIP_DATA_IDLE_TIMEOUT,
    _VIP_DATA_PER_CONNECTION,
    _ROUTER_OVS_PORT, _ROUTER_MAC,
    ClientVipBinding,
    logger,
)
from ..scaling_config import _VIP_FLOW_ISOLATION, _VIP_SERVER_PER_CONNECTION


# Flow cookies for VIP_DATA forward rules, keyed by domain.
# All per-client forward rules for a domain share the same cookie, allowing
# bulk OFPFC_DELETE on unregister_storage_backend without tracking
# individual clients.
_COOKIE_VIP_DATA_FWD = {
    "n1": 0x56494441,  # 'VIDA' in hex
    "n2": 0x56494442,  # 'VIDB' in hex
}

# Conntrack zone per VIP_DATA domain: forward rules commit in the domain's
# zone and reply rules look the connection up in that same zone.
_CT_ZONE = {"n1": 1, "n2": 2}

# Fixed topology property: n1 backends always live on LAN1, n2 on LAN2.  It is
# what distinguishes the two reply rules (see install_vip_data_reply_rule).
_BACKEND_SUBNET = {"n1": "10.0.0.0/24", "n2": "10.0.1.0/24"}


def install_vip_data_forward_rule(
    controller, datapath,
    vip_ip, vip_mac, domain,
    client_mac, client_ip,
    backend_ip, backend_mac, backend_port,
    is_cross_network=False,
    client_src_port=None,
):
    """Install/update a per-client or per-connection forward rule for VIP_DATA.

    Default (client_src_port is None, ``VIP_DATA_PER_CONNECTION_FLOWS=0``):
    the match is scoped to one client (eth_src / ipv4_src), preserving the
    per-client WSM load distribution from the current static-NAT design.

    Per-connection (client_src_port is not None, ``VIP_DATA_PER_CONNECTION_FLOWS=1``):
    the match also carries ``tcp_src`` so a pooled edge client
    (``EDGE_MONGO_MAX_POOL_SIZE>1``) fans its connections out to different
    storage backends.  These flows are installed on the connection SYN only
    and expire on ``_VIP_DATA_IDLE_TIMEOUT`` idle (hard_timeout=0), so an
    established connection is NEVER re-pinned mid-stream while it is active
    (re-pinning would DNAT an already-committed conntrack connection to a new
    backend port and break it).  They are cleaned by the domain-cookie bulk
    delete on unregister_storage_backend.

    Uses ct(commit, nat(dst=backend_ip)) so OVS tracks each connection
    independently.  Multiple per-client/per-connection forward rules share
    the same domain cookie — bulk-deleted on unregister_storage_backend.

    Cross-network: when the backend is on the peer LAN, eth_dst must be
    the router's MAC so the router accepts the frame for L3 forwarding.
    """
    parser = datapath.ofproto_parser
    ofproto = datapath.ofproto

    per_connection = _VIP_DATA_PER_CONNECTION and client_src_port is not None

    if per_connection:
        match = parser.OFPMatch(
            eth_type=0x0800,
            eth_src=client_mac,
            eth_dst=vip_mac,
            ipv4_src=client_ip,
            ipv4_dst=vip_ip,
            ip_proto=6,
            tcp_src=client_src_port,
            tcp_dst=27018,
        )
    else:
        match = parser.OFPMatch(
            eth_type=0x0800,
            eth_src=client_mac,
            eth_dst=vip_mac,
            ipv4_src=client_ip,
            ipv4_dst=vip_ip,
            ip_proto=6,
            tcp_dst=27018,
        )

    dnat_eth_dst = (_ROUTER_MAC if is_cross_network and _ROUTER_MAC
                    else backend_mac)

    # zone_src=None means the zone is an immediate value, and zone_ofs_nbits
    # then carries the zone number itself instead of naming a register.
    ct_action = parser.NXActionCT(
        flags=1,                    # NX_CT_F_COMMIT
        zone_src=None,
        zone_ofs_nbits=_CT_ZONE[domain],
        recirc_table=ofproto.OFPTT_ALL,
        alg=0,
        actions=[
            parser.NXActionNAT(
                flags=2,            # 2 = NX_NAT_F_DST (destination NAT)
                range_ipv4_min=backend_ip,
                range_ipv4_max=backend_ip,
            ),
        ],
    )

    actions = [
        ct_action,
        parser.OFPActionSetField(eth_dst=dnat_eth_dst),
        parser.OFPActionOutput(backend_port),
    ]

    if per_connection:
        # Idle-expire like per-client flows (a closed connection's flow must
        # clear so its port can be re-pinned), but NEVER hard-expire: an
        # active connection must not be forcibly re-pinned mid-stream.  On
        # idle expiry the next packet packet-ins and the controller reinstalls
        # the SAME backend from the binding map (ingress._handle_vip_data),
        # never a fresh select.  Idle is env-driven (_VIP_DATA_IDLE_TIMEOUT)
        # so a connection waiting on a slow, queued backend is not dropped.
        idle_timeout = _VIP_DATA_IDLE_TIMEOUT
        hard_timeout = 0
    else:
        # No delete-before-install: a re-selection yields the same match
        # (eth_src + ipv4_src + VIP fields), and OVS replaces a
        # same-priority/same-match flow in place.
        idle_timeout = _VIP_DATA_IDLE_TIMEOUT
        hard_timeout = 120

    controller._install_flow(
        datapath,
        priority=200,
        match=match,
        actions=actions,
        idle_timeout=idle_timeout,
        hard_timeout=hard_timeout,
        cookie=_COOKIE_VIP_DATA_FWD[domain],
    )

    mode = "per-connection" if per_connection else "per-client"
    logger.info(
        "vip_data(%s): %s forward rule installed — client=%s:%s vip=%s "
        "backend=%s (idle=%ss hard=%ss cookie=0x%x)",
        domain, mode, client_ip,
        client_src_port if per_connection else "*",
        vip_ip, backend_ip, idle_timeout, hard_timeout,
        _COOKIE_VIP_DATA_FWD[domain],
    )


def install_vip_data_reply_rule(
    controller, datapath,
    client_mac, client_ip, vip_mac, in_port, domain,
):
    """Install a reply rule for VIP_DATA traffic for one client+domain.

    The reply rule matches MongoDB reply packets (tcp_src=27018) addressed
    to the client and sends them through conntrack with ct(zone=N,nat) for
    automatic reverse NAT (backend IP → VIP IP).  After NAT, the source MAC
    is rewritten to the VIP MAC so the client sees the VIP as the source.

    The ipv4_src match on the backend subnet differentiates n1 (10.0.0.0/24)
    from n2 (10.0.1.0/24) so both reply rules can coexist for the same client
    without collision.  The ct(zone=N,nat) action then ensures the kernel looks
    up the connection in the correct conntrack zone.

    IMPORTANT: This rule does NOT match on ct_state because the reply packet
    has not been through ct() yet.  Instead, the ct(zone=N,nat) action in the
    pipeline processes the packet through conntrack, which applies reverse
    NAT and sets ct_state for the kernel's own state tracking.
    """
    parser = datapath.ofproto_parser
    ofproto = datapath.ofproto

    # No ct_state match: the reply has not been through ct() yet.  The backend
    # subnet is the domain differentiator — it replaces the broken ct_zone
    # match of the original design.
    match = parser.OFPMatch(
        eth_type=0x0800,
        eth_dst=client_mac,
        ipv4_src=_BACKEND_SUBNET[domain],
        ipv4_dst=client_ip,
        ip_proto=6,
        tcp_src=27018,
    )

    # ct(zone=N, nat) pushes the packet through conntrack in that zone, which
    # finds the connection and applies the reverse of the committed NAT.  Both
    # flags are 0: no commit (the connection already exists) and no explicit
    # direction on NXActionNAT, i.e. take it from connection state.
    ct_action = parser.NXActionCT(
        flags=0,
        zone_src=None,
        zone_ofs_nbits=_CT_ZONE[domain],
        recirc_table=ofproto.OFPTT_ALL,  # OFPTT_ALL = no recirculation
        alg=0,
        actions=[
            parser.NXActionNAT(
                flags=0,
            ),
        ],
    )

    actions = [
        ct_action,
        parser.OFPActionSetField(eth_src=vip_mac),
        parser.OFPActionOutput(in_port),
    ]
    controller._install_flow(
        datapath,
        priority=200,
        match=match,
        actions=actions,
        idle_timeout=0,    # conntrack owns the lifecycle
        hard_timeout=0,    # never expire
        cookie=0,          # never bulk-deleted
    )


def _delete_flow_by_cookie(controller, datapath, cookie):
    """Delete all flows matching a specific cookie value."""
    parser = datapath.ofproto_parser
    ofproto = datapath.ofproto
    mod = parser.OFPFlowMod(
        datapath=datapath,
        cookie=cookie,
        cookie_mask=0xFFFFFFFFFFFFFFFF,
        table_id=ofproto.OFPTT_ALL,
        command=ofproto.OFPFC_DELETE,
        out_port=ofproto.OFPP_ANY,
        out_group=ofproto.OFPG_ANY,
        match=parser.OFPMatch(),  # wildcard match — cookie_mask is the filter
    )
    datapath.send_msg(mod)


def delete_vip_server_client_flows(controller, datapath, binding: ClientVipBinding):
    """Delete the exact VIP_SERVER DNAT+SNAT pair for one client+backend (RQ3).

    Uses the recorded binding for an EXACT match so it never matches the
    never-expiring ``VIP_DATA`` reply rule (``tcp_src=27018``) or another
    backend's ``VIP_SERVER`` SNAT rule. After deletion the priority-100 punt
    rule resumes → the next SYN triggers a fresh ``select_server()``.
    """
    parser = datapath.ofproto_parser
    ofproto = datapath.ofproto

    dnat_fields = {
        "eth_type": 0x0800,
        "eth_src": binding.client_mac,
        "eth_dst": binding.vip_mac,
        "ipv4_src": binding.client_ip,
        "ipv4_dst": binding.vip_ip,
        "ip_proto": 6,
    }
    snat_fields = {
        "eth_type": 0x0800,
        "eth_src": binding.snat_eth_src,
        "eth_dst": binding.client_mac,
        "ipv4_src": binding.backend_ip,
        "ipv4_dst": binding.client_ip,
        "ip_proto": 6,
    }
    # Per-connection binding: delete the exact connection pair (tcp_src on
    # forward, tcp_dst on reply) so a sibling connection's flow is untouched.
    if binding.client_port:
        dnat_fields["tcp_src"] = binding.client_port
        snat_fields["tcp_dst"] = binding.client_port
    dnat_match = parser.OFPMatch(**dnat_fields)
    snat_match = parser.OFPMatch(**snat_fields)
    for match in (dnat_match, snat_match):
        mod = parser.OFPFlowMod(
            datapath=datapath,
            table_id=ofproto.OFPTT_ALL,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match,
        )
        datapath.send_msg(mod)
    logger.debug(
        "vip_server: flow delete issued — client=%s vip=%s backend=%s",
        binding.client_ip, binding.vip_ip, binding.backend_ip,
    )


def delete_vip_data_forward_rule(controller, datapath, domain):
    """Delete the forward rule for a VIP_DATA domain.

    After deletion, new SYNs to the VIP will be punted to the controller
    (priority-100 punt rule), triggering fresh select_storage().
    Established connections survive in conntrack state.

    Called from unregister_storage_backend via state.py.
    """
    _delete_flow_by_cookie(controller, datapath, _COOKIE_VIP_DATA_FWD[domain])
    logger.info(
        "vip_data(%s): forward rule deleted (cookie=0x%x)",
        domain, _COOKIE_VIP_DATA_FWD[domain],
    )


def install_vip_dnat_snat(
    controller, datapath, in_port, pkt, *,
    client_mac, client_ip, ip_proto, vip_ip, vip_mac,
    real_backend_ip, real_backend_mac,
    client_port=None, idle_timeout=None, hard_timeout=None,
) -> None:
    """Install a DNAT + SNAT flow rule pair and Packet-Out the first packet.

    DNAT (forward):
        match(eth_dst=VIP_MAC, ipv4_src=client, ipv4_dst=VIP, ip_proto)
        → set_field(eth_dst=real_mac, ipv4_dst=real_ip), output toward backend

    SNAT (return):
        match(eth_src=backend_mac, eth_dst=client_mac,
              ipv4_src=backend, ipv4_dst=client, ip_proto)
        → set_field(eth_src=VIP_mac, ipv4_src=VIP_ip), output to client port

    Transport ports are excluded in per-client mode so one steady-state
    rule can cover concurrent connections from the same client. When
    ``client_port`` is set (VIP_SERVER per-connection mode,
    ``VIP_SERVER_PER_CONNECTION_FLOWS=1``), the DNAT/SNAT pair is scoped to
    the client's ephemeral source port so each fresh request connection gets
    its own exact flow pair.
    """
    parser  = datapath.ofproto_parser
    ofproto = datapath.ofproto

    is_cross_network = False
    backend_port = controller.get_next_hop_port(datapath.id, client_mac, real_backend_mac)
    if backend_port is None:
        backend_loc = controller.host_attachment.get(real_backend_mac)
        if backend_loc is not None:
            _, backend_port = backend_loc
        elif real_backend_mac in controller.peer_hosts and _ROUTER_OVS_PORT > 0:
            backend_port = _ROUTER_OVS_PORT
            is_cross_network = True
            logger.info(
                "dnat/snat: cross-network mac=%s -> router port %d",
                real_backend_mac, _ROUTER_OVS_PORT,
            )
        else:
            logger.warning(
                "dnat/snat: mac=%s not reachable from dpid=%s, skipping",
                real_backend_mac, datapath.id,
            )
            return

    # ipv4_src scopes the rule to one client, so simultaneous clients each
    # keep the backend they selected.
    dnat_fields = {
        "eth_type": 0x0800,
        "eth_src": client_mac,
        "eth_dst": vip_mac,
        "ipv4_src": client_ip,
        "ipv4_dst": vip_ip,
        "ip_proto": ip_proto,
    }
    # Per-connection mode: scope the DNAT to the client's ephemeral source
    # port, so the request_complete delete targets exactly that connection
    # and never a sibling's flow.
    if _VIP_SERVER_PER_CONNECTION and client_port:
        dnat_fields["tcp_src"] = client_port
    dnat_match = parser.OFPMatch(**dnat_fields)
    # Cross-network: the frame must be addressed to the router's LAN MAC so
    # the router's kernel IP stack accepts it for L3 forwarding.  Sending
    # eth_dst=real_backend_mac causes the router to silently drop the frame
    # (not destined for any of its own interfaces).
    dnat_eth_dst = (_ROUTER_MAC if is_cross_network and _ROUTER_MAC
                    else real_backend_mac)
    dnat_actions = [
        parser.OFPActionSetField(eth_dst=dnat_eth_dst),
        parser.OFPActionSetField(ipv4_dst=real_backend_ip),
        parser.OFPActionOutput(backend_port),
    ]
    controller._install_flow(
        datapath, priority=200,
        match=dnat_match, actions=dnat_actions,
        idle_timeout=idle_timeout if idle_timeout is not None else _VIP_IDLE_TIMEOUT,
        hard_timeout=hard_timeout if hard_timeout is not None else _VIP_HARD_TIMEOUT,
    )

    # eth_dst=client_mac + ipv4_dst=client_ip are critical: without them ALL
    # outgoing traffic from the backend (to any host) would get its source
    # rewritten to VIP_IP, breaking the backend's non-VIP connections.
    #
    # Cross-network: the router does L3 forwarding between LANs, which
    # rewrites eth_src to the router's own LAN MAC.  The return packet
    # arrives at this switch with eth_src=ROUTER_MAC, not the real backend
    # MAC.  We must match on the router MAC to intercept return traffic.
    if is_cross_network and _ROUTER_MAC:
        snat_eth_src = _ROUTER_MAC
        logger.debug(
            "snat: cross-network, matching router mac=%s instead of backend mac=%s",
            _ROUTER_MAC, real_backend_mac,
        )
    else:
        snat_eth_src = real_backend_mac
    snat_fields = {
        "eth_type": 0x0800,
        "eth_src": snat_eth_src,
        "eth_dst": client_mac,
        "ipv4_src": real_backend_ip,
        "ipv4_dst": client_ip,
        "ip_proto": ip_proto,
    }
    # The matching SNAT is scoped by that same port, as its tcp_dst, so the
    # reply of one connection never matches another's rule.
    if _VIP_SERVER_PER_CONNECTION and client_port:
        snat_fields["tcp_dst"] = client_port
    snat_match = parser.OFPMatch(**snat_fields)
    snat_actions = [
        parser.OFPActionSetField(eth_src=vip_mac),
        parser.OFPActionSetField(ipv4_src=vip_ip),
        parser.OFPActionOutput(in_port),
    ]
    controller._install_flow(
        datapath, priority=200,
        match=snat_match, actions=snat_actions,
        idle_timeout=idle_timeout if idle_timeout is not None else _VIP_IDLE_TIMEOUT,
        hard_timeout=hard_timeout if hard_timeout is not None else _VIP_HARD_TIMEOUT,
    )

    # Flow isolation: record the client->backend binding.  Only active with
    # VIP_FLOW_ISOLATION=1; off (the default) this block is skipped entirely,
    # so canonical/RQ1/RQ2 runs are unaffected.  On re-selection the old exact
    # DNAT+SNAT pair is deleted first (on every datapath), otherwise the
    # previous backend's SNAT rule lingers with a different match.
    if _VIP_FLOW_ISOLATION:
        # Per-connection mode keys by (client_mac, client_port) so concurrent
        # requests from one client stay independent; per-client mode keys by
        # client_mac and the newest selection wins.
        _key = ((client_mac, client_port)
                if (_VIP_SERVER_PER_CONNECTION and client_port) else client_mac)
        with controller._warm_lock:
            old = controller._vip_server_client_map.get(_key)
            if old is not None and (old.backend_mac != real_backend_mac
                                    or old.vip_ip != vip_ip):
                try:
                    for _dp in controller.datapaths:
                        delete_vip_server_client_flows(controller, _dp, old)
                except Exception:
                    logger.exception(
                        "vip_server: failed to delete old binding for client=%s",
                        client_mac,
                    )
            controller._vip_server_client_map[_key] = ClientVipBinding(
                client_mac=client_mac,
                client_ip=client_ip,
                backend_mac=real_backend_mac,
                backend_ip=real_backend_ip,
                vip_ip=vip_ip,
                vip_mac=vip_mac,
                snat_eth_src=snat_eth_src,
                client_port=client_port or 0,
            )

    logger.info(
        "dnat/snat installed: vip=%s -> real=%s (idle=%ds hard=%ds)",
        vip_ip,
        real_backend_ip,
        idle_timeout if idle_timeout is not None else _VIP_IDLE_TIMEOUT,
        hard_timeout if hard_timeout is not None else _VIP_HARD_TIMEOUT,
    )

    # First packet out now, while the rules above are still propagating.
    out = parser.OFPPacketOut(
        datapath=datapath,
        buffer_id=ofproto.OFP_NO_BUFFER,
        in_port=in_port,
        actions=dnat_actions,
        data=pkt.data,
    )
    datapath.send_msg(out)
