"""
node_common.py — Shared types, constants, and base class for node lifecycle managers.

Consumed by:
  - compute_node_manager.py  (ComputeNodeAdder)
  - storage_node_manager.py  (StorageNodeAdder)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import bisect
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

logger = logging.getLogger("os_ken.node_manager")

# Override via NODE_SCRIPTS_DIR when the controller runs in a container.
_pkg_root = Path(__file__).parent.parent
SCRIPTS_DIR = Path(os.environ.get("NODE_SCRIPTS_DIR", str(_pkg_root / "scripts" / "network")))

_RESULT_IP_RE  = re.compile(r"^RESULT_IP=(\S+)",  re.MULTILINE)
_RESULT_MAC_RE = re.compile(r"^RESULT_MAC=(\S+)", re.MULTILINE)


class NodeOperationState(Enum):
    PENDING           = auto()
    RUNNING_CONTAINER = auto()
    ATTACHING_NETWORK = auto()
    JOINING_RS        = auto()  # storage only
    DONE              = auto()
    FAILED            = auto()


@dataclass
class StepTimings:
    """Wall-clock durations (seconds) for each lifecycle phase.

    ``total_s`` is measured end-to-end and includes inter-step overhead; it is
    NOT the arithmetic sum of the individual fields.
    """
    docker_run_s:       float = 0.0
    network_attach_s:   float = 0.0
    replica_set_join_s: float = 0.0  # storage only; absorbed in network_attach_s when not split
    total_s:            float = 0.0


@dataclass
class NodeResult:
    success:        bool
    container_name: str
    ip:             str | None
    mac:            str | None
    timings:        StepTimings
    state:          NodeOperationState
    stdout:         str = ""
    stderr:         str = ""


@dataclass
class RemovalTimings:
    """Wall-clock durations (seconds) for each removal phase."""
    drain_signal_s:    float = 0.0
    drain_wait_s:      float = 0.0   # bounded by container exit or the idle timeout
    network_cleanup_s: float = 0.0   # teardown script: flow flush + docker/OVS teardown
    total_s:           float = 0.0


@dataclass
class RemovalResult:
    success:        bool
    container_name: str
    mac:            str | None
    timings:        RemovalTimings
    state:          NodeOperationState
    stdout:         str = ""
    stderr:         str = ""


@dataclass
class NodeInfo:
    """Records a dynamically added node for LIFO scale-down selection.

    Stored by Thread 2 (via consume_addition_completions) when Thread 3
    successfully adds a node. Supplies all fields needed to build
    ScaleDownComputeAlert or ScaleDownDataAlert without re-discovery.
    """
    mac:               str
    lan:               int
    network_id:        str
    name:              str
    ip:                str
    node_type:         str   # "compute" | "storage"
    rs_name:           str = ""
    primary_container: str = ""
    port:              int = 27018
    spawn_started_monotonic_s: float = 0.0
    ready_logged: bool = False
    standby_reserved: bool = False  # storage persistent reserve — excluded from VIP & ordinary accounting


def log_ready_timing(
    container_name: str,
    node_type: str,
    source: str,
    total_s: float,
    state: str = "READY",
) -> None:
    """Emit a stable ready-to-serve timing line for offline analysis."""
    logger.info(
        "[node_ready] timing  container=%s  node_type=%s  source=%s"
        "  total=%.2fs  state=%s",
        container_name,
        node_type,
        source,
        total_s,
        state,
    )


class _BaseNodeAdder:
    """Shared low-level helpers used by both ComputeNodeAdder and StorageNodeAdder."""

    def log_timings(self, result: NodeResult) -> None:
        t = result.timings
        logger.info(
            "[node_add] timing  container=%s  docker_run=%.2fs  net_attach=%.2fs"
            "  rs_join=%.2fs  total=%.2fs  state=%s",
            result.container_name,
            t.docker_run_s, t.network_attach_s,
            t.replica_set_join_s, t.total_s,
            result.state.name,
        )

    def log_removal_timings(self, result: RemovalResult) -> None:
        t = result.timings
        logger.info(
            "[node_remove] timing  container=%s  drain_signal=%.2fs"
            "  net_cleanup=%.2fs  total=%.2fs  state=%s",
            result.container_name,
            t.drain_signal_s, t.network_cleanup_s, t.total_s,
            result.state.name,
        )

    def _container_state(self, name: str) -> str | None:
        """Return the Docker container status string, or None if not found."""
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _run_script(self, script: Path, args: list[str]) -> tuple[bool, str | None, str | None, str, str]:
        cmd = ["/bin/bash", str(script), *args]
        logger.debug("[node_add] running script: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        ip:  str | None = None
        mac: str | None = None
        m = _RESULT_IP_RE.search(result.stdout)
        if m:
            ip = m.group(1)
        m = _RESULT_MAC_RE.search(result.stdout)
        if m:
            mac = m.group(1)
        ok = result.returncode == 0
        if ok:
            logger.debug("[node_add] script ok  ip=%s mac=%s\n%s", ip, mac, result.stdout)
        else:
            logger.error(
                "[node_add] script failed  cmd=%s\n--- stdout ---\n%s\n--- stderr ---\n%s",
                " ".join(cmd), result.stdout, result.stderr,
            )
        return ok, ip, mac, result.stdout, result.stderr

    def _run_cmd(self, cmd: list[str]) -> tuple[bool, str, str]:
        logger.debug("[node_add] running cmd: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        ok = result.returncode == 0
        if ok:
            logger.debug("[node_add] cmd ok: %s", result.stdout.strip())
        else:
            logger.error(
                "[node_add] command failed  cmd=%s\nstdout=%s\nstderr=%s",
                " ".join(cmd), result.stdout, result.stderr,
            )
        return ok, result.stdout, result.stderr

    def _cleanup_container(self, name: str) -> None:
        """Best-effort stop + remove + volume removal. Does not raise on failure."""
        logger.debug("[node_add] cleanup: stopping and removing container %s", name)
        for cmd in (
            ["docker", "stop", name],
            ["docker", "rm", name],
            ["docker", "volume", "rm", f"{name}-data"],
        ):
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.debug("[node_add] cleanup %s: %s", " ".join(cmd), result.stderr.strip())


class IpAllocator:
    """Per-LAN IP allocator for dynamic service nodes (.6–.55).

    Suffixes 1–5 are reserved for static infrastructure on each LAN:
        .1  router (default gateway)
        .2  edge_server (compute)
        .3  (reserved)
        .4  edge_storage_server (MongoDB primary)
        .5  local_state_server (aggregator)

    Suffixes 56–105 are reserved for test clients (namespace-based).
    Suffixes 252–254 are reserved for VIPs:
        .253 VIP_SERVER_N1 (LAN1) / VIP_SERVER_N2 (LAN2)
        .254 VIP_DATA_N1 (LAN1) / VIP_DATA_N2 (LAN2)

    Dynamic nodes start at suffix 6 to avoid IP collisions.

    MAC addresses are derived deterministically:
        00:00:00:00:{lan:02x}:{suffix:02x}
    """

    _MIN_SUFFIX = 6
    _MAX_SUFFIX = 55

    def __init__(self, lan: int) -> None:
        self._lan = lan
        self._subnet_prefix = f"10.0.{lan - 1}"   # lan1 → 10.0.0, lan2 → 10.0.1
        self._free: list[int] = list(range(self._MIN_SUFFIX, self._MAX_SUFFIX + 1))
        self._in_use: set[int] = set()

    def allocate(self) -> tuple[str, str]:
        """Return (ip, mac) for the next available suffix. Raises if exhausted."""
        if not self._free:
            raise RuntimeError(f"IP pool exhausted for LAN {self._lan}")
        suffix = self._free.pop(0)
        self._in_use.add(suffix)
        ip  = f"{self._subnet_prefix}.{suffix}"
        mac = f"00:00:00:00:{self._lan:02x}:{suffix:02x}"
        return ip, mac

    def release(self, ip: str) -> None:
        """Return an IP to the free pool."""
        suffix = int(ip.rsplit(".", 1)[1])
        if suffix in self._in_use:
            self._in_use.discard(suffix)
            bisect.insort(self._free, suffix)
