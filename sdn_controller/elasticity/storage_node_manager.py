"""
storage_node_manager.py — Timed, idempotent lifecycle for storage (edge_storage_server) nodes.

Covers:
  - Spawning an edge_storage_server container, attaching it to OVS, and joining
        the MongoDB replica set via the sidecar's direct seed-host reconfig path
        (add_storage_node)
  - Graceful removal: rs.remove() via the primary, then OVS/Docker teardown
    (remove_storage_node)
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

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


class StorageNodeAdder(_BaseNodeAdder):
    """Stateless helper — each method is a self-contained, timed, idempotent lifecycle."""

    def add_storage_node(
        self,
        lan: int,
        name: str,
        rs_name: str,
        port: int = 27018,
        ip: str | None = None,
        mac: str | None = None,
        heartbeat_enabled: bool = False,
        rs_seed_host_override: str | None = None,
    ) -> NodeResult:
        """Spawn an edge_storage_server container, attach it to OVS LAN ``lan``,
                and join the replica set via the sidecar's direct seed-host reconfig.

        Steps:
          1. ``docker run --network none edge_storage_server mongod --replSet ...``
          2. ``add_network_node.sh`` (veth + OVS attach; RS join handled by sidecar)
        """
        timings = StepTimings()
        t_total = time.perf_counter()

        if rs_seed_host_override is not None:
            rs_seed_host = rs_seed_host_override
            logger.info("[node_add] RS seed host (override): %s", rs_seed_host)
        else:
            # LAN convention: the storage primary is always 10.0.{lan-1}.4.
            primary_ip = f"10.0.{lan - 1}.4"
            rs_seed_host = f"{primary_ip}:{port}"
            logger.info("[node_add] RS seed host for lan%d: %s", lan, rs_seed_host)

        logger.info("[node_add] step=docker_run container=%s rs=%s", name, rs_name)
        t0 = time.perf_counter()
        ok, stdout, stderr = self._docker_run_storage(
            name,
            rs_name,
            port,
            lan,
            rs_seed_host=rs_seed_host,
            ip=ip,
            mac=mac,
            heartbeat_enabled=heartbeat_enabled,
        )
        timings.docker_run_s = time.perf_counter() - t0

        if not ok:
            timings.total_s = time.perf_counter() - t_total
            return NodeResult(False, name, None, None, timings, NodeOperationState.FAILED, stdout, stderr)

        logger.info("[node_add] step=network_attach container=%s lan=%d", name, lan)
        t0 = time.perf_counter()
        script_args = ["--lan", str(lan), "--name", name]
        if ip:
            script_args += ["--ip", ip]
        if mac:
            script_args += ["--mac", mac]
        ok, ip, mac, stdout2, stderr2 = self._run_script(
            SCRIPTS_DIR / "add_network_node.sh",
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

        logger.debug("[node_add] attach+join complete  container=%s ip=%s mac=%s", name, ip, mac)
        return NodeResult(True, name, ip, mac, timings, NodeOperationState.DONE,
                          stdout + stdout2, stderr + stderr2)

    def remove_storage_node(
        self,
        lan: int,
        name: str,
        mac: str,
        ip: str,
        rs_name: str,
        primary_container: str,
        port: int = 27018,
        keep_volume: bool = False,
        *,
        best_effort_rs_remove: bool = False,
    ) -> RemovalResult:
        """Remove a storage node: rs.remove() via primary then script teardown.

        The script is called with ``--skip-rs`` because rs.remove() is
        performed here in Python (with polling) before the script runs.
        The script handles: DNAT flow flush → docker stop → OVS port/veth
        deletion → docker rm → optional volume removal.

        When *best_effort_rs_remove* is ``True`` (reserve cleanup only):

        * Primary discovery failure is **not** fatal — the code assumes the
          member never joined and continues to teardown.
        * Non-``ok:1`` ``rs.remove()`` and RS-wait timeouts are logged at
          warning level but teardown still proceeds.
        * The final success/failure is driven by the teardown script outcome,
          not by the RS-eviction step.

        Ordinary storage scale-down must **never** use this mode.
        """
        timings = RemovalTimings()
        t_total = time.perf_counter()
        combined_stdout = ""
        combined_stderr = ""

        logger.info("[node_remove] storage: removing %s (mac=%s ip=%s best_effort=%s)",
                    name, mac, ip, best_effort_rs_remove)

        member_host = f"{ip}:{port}"
        primary_host = self._find_rs_primary(primary_container, port)
        if primary_host is None:
            if not best_effort_rs_remove:
                timings.total_s = time.perf_counter() - t_total
                return RemovalResult(False, name, mac, timings, NodeOperationState.FAILED,
                                     "", "Could not determine RS primary")
            logger.warning(
                "[node_remove] primary_not_found_assume_not_joined name=%s member=%s",
                name, member_host,
            )
        else:
            ok_remove = self._rs_remove_member(primary_container, primary_host, member_host)
            if ok_remove:
                logger.info("[node_remove] rs_remove_succeeded name=%s member=%s", name, member_host)
                removed = self._wait_rs_member_removed(primary_container, primary_host, member_host)
                if not removed:
                    logger.warning(
                        "[node_remove] rs_remove_wait_timeout name=%s member=%s — "
                        "member may still appear in rs.status()",
                        name, member_host,
                    )
            else:
                logger.warning(
                    "[node_remove] rs_remove_non_ok name=%s member=%s — "
                    "proceeding with teardown anyway",
                    name, member_host,
                )

        t0 = time.perf_counter()
        script_args = [
            "--lan",     str(lan),
            "--name",    name,
            "--rs-name", rs_name,
            "--primary", primary_container,
            "--port",    str(port),
            "--skip-rs",
        ]
        if keep_volume:
            script_args.append("--keep-volume")

        ok, _, _, stdout2, stderr2 = self._run_script(
            SCRIPTS_DIR / "remove_network_storage_node.sh",
            script_args,
        )
        timings.network_cleanup_s = time.perf_counter() - t0
        timings.total_s = time.perf_counter() - t_total
        combined_stdout += stdout2
        combined_stderr += stderr2

        state = NodeOperationState.DONE if ok else NodeOperationState.FAILED
        if ok:
            logger.info("[node_remove] storage done: container=%s", name)
        else:
            logger.error("[node_remove] storage FAILED: container=%s\nstdout=%s\nstderr=%s",
                         name, combined_stdout, combined_stderr)
        return RemovalResult(ok, name, mac, timings, state, combined_stdout, combined_stderr)

    # Private helpers

    def _docker_run_storage(
        self,
        name: str,
        rs_name: str,
        port: int,
        lan: int,
        rs_seed_host: str | None = None,
        ip: str | None = None,
        mac: str | None = None,
        heartbeat_enabled: bool = False,
    ) -> tuple[bool, str, str]:
        state = self._container_state(name)
        if state == "running":
            logger.info("[node_add] container %s already running — skipping docker run", name)
            return True, "", ""
        if state is not None:
            logger.info("[node_add] removing stale container %s (state=%s)", name, state)
            self._cleanup_container(name)
        vol = f"{name}-data"
        storage_cpus = os.environ.get("STORAGE_CPUS", "0.15")
        storage_mem = os.environ.get("STORAGE_MEMORY", "512m")
        cmd = [
            "docker", "run", "-dit",
            "--cpus", storage_cpus,
            "--memory", storage_mem,
            "--network", "none",
            "--name", name,
            "-v", f"{vol}:/data/db",
            "-e", f"LAN_ID=lan{lan}",
            "-e", f"MONGO_REPLSET={rs_name}",
            "-e", f"MONGO_PORT={port}",
            "-e", f"CONTAINER_NAME={name}",
            "-e", "IFACE=eth0",
            # The sidecar parses this strictly as true/false; standby reserves need
            # heartbeats so the controller can tell a ready reserve from a missing one.
            "-e", f"HEARTBEAT_ENABLED={'true' if heartbeat_enabled else 'false'}",
        ]
        if ip:
            cmd += ["-e", f"OWN_IP={ip}"]
        if mac:
            cmd += ["-e", f"OWN_MAC={mac}"]
        # The sidecar self-joins the RS when a seed host is given.
        if rs_seed_host:
            cmd += ["-e", "RS_ADD_SELF=true", "-e", f"RS_SEED_HOST={rs_seed_host}"]
        cmd.append("edge_storage_server")
        return self._run_cmd(cmd)

    def _find_rs_primary(self, primary_container: str, port: int) -> str | None:
        """Return the host:port string of the current RS primary, or None."""
        result = subprocess.run(
            [
                "docker", "exec", "-i", primary_container,
                "mongosh", "--quiet", f"--port={port}", "--eval",
                "try { print(db.adminCommand({isMaster:1}).primary); } "
                "catch(e) { print('ERROR:' + e); }",
            ],
            capture_output=True, text=True,
        )
        out = (result.stdout or "").strip().splitlines()
        primary = out[-1].strip() if out else ""
        if not primary or primary.startswith("ERROR:"):
            logger.warning("[node_remove] could not determine RS primary via %s: %s", primary_container, primary)
            return None
        return primary

    def _rs_remove_member(self, primary_container: str, primary_host: str, member_host: str) -> bool:
        """Run rs.remove(member_host) via the primary. Returns True on ok:1."""
        primary_ip   = primary_host.split(":")[0]
        primary_port = primary_host.split(":")[-1]
        result = subprocess.run(
            [
                "docker", "exec", "-i", primary_container,
                "mongosh", "--quiet",
                "--host", primary_ip, f"--port={primary_port}",
                "--eval", f"JSON.stringify(rs.remove('{member_host}'))",
            ],
            capture_output=True, text=True,
        )
        ok = '"ok":1' in result.stdout or '"ok": 1' in result.stdout
        if ok:
            logger.info("[node_remove] rs.remove('%s') succeeded", member_host)
        else:
            logger.warning("[node_remove] rs.remove('%s') did not return ok:1: %s",
                           member_host, result.stdout)
        return ok

    def _wait_rs_member_removed(
        self,
        primary_container: str,
        primary_host: str,
        member_host: str,
        max_retries: int = 10,
        retry_delay: float = 3.0,
    ) -> bool:
        """Poll rs.status() until member_host is gone. Returns True on success."""
        primary_ip   = primary_host.split(":")[0]
        primary_port = primary_host.split(":")[-1]
        for attempt in range(1, max_retries + 1):
            result = subprocess.run(
                [
                    "docker", "exec", "-i", primary_container,
                    "mongosh", "--quiet",
                    "--host", primary_ip, f"--port={primary_port}",
                    "--eval",
                    f"try {{ var s = rs.status(); "
                    f"print(s.members.find(m => m.name === '{member_host}') ? 'FOUND' : 'REMOVED'); }}"
                    f" catch(e) {{ print('ERROR:' + e); }}",
                ],
                capture_output=True, text=True,
            )
            out = (result.stdout or "").strip().splitlines()
            status = out[-1].strip() if out else ""
            if status == "REMOVED":
                logger.info("[node_remove] member '%s' removed from RS after %d attempt(s)",
                            member_host, attempt)
                return True
            logger.debug("[node_remove] rs.status() check %d/%d: %s", attempt, max_retries, status)
            if attempt < max_retries:
                time.sleep(retry_delay)
        logger.warning("[node_remove] member '%s' still in RS after %d retries", member_host, max_retries)
        return False
