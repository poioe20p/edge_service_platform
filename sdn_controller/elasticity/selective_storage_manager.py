"""
selective_storage_manager.py — Timed, idempotent lifecycle for Tier 1
selective-sync (``edge_selective_storage``) nodes.

Mirrors :mod:`storage_node_manager` but:
  - runs ``mongod`` standalone (no ``--replSet`` and no ``rs.add()``);
  - passes ``OWNER_HOST`` / ``COLLECTIONS_JSON`` / ``MAX_TTL_S`` env vars
    instead of RS seed config;
  - exposes ``reconfigure(...)`` which POSTs the live forwarder set to the
    container's ``/forwarder_config`` admin endpoint, and
    ``initiate_drain(...)`` which POSTs ``/drain`` for two-phase teardown
    mirroring :meth:`ComputeNodeAdder.initiate_drain`.

Design reference:
``docs/operation/elasticy_manager/implementation/tier1_selective_sync/README.md``
(§§1, 2 in the consolidated write-up).
"""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import time
import requests
from typing import Mapping

from .compute_node_manager import PendingDrain
from .node_common import (
    SCRIPTS_DIR,
    NodeOperationState,
    NodeResult,
    RemovalResult,
    RemovalTimings,
    StepTimings,
    _BaseNodeAdder,
)

logger = logging.getLogger("os_ken.node_manager")

# Tier 1 supervisor admin port (see edge_selective_storage/config.py — ADMIN_PORT default 5001).
_ADMIN_PORT = 5001
_DRAIN_TIMEOUT_S = 2.0
_RECONFIGURE_TIMEOUT_S = 5.0

_READINESS_POLL_INTERVAL_S = 0.5
_READINESS_MAX_WAIT_S = 30.0


def _wait_for_port(ip: str, port: int, timeout: float) -> bool:
    """Block until {ip}:{port} accepts a TCP connection, or *timeout* expires.

    Used by the elasticity manager to confirm a Tier 1 container's admin
    server is listening before marking the spawn as ACTIVE.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((ip, port), timeout=1.0):
                return True
        except (OSError, ConnectionRefusedError, TimeoutError):
            time.sleep(_READINESS_POLL_INTERVAL_S)
    return False


class SelectiveStorageNodeAdder(_BaseNodeAdder):
    """Stateless helper for Tier 1 selective-sync node lifecycle."""

    def container_is_present(self, name: str) -> bool:
        """Return True while Docker still tracks the selective node container."""
        return self._container_state(name) is not None

    def add_selective_storage_node(
        self,
        lan: int,
        name: str,
        primary_host: str,
        collections: Mapping[str, tuple[str, ...]],
        max_ttl_s: int,
        ip: str | None = None,
        mac: str | None = None,
    ) -> NodeResult:
        """Spawn a Tier 1 container and attach it to OVS LAN ``lan``.

        Steps:
          1. ``docker run --network none edge_selective_storage``
          2. ``add_selective_network_node.sh --lan <lan> --name <name>``
        """
        timings = StepTimings()
        t_total = time.perf_counter()

        logger.info(
            "[node_add] step=docker_run container=%s tier=1 owner=%s collections=%d",
            name, primary_host, len(collections),
        )
        t0 = time.perf_counter()
        ok, stdout, stderr = self._docker_run_selective(
            name, lan, primary_host, collections, max_ttl_s,
        )
        timings.docker_run_s = time.perf_counter() - t0
        if not ok:
            timings.total_s = time.perf_counter() - t_total
            return NodeResult(False, name, None, None, timings,
                              NodeOperationState.FAILED, stdout, stderr)

        logger.info("[node_add] step=attach_network container=%s lan=%d (selective)", name, lan)
        t0 = time.perf_counter()
        script_args = ["--lan", str(lan), "--name", name]
        if ip:
            script_args += ["--ip", ip]
        if mac:
            script_args += ["--mac", mac]
        ok, ip2, mac2, stdout2, stderr2 = self._run_script(
            SCRIPTS_DIR / "add_selective_network_node.sh",
            script_args,
        )
        timings.network_attach_s = time.perf_counter() - t0
        timings.total_s = time.perf_counter() - t_total

        if not ok:
            self._cleanup_container(name)
            return NodeResult(
                False, name, None, None, timings, NodeOperationState.FAILED,
                stdout + stdout2, stderr + stderr2,
            )

        logger.debug("[node_add] tier1 attach complete container=%s ip=%s mac=%s",
                     name, ip2, mac2)
        return NodeResult(True, name, ip2, mac2, timings, NodeOperationState.DONE,
                          stdout + stdout2, stderr + stderr2)

    def reconfigure(self, container_name: str, ip: str,
                    collections: Mapping[str, tuple[str, ...]]) -> bool:
        """Live update via the container's admin endpoint — no restart."""
        body = {"collections": {c: list(ids) for c, ids in collections.items()}}
        url = f"http://{ip}:{_ADMIN_PORT}/forwarder_config"
        try:
            r = requests.post(url, json=body, timeout=_RECONFIGURE_TIMEOUT_S)
            r.raise_for_status()
            logger.info("[node_add] reconfigured %s collections=%d",
                        container_name, len(collections))
            return True
        except requests.RequestException as exc:
            logger.error("[node_add] reconfigure %s failed: %s", container_name, exc)
            return False

    def initiate_drain(self, lan: int, container_name: str, mac: str,
                       ip: str, owner_lan: str) -> PendingDrain | None:
        """Phase A: POST /drain to the container; return a ``PendingDrain``.

        On HTTP 2xx the supervisor has accepted the drain request and is
        stopping its ``ForwarderWorker``s in a background thread; it will
        emit a ``drain_complete`` control-event frame once workers have
        persisted final resume tokens and ``mongod`` has shut down cleanly.
        On any failure (timeout, non-2xx, connection refused) we still
        return a ``PendingDrain`` with ``drain_signaled=False`` so the
        caller can submit an immediate Phase B cleanup.

        Returns ``None`` only when ``ip`` is empty — i.e. when the caller
        cannot reach the container at all; in practice Tier 1 ``PendingDrain``
        is always constructed with the known container IP.
        """
        veth = self._discover_veth(container_name) or ""
        if not veth:
            logger.warning(
                "[node_remove] tier1 drain: could not discover veth for %s before drain",
                container_name,
            )

        if not ip:
            logger.warning("[node_remove] tier1 drain: no ip for %s — skipping HTTP", container_name)
            return PendingDrain(
                mac=mac, veth=veth, container_name=container_name, lan=lan,
                initiated_ts=time.time(), drain_signaled=False, ip=ip,
                node_type="selective_storage", owner_lan=owner_lan,
            )

        drain_ok = False
        url = f"http://{ip}:{_ADMIN_PORT}/drain"
        try:
            r = requests.post(url, timeout=_DRAIN_TIMEOUT_S)
            if r.status_code in (200, 202):
                drain_ok = True
                logger.info("[node_remove] tier1 drain accepted %s (status=%d)",
                            container_name, r.status_code)
            else:
                logger.warning("[node_remove] tier1 drain %s returned status=%d",
                               container_name, r.status_code)
        except requests.RequestException as exc:
            logger.warning("[node_remove] tier1 drain %s failed: %s", container_name, exc)

        return PendingDrain(
            mac=mac, veth=veth, container_name=container_name, lan=lan,
            initiated_ts=time.time(), drain_signaled=drain_ok, ip=ip,
            node_type="selective_storage", owner_lan=owner_lan,
        )

    def remove_selective_storage_node(
        self, lan: int, name: str, mac: str, ip: str,
        veth: str = "",
        keep_volume: bool = False,
    ) -> RemovalResult:
        """Phase B: OVS teardown + ``docker rm`` for a drained Tier 1 node.

        Tier 1 is not a replica-set member. Its drain path shuts down the
        local standalone ``mongod``, so the container may already be exited;
        the dedicated teardown script uses the Phase-A veth metadata for OVS
        cleanup instead of relying on a live container namespace.
        """
        timings = RemovalTimings()
        t_total = time.perf_counter()

        logger.info("[node_remove] tier1: removing %s (mac=%s ip=%s)", name, mac, ip)

        t0 = time.perf_counter()
        script_args = [
            "--lan",  str(lan),
            "--name", name,
        ]
        if mac:
            script_args += ["--mac", mac]
        if ip:
            script_args += ["--ip", ip]
        if veth:
            script_args += ["--veth", veth]
        if keep_volume:
            script_args.append("--keep-volume")

        ok, _, _, stdout, stderr = self._run_script(
            SCRIPTS_DIR / "remove_selective_network_node.sh",
            script_args,
        )
        timings.network_cleanup_s = time.perf_counter() - t0
        timings.total_s = time.perf_counter() - t_total

        state = NodeOperationState.DONE if ok else NodeOperationState.FAILED
        if ok:
            logger.info("[node_remove] tier1 done: container=%s", name)
        else:
            logger.error(
                "[node_remove] tier1 FAILED: container=%s\nstdout=%s\nstderr=%s",
                name, stdout, stderr,
            )
        return RemovalResult(ok, name, mac, timings, state, stdout, stderr)

    # Private helpers

    def _docker_run_selective(
        self, name: str, lan: int, primary_host: str,
        collections: Mapping[str, tuple[str, ...]], max_ttl_s: int,
    ) -> tuple[bool, str, str]:
        state = self._container_state(name)
        if state == "running":
            logger.info("[node_add] container %s already running — skipping docker run", name)
            return True, "", ""
        if state is not None:
            logger.info("[node_add] removing stale container %s (state=%s)", name, state)
            self._cleanup_container(name)

        vol = f"{name}-data"
        collections_json = json.dumps({c: list(ids) for c, ids in collections.items()})
        cmd = [
            "docker", "run", "-dit",
            "--network", "none",
            "--name", name,
            "-v", f"{vol}:/data/db",
            "-e", f"LAN_ID=lan{lan}",
            # ``OWNER_HOST`` is the RS primary resolved at promotion time from
            # ``node_registry``; the supervisor pins it via ``directConnection=true``.
            "-e", f"OWNER_HOST={primary_host}",
            "-e", f"COLLECTIONS_JSON={collections_json}",
            "-e", f"MAX_TTL_S={int(max_ttl_s)}",
            "-e", f"CONTAINER_NAME={name}",
            "edge_selective_storage",
        ]
        return self._run_cmd(cmd)

    def _discover_veth(self, name: str) -> str | None:
        """Discover the OVS-side veth while the Tier 1 container is still running."""
        ok, stdout, _ = self._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Pid}}", name]
        )
        if not ok or not stdout.strip() or stdout.strip() == "0":
            return None
        pid = stdout.strip()

        peer_result = subprocess.run(
            ["sudo", "nsenter", "-t", pid, "-n", "ip", "link", "show", "eth0"],
            capture_output=True, text=True,
        )
        if peer_result.returncode != 0:
            return None
        match = re.search(r"@if(\d+)", peer_result.stdout)
        if not match:
            return None
        peer_ifindex = match.group(1)

        ok2, stdout2, _ = self._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Pid}}", "ovs"]
        )
        if not ok2 or not stdout2.strip() or stdout2.strip() == "0":
            return None
        ovs_pid = stdout2.strip()

        ovs_result = subprocess.run(
            ["sudo", "nsenter", "-t", ovs_pid, "-n", "ip", "link", "show"],
            capture_output=True, text=True,
        )
        if ovs_result.returncode != 0:
            return None
        match2 = re.search(rf"^{re.escape(peer_ifindex)}:\s+(\S+?)[@:]",
                           ovs_result.stdout, re.MULTILINE)
        if not match2:
            return None
        return match2.group(1)
