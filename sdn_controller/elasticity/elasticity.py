"""
elasticity.py — Thread 3: Elasticity & Placement Manager.

Owns a threading.Queue fed by Thread 2's on_update callback. Dispatches
typed alerts to the appropriate handler. All infrastructure mutations flow
through NodeAdder — this module is the sole orchestrator of container
lifecycle changes.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from ..readiness_gate import PendingBackend
from ..scaling_config import _EDGE_READY_PORT

from .node_common import IpAllocator, NodeInfo, RemovalResult, log_ready_timing
from .compute_node_manager import ComputeNodeAdder, PendingDrain
from .storage_node_manager import StorageNodeAdder

logger = logging.getLogger("os_ken.elasticity")


class ElasticityController(Protocol):
    """Explicit contract between the injected controller and ElasticityManager.

    In simple terms, this lists the controller-side methods that the composed controller 
    exposes to Thread 3 for backend admission, removal, and drain-cancel handling.

    The object passed here is the full composed controller from main_n*.py,
    not a bare TopologyMixin.
    """

    def register_new_server_backend(self, mac: str, ip: str) -> None:
        ...

    def register_backend_ip(self, mac: str, ip: str) -> None:
        ...

    def unregister_server_backend(self, mac: str) -> None:
        ...

    def add_server_mac(self, mac: str) -> None:
        ...

    def unregister_storage_backend(self, mac: str, domain: str = "n1") -> None:
        ...

# Per-network sequence counter used to generate unique dynamic container names.
_COUNTER: dict[str, int] = {}


# Alert types — produced by Thread 2, consumed by Thread 3

@dataclass(frozen=True)
class ComputeAlert:
    """Raised by Thread 2 when avg T_proc > τ_proc for a network domain."""
    lan: int          # target LAN number (1 or 2)
    network_id: str   # e.g. "lan1"


@dataclass(frozen=True)
class DataAlert:
    """Raised by Thread 2 when avg T_dados > τ_dados for a network domain."""
    lan:               int
    network_id:        str
    rs_name:           str   # e.g. "rs_net1"
    primary_container: str   # container to run rs.add() against
    port:              int = 27018


@dataclass(frozen=True)
class ScaleDownComputeAlert:
    """Scale-down: remove the most recently added dynamic compute node."""
    lan:            int
    network_id:     str
    container_name: str
    mac:            str
    ip:             str
    reason:         str = "scale_down"   # "scale_down" | "absent" (absent-cleanup holds its slot)


@dataclass(frozen=True)
class ScaleDownDataAlert:
    """Scale-down: remove the most recently added dynamic storage node.

    ``reason`` is "scale_down" (ordinary) or "absent" (absent-cleanup holds
    its slot), mirroring :class:`ScaleDownComputeAlert`.
    """
    lan:               int
    network_id:        str
    container_name:    str
    mac:               str
    ip:                str
    rs_name:           str
    primary_container: str
    port:              int = 27018
    reason:            str = "scale_down"   # "scale_down" | "absent" (absent-cleanup holds its slot)


@dataclass(frozen=True)
class CleanupComputeAlert:
    """Phase B trigger: submitted when the drain_complete control event
    arrives.  Causes Thread 3 to run OVS teardown."""
    mac: str


@dataclass(frozen=True)
class AbandonComputeBackendAlert:
    """RQ3: teardown a spawned-but-never-ready compute backend (never admitted).

    Runs in Thread 3 (so the readiness-gate worker thread is never blocked and
    ``_busy`` is set during the teardown). See readiness_gate.py.
    """
    mac: str
    ip: str
    container_name: str
    lan: int


@dataclass(frozen=True)
class CancelComputeDrainAlert:
    """Cancel one pending compute drain when compute scale-up is triggered."""
    mac: str | None = None


@dataclass(frozen=True)
class PrepareStandbyStorageAlert:
    """Prepare one storage reserve through the existing storage add path.

    The node is created from the reserve add path with
    ``standby_reserved=True`` and held
    outside VIP until activated by a load or recovery trigger.
    """
    lan: int
    network_id: str
    rs_name: str
    primary_container: str
    port: int = 27018


@dataclass(frozen=True)
class CleanupReserveAlert:
    """Immediate-terminate cleanup for a lost or failed reserved storage node.

    No drain — the reserve was never serving edge traffic.  Thread 3 stops
    and removes the container.  When *rs_name*, *primary_container*, and *ip*
    are all non-empty the handler performs a full ``rs.remove()``-then-teardown
    through the existing storage removal path; otherwise it does container-only
    cleanup (the reserve never joined the replica set).
    """
    lan: int
    mac: str
    container_name: str
    ip: str = ""
    rs_name: str = ""
    primary_container: str = ""
    port: int = 27018


@dataclass
class ReservePrepareFailed:
    """Published by Thread 3 when a reserve spawn fails.

    Carries best-effort identity for logging correlation.
    """
    lan: int
    name: str = ""
    ip: str = ""
    mac: str = ""


# Alert dispatch priorities — lower number wins.  Cleanup alerts sit beside
# their paired scale-down alerts so a pending Phase B beats a fresh scale-down.
_PRIORITY_DATA_ALERT                 = 1
_PRIORITY_PREPARE_STANDBY_STORAGE    = 4
_PRIORITY_COMPUTE_ALERT              = 4
_PRIORITY_CLEANUP_RESERVE            = 5
_PRIORITY_CLEANUP_COMPUTE            = 5
_PRIORITY_ABANDON_COMPUTE            = 5
_PRIORITY_CANCEL_COMPUTE_DRAIN       = 7
_PRIORITY_SCALEDOWN_DATA             = 8
_PRIORITY_SCALEDOWN_COMPUTE          = 10

# Tie-breaker: monotonically increasing sequence so alerts with the same
# priority are processed in FIFO order.
_alert_seq = itertools.count()

# Type → priority lookup (covers all alert types)
_ALERT_PRIORITY: dict[type, int] = {
    DataAlert:                      _PRIORITY_DATA_ALERT,
    PrepareStandbyStorageAlert:     _PRIORITY_PREPARE_STANDBY_STORAGE,
    CleanupReserveAlert:            _PRIORITY_CLEANUP_RESERVE,
    ComputeAlert:                   _PRIORITY_COMPUTE_ALERT,
    CleanupComputeAlert:            _PRIORITY_CLEANUP_COMPUTE,
    AbandonComputeBackendAlert:     _PRIORITY_ABANDON_COMPUTE,
    CancelComputeDrainAlert:        _PRIORITY_CANCEL_COMPUTE_DRAIN,
    ScaleDownDataAlert:             _PRIORITY_SCALEDOWN_DATA,
    ScaleDownComputeAlert:          _PRIORITY_SCALEDOWN_COMPUTE,
}


# ElasticityManager

class ElasticityManager:
    """Thread 3: the sole actor for infrastructure mutations.

    Thread 2 calls ``submit()``; this manager pops alerts off its queue
    and runs the appropriate NodeAdder lifecycle in a dedicated daemon thread.
    The calling thread is never blocked — the queue decouples detection from
    execution.
    """

    def __init__(
        self,
        topology_mixin: ElasticityController,
    ) -> None:
        """Initialise the manager.

        Parameters
        ----------
        topology_mixin
            The owning controller; used for VIP pool mutations.
        """
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._compute_adder = ComputeNodeAdder()
        self._storage_adder = StorageNodeAdder()
        self._topo    = topology_mixin
        # RQ3 readiness gate, injected by T7. None → pre-RQ3 behaviour:
        # register_new_server_backend immediately in _handle_compute.
        self.readiness_gate = None
        self._lock    = threading.Lock()
        self._ip_allocs: dict[int, IpAllocator] = {}

        # Scale-down state — written by Thread 3, read by Thread 2
        self._busy: bool = False
        self._pending_drains: dict[str, PendingDrain] = {}

        # Add/removal completion notifications for Thread 2
        self._addition_complete_lock  = threading.Lock()
        self._addition_complete_infos: list[NodeInfo] = []
        self._removal_complete_lock   = threading.Lock()
        self._removal_complete_entries: list[tuple[str, str]] = []

        # Reserve-specific outcome queues (Thread 3 → Thread 2).
        # Per-LAN ownership — one controller must never drain the other LAN's failures.
        self._reserve_prepare_failed_lock = threading.Lock()
        self._reserve_prepare_failures: dict[int, list[ReservePrepareFailed]] = {}

        self._thread  = threading.Thread(
            target=self._loop, name="elasticity-mgr", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("[elasticity] manager started")

    # Late wiring: the coordinator's constructor needs this manager (for
    # ``submit``), so attachment is two-phase from ``main_n*.py``.

    def submit(self, alert) -> None:
        """Unified thread-safe enqueue for any alert type."""
        priority = _ALERT_PRIORITY.get(type(alert))
        if priority is None:
            logger.warning("[elasticity] unknown alert type %s \u2014 using lowest priority", type(alert).__name__)
            priority = _PRIORITY_SCALEDOWN_COMPUTE
        logger.info("[elasticity] alert submitted (priority=%d): %s", priority, alert)
        self._queue.put((priority, next(_alert_seq), alert))

    def submit_cleanup(self, mac: str) -> None:
        """Phase B trigger: enqueue a :class:`CleanupComputeAlert` for *mac*.

        Submitted when the ``drain_complete`` control event arrives.  A warn
        is logged for an unknown MAC so a stray event is visible in the log
        rather than silently wedging the queue.
        """
        if self._get_pending_drain(mac) is None:
            logger.warning(
                "[elasticity] submit_cleanup for unknown mac=%s", mac,
            )
        self.submit(CleanupComputeAlert(mac=mac))

    def submit_cleanup_reserve(self, alert: CleanupReserveAlert) -> None:
        """Enqueue a reserve-specific immediate-terminate cleanup."""
        self.submit(alert)

    def submit_abandon(self, mac: str, ip: str, container_name: str, lan: int) -> None:
        """RQ3: enqueue a full teardown for a spawned-but-never-ready backend."""
        self.submit(AbandonComputeBackendAlert(
            mac=mac, ip=ip, container_name=container_name, lan=lan,
        ))

    def drain_reserve_prepare_failures(self, lan: int) -> list[ReservePrepareFailed]:
        """Atomically drain and return queued reserve-prepare failures for *lan*.

        Per-LAN ownership — each controller drains only its own LAN's failures.
        """
        with self._reserve_prepare_failed_lock:
            result = self._reserve_prepare_failures.pop(lan, [])
        return result

    def submit_cancel_compute_drain(self, mac: str | None = None) -> None:
        """Submit a lower-priority cancel request for a pending compute drain."""
        self.submit(CancelComputeDrainAlert(mac=mac))

    def has_active_operation(self) -> bool:
        """Return True while Thread 3 is actively executing a handler."""
        return self._busy

    def is_busy(self) -> bool:
        """Thread-safe check: is an operation currently blocking general evaluation?

        Returns True while Thread 3 is executing any add/remove handler, or
        while a Phase A drain is pending. Thread 2 uses this stricter gate for
        scale-down and other general checks. ``_busy`` is a plain bool written
        only by Thread 3 and read by Thread 2; Python's GIL guarantees atomic
        reads/writes of bool.
        """
        if self._busy:
            return True
        return bool(self._pending_drain_snapshot())

    def blocks_compute_scale_up(self) -> bool:
        """Return True when compute scale-up should be skipped."""
        return self._busy

    def blocks_storage_scale_up(self) -> bool:
        """Return True when storage scale-up should be skipped."""
        if self._busy:
            return True
        return any(
            pending.node_type == "storage"
            for pending in self._pending_drain_snapshot()
        )

    def has_pending_drain(self, mac: str) -> bool:
        """Check if a MAC has an in-progress drain (Phase A done, Phase B pending)."""
        return self._get_pending_drain(mac) is not None

    def has_pending_compute_drain(self) -> bool:
        """Return True when any compute drain is pending Phase B cleanup/cancel."""
        return any(
            pending.node_type == "compute"
            for pending in self._pending_drain_snapshot()
        )

    def pending_compute_drain_count(self, exclude_reason: str | None = None) -> int:
        """Count pending compute drains without changing lifecycle registry state.

        ``exclude_reason`` (e.g. "absent") skips drains tagged with that reason so
        an absent-cleanup keeps its budget slot until Phase B completes —
        preventing the absent→respawn overlap (transient budget+1 overshoot).
        """
        return sum(
            1
            for pending in self._pending_drain_snapshot()
            if pending.node_type == "compute"
            and (exclude_reason is None or pending.reason != exclude_reason)
        )

    def consume_addition_completions(self) -> list[NodeInfo]:
        """Called by Thread 2 to collect NodeInfo records for newly added nodes."""
        with self._addition_complete_lock:
            result, self._addition_complete_infos = list(self._addition_complete_infos), []
        return result

    def consume_removal_completions(self) -> list[tuple[str, str]]:
        """Called by Thread 2 to collect (mac, container_name) pairs of fully removed nodes.

        Each pair carries the container name so the registry can verify that
        the MAC has not been recycled by a newer node before removing it.
        """
        with self._removal_complete_lock:
            result = list(self._removal_complete_entries)
            self._removal_complete_entries.clear()
        return result

    # Private — runs exclusively in the elasticity daemon thread

    def _loop(self) -> None:
        while True:
            try:
                _priority, _seq, alert = self._queue.get()
                self._busy = True
                try:
                    if isinstance(alert, ComputeAlert):
                        self._handle_compute(alert)
                    elif isinstance(alert, DataAlert):
                        self._handle_data(alert)
                    elif isinstance(alert, PrepareStandbyStorageAlert):
                        self._handle_prepare_standby_storage(alert)
                    elif isinstance(alert, CleanupReserveAlert):
                        self._handle_cleanup_reserve(alert)
                    elif isinstance(alert, ScaleDownComputeAlert):
                        self._handle_scale_down_compute(alert)
                    elif isinstance(alert, ScaleDownDataAlert):
                        self._handle_scale_down_data(alert)
                    elif isinstance(alert, CleanupComputeAlert):
                        self._handle_cleanup_compute(alert)
                    elif isinstance(alert, AbandonComputeBackendAlert):
                        self._handle_abandon_compute(alert)
                    elif isinstance(alert, CancelComputeDrainAlert):
                        self._handle_cancel_compute_drain(alert)

                    else:
                        logger.warning("[elasticity] unknown alert type: %s", type(alert))
                finally:
                    # CleanupComputeAlert is Phase B — keep _busy False only
                    # after it finishes (pending_drains already cleared inside handler).
                    self._busy = False
            except Exception:  
                logger.exception("[elasticity] unhandled error in loop")

    def _get_allocator(self, lan: int) -> IpAllocator:
        """Lazy per-LAN allocator — created on first use."""
        if lan not in self._ip_allocs:
            self._ip_allocs[lan] = IpAllocator(lan)
        return self._ip_allocs[lan]

    def _handle_compute(self, alert: ComputeAlert) -> None:
        name = self._next_name("edge_server", alert.network_id)
        ip, mac = self._get_allocator(alert.lan).allocate()
        spawn_started_monotonic_s = time.monotonic()
        spawn_start_wall_s = time.time()
        logger.info("[elasticity] compute: spawning %s on LAN %d (ip=%s mac=%s)", name, alert.lan, ip, mac)

        result = self._compute_adder.add_edge_server(lan=alert.lan, name=name, ip=ip, mac=mac)
        self._compute_adder.log_timings(result)

        logger.debug("[elasticity] compute result: success=%s ip=%s mac=%s", result.success, result.ip, result.mac)

        if result.success and result.ip:
            effective_mac = result.mac or mac
            effective_ip  = result.ip or ip
            if effective_mac:
                if self.readiness_gate is None:
                    self._admit_compute_backend(
                        effective_mac, effective_ip, name,
                        alert.lan, alert.network_id,
                        spawn_started_monotonic_s, "vip_backend_registered",
                    )
                else:
                    # RQ3 arms: hand off to the readiness gate; admission is gated on
                    # /ready, so nothing is registered yet.
                    self.readiness_gate.enqueue(PendingBackend(
                        mac=effective_mac, ip=effective_ip, name=name,
                        lan=alert.lan, network_id=alert.network_id,
                        ready_port=_EDGE_READY_PORT,
                        spawn_started_wall_s=spawn_start_wall_s,
                        spawn_complete_wall_s=time.time(),
                        spawn_started_mono_s=spawn_started_monotonic_s,
                    ))
            else:
                logger.warning(
                    "[elasticity] compute: %s online at %s but MAC not available in script output",
                    name, result.ip,
                )
        else:
            self._get_allocator(alert.lan).release(ip)
            logger.error("[elasticity] compute: failed to spawn %s", name)

    def _admit_compute_backend(
        self, effective_mac, effective_ip, name, lan, network_id,
        spawn_started_monotonic_s, source: str,
    ) -> None:
        """Register a compute backend into the VIP_SERVER pool.

        Shared by the pre-RQ3 ``off`` path (called directly, source
        ``vip_backend_registered``) and the RQ3 readiness gate (called from the
        gate worker thread on verified readiness, source ``readiness_gate``).
        """
        self._topo.register_new_server_backend(effective_mac, effective_ip)
        # Never raise AFTER registration: the backend is already admitted, so a
        # later failure must not cause a re-admit or an abandon.
        try:
            log_ready_timing(
                name, "compute", source,
                time.monotonic() - spawn_started_monotonic_s,
            )
        except Exception:
            logger.exception("[elasticity] log_ready_timing failed for %s", name)
        logger.info(
            "[elasticity] compute: %s online  ip=%s  mac=%s",
            name, effective_ip, effective_mac,
        )
        # Notify Thread 2 so it can track this MAC for scale-down
        try:
            info = NodeInfo(
                mac=effective_mac, lan=lan, network_id=network_id,
                name=name, ip=effective_ip, node_type="compute",
                spawn_started_monotonic_s=spawn_started_monotonic_s,
                ready_logged=True,
            )
            with self._addition_complete_lock:
                self._addition_complete_infos.append(info)
        except Exception:
            logger.exception("[elasticity] NodeInfo bookkeeping failed for %s", name)

    def _abandon_compute_backend(self, effective_mac, effective_ip, name, lan) -> None:
        """Tear down a spawned-but-never-ready compute backend (RQ3).

        Reuses the existing full compute teardown (Phase A drain discovery +
        Phase B cleanup) so the OVS veth/port and host-attachment entries are
        removed too — not just the container. Best-effort and exception-safe.
        """
        try:
            pending = self._compute_adder.initiate_drain(lan, name, effective_mac)
            if pending is not None:
                pending.ip = effective_ip
                self._set_pending_drain(effective_mac, pending)
                self.submit_cleanup(effective_mac)   # Phase B: full teardown + IP release
            else:
                self._compute_adder.remove_failed_container(name)
                self._get_allocator(lan).release(effective_ip)
        except Exception:
            logger.exception(
                "[elasticity] abandon teardown failed for %s — attempting cleanup",
                name,
            )
            try:
                self._compute_adder.remove_failed_container(name)
            except Exception:
                pass
            try:
                self._get_allocator(lan).release(effective_ip)
            except Exception:
                pass
            try:
                self._pop_pending_drain(effective_mac)
            except Exception:
                pass
        # Recycled-MAC hygiene: the bare-remove paths skip the VIP helpers, and
        # even a never-ready container emits one bootstrap heartbeat.
        self._topo.drop_backend_caches(effective_mac)
        logger.error(
            "[elasticity] compute: %s abandoned (never ready) — teardown submitted",
            name,
        )

    def _handle_data(self, alert: DataAlert) -> None:
        name = self._next_name("edge_storage", alert.network_id)
        ip, mac = self._get_allocator(alert.lan).allocate()
        spawn_started_monotonic_s = time.monotonic()
        logger.info("[elasticity] data: spawning %s on LAN %d (ip=%s mac=%s)", name, alert.lan, ip, mac)


        result = self._storage_adder.add_storage_node(
            lan=alert.lan,
            name=name,
            rs_name=alert.rs_name,
            port=alert.port,
            ip=ip, mac=mac,
            # Everyday dynamic storage nodes heartbeat too (liveness only).
            heartbeat_enabled=True,
        )
        self._storage_adder.log_timings(result)

        logger.debug("[elasticity] data result: success=%s ip=%s mac=%s", result.success, result.ip, result.mac)

        if result.success and result.ip:
            effective_mac = result.mac or mac
            effective_ip  = result.ip or ip
            if effective_mac:
                # Pre-seed IP↔MAC table so Thread 1 has the mapping ready
                # when the node eventually enters the VIP pool.
                self._topo.register_backend_ip(effective_mac, effective_ip)

                # Do NOT call add_storage_mac() here — the node is not SECONDARY yet.
                # VIP registration is deferred until rs_secondary_ready arrives (Phase F).
                logger.info(
                    "[elasticity] data: %s online  ip=%s  mac=%s  (VIP deferred until SECONDARY)",
                    name, effective_ip, effective_mac,
                )
                # Notify Thread 2 so it can track this MAC for scale-down
                info = NodeInfo(
                    mac=effective_mac, lan=alert.lan, network_id=alert.network_id,
                    name=name, ip=effective_ip, node_type="storage",
                    rs_name=alert.rs_name,
                    primary_container=alert.primary_container,
                    port=alert.port,
                    spawn_started_monotonic_s=spawn_started_monotonic_s,
                )
                with self._addition_complete_lock:
                    self._addition_complete_infos.append(info)
            else:
                logger.warning(
                    "[elasticity] data: %s online at %s but MAC not available in script output",
                    name, result.ip,
                )
        else:
            self._get_allocator(alert.lan).release(ip)
            logger.error("[elasticity] data: failed to spawn %s", name)

    # Reserve cleanup helper

    def _cleanup_reserve_storage_best_effort(
        self,
        *,
        lan: int,
        name: str,
        mac: str,
        ip: str,
        rs_name: str,
        primary_container: str,
        port: int,
    ) -> RemovalResult:
        """Best-effort reserve storage teardown — call, time, return.

        Calls ``remove_storage_node(..., best_effort_rs_remove=True)`` so
        RS eviction is attempted but never blocks teardown.  The caller
        decides whether to release the allocator IP based on
        ``result.success``.
        """
        result = self._storage_adder.remove_storage_node(
            lan=lan,
            name=name,
            mac=mac,
            ip=ip,
            rs_name=rs_name,
            primary_container=primary_container,
            port=port,
            best_effort_rs_remove=True,
        )
        self._storage_adder.log_removal_timings(result)
        return result

    def _handle_prepare_standby_storage(self, alert: PrepareStandbyStorageAlert) -> None:
        """Prepare one same-LAN storage reserve through the existing storage add path.

        Creates a real heartbeating SECONDARY with ``standby_reserved=True``.
        The node is held outside VIP until activated.
        """
        name = self._next_name("edge_storage", alert.network_id)
        ip, mac = self._get_allocator(alert.lan).allocate()
        spawn_started_monotonic_s = time.monotonic()
        logger.info("[elasticity] standby_storage: spawning reserve %s on LAN %d (ip=%s mac=%s)",
                    name, alert.lan, ip, mac)

        result = self._storage_adder.add_storage_node(
            lan=alert.lan,
            name=name,
            rs_name=alert.rs_name,
            port=alert.port,
            ip=ip, mac=mac,
            heartbeat_enabled=True,
        )
        self._storage_adder.log_timings(result)

        if result.success and result.ip and result.mac:
            effective_mac = result.mac or mac
            effective_ip  = result.ip or ip
            self._topo.register_backend_ip(effective_mac, effective_ip)
            logger.info(
                "[elasticity] standby_storage: %s online  ip=%s  mac=%s  (VIP deferred, standby_reserved)",
                name, effective_ip, effective_mac,
            )
            # Notify Thread 2 with standby_reserved=True — the node stays
            # out of VIP and out of ordinary dynamic storage accounting.
            info = NodeInfo(
                mac=effective_mac, lan=alert.lan, network_id=alert.network_id,
                name=name, ip=effective_ip, node_type="storage",
                rs_name=alert.rs_name,
                primary_container=alert.primary_container,
                port=alert.port,
                spawn_started_monotonic_s=spawn_started_monotonic_s,
                standby_reserved=True,
            )
            with self._addition_complete_lock:
                self._addition_complete_infos.append(info)
        elif result.success and result.ip:
            # Up but no usable MAC, and the sidecar may already have self-joined —
            # evict best-effort in case the join reached the primary.
            logger.warning(
                "[elasticity] standby_storage: %s online at %s but MAC not available — "
                "best-effort RS removal",
                name, result.ip,
            )
            cleanup_result = self._cleanup_reserve_storage_best_effort(
                lan=alert.lan,
                name=name,
                mac=mac or "",
                ip=result.ip,
                rs_name=alert.rs_name,
                primary_container=alert.primary_container,
                port=alert.port,
            )
            if cleanup_result.success:
                if ip:
                    self._get_allocator(alert.lan).release(ip)
            else:
                logger.error("[elasticity] standby_storage cleanup FAILED for %s", name)
            with self._reserve_prepare_failed_lock:
                self._reserve_prepare_failures.setdefault(alert.lan, []).append(
                    ReservePrepareFailed(
                        lan=alert.lan,
                        name=name,
                        ip=ip or "",
                        mac=mac or "",
                    )
                )
            logger.error("[elasticity] standby_storage: failed to prepare %s (missing MAC)", name)
        else:
            # Hard failure — the container never reached the RS-add stage, so a plain
            # container teardown is enough.
            self._storage_adder._cleanup_container(name)
            if ip:
                self._get_allocator(alert.lan).release(ip)
            with self._reserve_prepare_failed_lock:
                self._reserve_prepare_failures.setdefault(alert.lan, []).append(
                    ReservePrepareFailed(
                        lan=alert.lan,
                        name=name,
                        ip=ip or "",
                        mac=mac or "",
                    )
                )
            logger.error("[elasticity] standby_storage: failed to spawn %s (success=%s ip=%s mac=%s)",
                        name, result.success, result.ip, result.mac)

    def _handle_cleanup_reserve(self, alert: CleanupReserveAlert) -> None:
        """Immediate-terminate cleanup for a lost reserved storage node.

        Uses best-effort RS eviction when enough member identity exists
        (``ip``, ``rs_name``, ``primary_container`` are all present):
        ``rs.remove()`` is attempted but teardown always proceeds regardless
        of RS outcome.  The allocator IP is released **only** when teardown
        succeeds.

        Falls back to container-only teardown when metadata is incomplete
        (the reserve never reached the RS-add stage).

        No drain or VIP unwiring — the reserve was never serving edge traffic.
        """
        logger.info("[elasticity] cleanup_reserve: terminating %s (mac=%s lan=%d rs=%s primary=%s)",
                    alert.container_name, alert.mac, alert.lan,
                    alert.rs_name, alert.primary_container)

        if alert.ip and alert.rs_name and alert.primary_container:
            # Best-effort eviction: rs.remove(), but always continue to teardown when
            # the primary is unreachable or the member was never present.
            cleanup_result = self._cleanup_reserve_storage_best_effort(
                lan=alert.lan,
                name=alert.container_name,
                mac=alert.mac,
                ip=alert.ip,
                rs_name=alert.rs_name,
                primary_container=alert.primary_container,
                port=alert.port,
            )
            if cleanup_result.success:
                if alert.ip:
                    try:
                        self._get_allocator(alert.lan).release(alert.ip)
                    except Exception:
                        pass
                logger.info("[elasticity] cleanup_reserve: done %s", alert.container_name)
            else:
                logger.error("[elasticity] cleanup_reserve FAILED: %s", alert.container_name)
        else:
            self._storage_adder._cleanup_container(alert.container_name)
            if alert.ip:
                try:
                    self._get_allocator(alert.lan).release(alert.ip)
                except Exception:
                    pass
            logger.info("[elasticity] cleanup_reserve: done %s", alert.container_name)

        # Recycled-MAC hygiene: the reserve never unregistered through the VIP
        # helpers, so drop its cached telemetry stats + discovery stamp here.
        self._topo.drop_backend_caches(alert.mac)

    def _handle_scale_down_compute(self, alert: ScaleDownComputeAlert) -> None:
        """Phase A: isolate from VIP, discover veth, send drain signal, return.

        Thread 3 returns immediately after Phase A.  Phase B is triggered by
        the drain_complete control event, which submits a CleanupComputeAlert.
        """
        logger.info("[elasticity] scale_down_compute: removing %s (mac=%s)", alert.container_name, alert.mac)

        # VIP first: Thread 1 must stop creating DNAT/SNAT flows toward this node
        # before drain. The helper also clears its compute warm lease.
        self._topo.unregister_server_backend(alert.mac)

        pending = self._compute_adder.initiate_drain(alert.lan, alert.container_name, alert.mac)
        if pending is None:
            # Veth discovery failed — the container netns is already gone.
            logger.warning("[elasticity] veth discovery failed for %s — attempting cleanup without veth", alert.container_name)
            if alert.ip:
                self._get_allocator(alert.lan).release(alert.ip)
            with self._removal_complete_lock:
                self._removal_complete_entries.append((alert.mac, alert.container_name))
            return

        pending.ip = alert.ip
        pending.reason = alert.reason
        self._set_pending_drain(alert.mac, pending)

        if not pending.drain_signaled:
            logger.info("[elasticity] drain failed for %s — submitting immediate CleanupComputeAlert", alert.container_name)
            self.submit(CleanupComputeAlert(mac=alert.mac))
        else:
            logger.info("[elasticity] drain initiated for %s — waiting for drain_complete event", alert.container_name)

    def _handle_cleanup_compute(self, alert: CleanupComputeAlert) -> None:
        """Phase B: stop container, flush flows, remove OVS port/veth, docker rm.

        Triggered by a drain_complete control event.
        """
        pending = self._get_pending_drain(alert.mac)
        if pending is None:
            logger.warning("[elasticity] CleanupComputeAlert for unknown mac=%s — ignoring", alert.mac)
            return

        logger.info("[elasticity] cleanup_compute: container=%s mac=%s", pending.container_name, alert.mac)
        result = self._compute_adder.cleanup_compute_node(pending)
        self._compute_adder.log_removal_timings(result)

        self._pop_pending_drain(alert.mac)

        # Release IP only after container is fully torn down
        if pending.ip:
            self._get_allocator(pending.lan).release(pending.ip)

        with self._removal_complete_lock:
            self._removal_complete_entries.append((alert.mac, pending.container_name))

        if result.success:
            logger.info("[elasticity] cleanup_compute done: container=%s", pending.container_name)
        else:
            logger.error("[elasticity] cleanup_compute FAILED: container=%s", pending.container_name)

    def _handle_abandon_compute(self, alert: AbandonComputeBackendAlert) -> None:
        """RQ3: full teardown of a spawned-but-never-ready compute backend.

        Runs in Thread 3 (busy=True) so the readiness-gate worker thread is not
        blocked and concurrent scale operations are gated by is_busy().
        """
        self._abandon_compute_backend(
            alert.mac, alert.ip, alert.container_name, alert.lan)

    def _handle_cancel_compute_drain(self, alert: CancelComputeDrainAlert) -> None:
        """Cancel one pending compute drain and re-admit the node to the VIP pool."""
        pending = self._select_pending_compute_drain(alert.mac)
        if pending is None:
            logger.info("[elasticity] no pending compute drain to cancel")
            return

        if self._compute_adder.cancel_drain(pending.container_name):
            self._topo.add_server_mac(pending.mac)
            self._pop_pending_drain(pending.mac)
            logger.info(
                "[elasticity] canceled compute drain mac=%s container=%s",
                pending.mac, pending.container_name,
            )
            return

        logger.warning(
            "[elasticity] cancel failed for compute drain mac=%s container=%s — submitting cleanup",
            pending.mac, pending.container_name,
        )
        self.submit_cleanup(pending.mac)

    def _handle_scale_down_data(self, alert: ScaleDownDataAlert) -> None:
        """Storage removal: VIP isolation → rs.remove() → script teardown."""
        logger.info("[elasticity] scale_down_data: removing %s (mac=%s)", alert.container_name, alert.mac)

        # VIP first: no new VIP_DATA flows for this node while it is evicted.
        # The helper also clears its storage warm lease.
        self._topo.unregister_storage_backend(alert.mac, domain=f"n{alert.lan}")

        result = self._storage_adder.remove_storage_node(
            lan=alert.lan,
            name=alert.container_name,
            mac=alert.mac,
            ip=alert.ip,
            rs_name=alert.rs_name,
            primary_container=alert.primary_container,
            port=alert.port,
        )
        self._storage_adder.log_removal_timings(result)

        if result.success:
            self._get_allocator(alert.lan).release(alert.ip)

        # Notify Thread 2 regardless of success so stale tracking is cleared.
        with self._removal_complete_lock:
            self._removal_complete_entries.append((alert.mac, alert.container_name))

        if result.success:
            logger.info("[elasticity] scale_down_data done: container=%s", alert.container_name)
        else:
            logger.error("[elasticity] scale_down_data FAILED: container=%s", alert.container_name)

    def _next_name(self, prefix: str, network_id: str) -> str:
        _COUNTER[network_id] = _COUNTER.get(network_id, 0) + 1
        name = f"{prefix}_{network_id}_dyn{_COUNTER[network_id]}"
        logger.debug("[elasticity] generated container name: %s", name)
        return name

    def _pending_drain_snapshot(self) -> tuple[PendingDrain, ...]:
        with self._lock:
            return tuple(self._pending_drains.values())

    def _get_pending_drain(self, mac: str) -> PendingDrain | None:
        with self._lock:
            return self._pending_drains.get(mac)

    def _set_pending_drain(self, mac: str, pending: PendingDrain) -> None:
        with self._lock:
            self._pending_drains[mac] = pending

    def _pop_pending_drain(self, mac: str) -> PendingDrain | None:
        with self._lock:
            return self._pending_drains.pop(mac, None)

    def _select_pending_compute_drain(self, mac: str | None = None) -> PendingDrain | None:
        with self._lock:
            if mac is not None:
                pending = self._pending_drains.get(mac)
                if pending and pending.node_type == "compute" and pending.reason != "absent":
                    return pending
                return None
            for pending in self._pending_drains.values():
                if pending.node_type == "compute" and pending.reason != "absent":
                    return pending
        return None
