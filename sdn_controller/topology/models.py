from __future__ import annotations

from pydantic import BaseModel


class TopologyHostEntry(BaseModel):
    mac: str
    dpid: int
    port_no: int
    ip: str | None = None


class TopologyLinkEntry(BaseModel):
    src_dpid: int
    src_port_no: int
    dst_dpid: int


class TopologyNetworkSection(BaseModel):
    hosts: list[TopologyHostEntry] = []
    links: list[TopologyLinkEntry] = []
    switches: list[int] = []


class TopologySnapshot(BaseModel):
    type: str = "topology"
    network_id: str
    networks: dict[str, TopologyNetworkSection] = {}
    hosts: list[TopologyHostEntry] = []
    links: list[TopologyLinkEntry] = []
    switches: list[int] = []
    hops: dict = {}
    ts: float = 0.0
    avg_hop_count: float = 0.0
    # MAC role sets — advertised by sender so peer can merge them into its own pools
    server_macs:     list[str] = []
    storage_macs_n1: list[str] = []
    storage_macs_n2: list[str] = []
    # MAC → RS role ("primary"/"secondary"/"") for the publishing LAN's storage nodes;
    # consumed by ``resolve_peer_primary``. ``STANDALONE_CACHE`` maps to ``""`` and must
    # never be advertised as an RS member.
    storage_roles:   dict[str, str] = {}
