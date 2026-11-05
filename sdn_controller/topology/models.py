from __future__ import annotations

from pydantic import BaseModel


class TopologyHostEntry(BaseModel):
    mac: str
    dpid: int
    port_no: int
    ip: str | None = None


class TopologyNetworkSection(BaseModel):
    hosts: list[TopologyHostEntry] = []


class TopologySnapshot(BaseModel):
    type: str = "topology"
    network_id: str
    networks: dict[str, TopologyNetworkSection] = {}
    avg_hop_count: float = 0.0
    # MAC role sets — advertised by sender so peer can merge them into its own pools
    server_macs:     list[str] = []
    storage_macs_n1: list[str] = []
    storage_macs_n2: list[str] = []
