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
from typing import Any, Callable, Mapping, Optional, Protocol

from ..readiness_gate import PendingBackend
from ..release_log import append_row
from ..scaling_config import _EDGE_READY_PORT, _RELEASE_MECHANISM

from .node_common import IpAllocator, NodeInfo, RemovalResult, log_ready_timing
from .compute_node_manager import ComputeNodeAdder, PendingDrain
from .storage_node_manager import StorageNodeAdder
from .selective_storage_manager import (
    SelectiveStorageNodeAdder,
    _ADMIN_PORT,
    _READINESS_MAX_WAIT_S,
    _wait_for_port,
)

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
    # Cross-region spawn: ``lan`` is the consumer LAN and ``owner_primary`` is
    # the resolved remote primary; same-LAN alerts leave ``owner_primary=""``.
    cross_lan_rs:      bool = False
    owner_lan:         str | None = None
    owner_primary:     str = ""   # "10.0.0.4:27018" for cross-region


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
    """Phase B trigger: submitted when drain_complete ZMQ event arrives (or
    telemetry timeout fallback).  Causes Thread 3 to run OVS teardown."""
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

    Same-LAN: ``owner_primary=""``, ``owner_lan=""``, RS name matches the
    local LAN.
    Cross-region: ``owner_primary`` set to the remote primary ``ip:port``
    (e.g. ``"10.0.0.4:27018"``), ``owner_lan`` is the data-source LAN
    (e.g. ``"lan1"``), and ``rs_name`` is the remote RS.

    The resulting node is created with ``standby_reserved=True`` and held
    outside VIP until activated by a load or recovery trigger.
    """
    lan: int
    network_id: str
    rs_name: str
    primary_container: str
    port: int = 27018
    owner_primary: str = ""   # "10.0.0.4:27018" for cross-region
    owner_lan: str = ""       # "lan1" for cross-region


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


# Tier 1 selective-sync alerts

@dataclass(frozen=True)
class SelectiveSyncAlert:
    """Raised by the consumer-side ``PromotionCoordinator`` once its local
    promotion predicate (sustained QoE breach + cross-region footprint +
    read-heavy op mix) fires for a ``(owner_lan, collections)`` pair.

    Triggers :class:`SelectiveStorageNodeAdder.add_selective_storage_node`
    and, on success, a single manifest broadcast via the optional
    ``on_spawned`` coordinator hook.
    """
    lan:           int                             # consumer LAN (this controller's LAN)
    network_id:    str                             # e.g. "lan2"
    owner_lan:     str                             # "lan1" — source region for replicated data
    owner_rs:      str                             # "rs_net1"
    owner_primary: str                             # "10.0.0.4:27018" — RS primary resolved from node_registry
    collections:   Mapping[str, tuple[str, ...]]   # {collection: hot_doc_ids}
    max_ttl_s:     int


@dataclass(frozen=True)
class SelectiveSyncReconfigureAlert:
    """Live update to an already-running Tier 1 container.

    Rebroadcasts the manifest and POSTs the new forwarder config to
    ``/forwarder_config``. Manifest-first ordering stops edge traffic on
    dropped collections before the forwarder closes their Change Streams.
    """
    lan:            int
    network_id:     str
    container_name: str
    container_ip:   str
    owner_lan:      str
    collections:    Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class ScaleDownSelectiveAlert:
    """Phase A teardown of a Tier 1 node. Mirrors :class:`ScaleDownComputeAlert`.

    The elasticity manager is the single revocation site for the manifest
    (``host: null`` broadcast), then POSTs ``/drain`` to the container and
    records a ``PendingDrain``. Phase B runs on ``drain_complete`` or the
    existing telemetry timeout fallback.
    """
    lan:            int
    network_id:     str
    owner_lan:      str
    container_name: str
    mac:            str
    ip:             str


@dataclass(frozen=True)
class CleanupSelectiveAlert:
    """Phase B trigger for Tier 1 teardown. Mirrors :class:`CleanupComputeAlert`."""
    mac: str


# Alert dispatch priorities — lower number wins.  Cleanup alerts sit beside
# their paired scale-down alerts so a pending Phase B beats a fresh scale-down.
_PRIORITY_DATA_ALERT                 = 1
_PRIORITY_SELECTIVE_SYNC             = 2
_PRIORITY_SELECTIVE_SYNC_RECONFIGURE = 3
_PRIORITY_PREPARE_STANDBY_STORAGE    = 4
_PRIORITY_COMPUTE_ALERT              = 4
_PRIORITY_CLEANUP_RESERVE            = 5
_PRIORITY_CLEANUP_COMPUTE            = 5
_PRIORITY_ABANDON_COMPUTE            = 5
_PRIORITY_CLEANUP_SELECTIVE          = 6
_PRIORITY_CANCEL_COMPUTE_DRAIN       = 7
_PRIORITY_SCALEDOWN_DATA             = 8
_PRIORITY_SCALEDOWN_SELECTIVE        = 9
_PRIORITY_SCALEDOWN_COMPUTE          = 10

# Tie-breaker: monotonically increasing sequence so alerts with the same
# priority are processed in FIFO order.
_alert_seq = itertools.count()

# Type → priority lookup (covers all alert types)
_ALERT_PRIORITY: dict[type, int] = {
    DataAlert:                      _PRIORITY_DATA_ALERT,
    SelectiveSyncAlert:             _PRIORITY_SELECTIVE_SYNC,
    SelectiveSyncReconfigureAlert:  _PRIORITY_SELECTIVE_SYNC_RECONFIGURE,
    PrepareStandbyStorageAlert:     _PRIORITY_PREPARE_STANDBY_STORAGE,
    CleanupReserveAlert:            _PRIORITY_CLEANUP_RESERVE,
    ComputeAlert:                   _PRIORITY_COMPUTE_ALERT,
    CleanupComputeAlert:            _PRIORITY_CLEANUP_COMPUTE,
    AbandonComputeBackendAlert:     _PRIORITY_ABANDON_COMPUTE,
    CleanupSelectiveAlert:          _PRIORITY_CLEANUP_SELECTIVE,
    CancelComputeDrainAlert:        _PRIORITY_CANCEL_COMPUTE_DRAIN,
    ScaleDownDataAlert:             _PRIORITY_SCALEDOWN_DATA,
    ScaleDownSelectiveAlert:        _PRIORITY_SCALEDOWN_SELECTIVE,
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
        *,
        selective_sync_coordinator: Any = None,
        broadcast_tier1_manifest: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        """Initialise the manager.

        Parameters
        ----------
        topology_mixin
            The owning controller; used for VIP pool mutations.
        selective_sync_coordinator
            Optional ``PromotionCoordinator`` providing ``on_spawned(...)``
            and ``drain(owner_lan, reason)``. When ``None`` the selective-sync
            handlers still perform container / registry work but skip the
            coordinator state-machine hooks (scaffolded until the
            coordinator lands — see tier1_selective_sync/event_protocol.md).
        broadcast_tier1_manifest
            Optional ``(network_id, manifest_dict) -> None`` callback that
            fans out ``PUT /tier1_manifest`` to edge servers in the given
            LAN. When ``None`` manifest broadcasts are logged and skipped.
        """
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._compute_adder = ComputeNodeAdder()
        self._storage_adder = StorageNodeAdder()
        self._selective_adder = SelectiveStorageNodeAdder()
        self._coordinator = selective_sync_coordinator
        self._broadcast_tier1_manifest_fn = broadcast_tier1_manifest
        self._topo    = topology_mixin
        # RQ3 readiness gate, injected by T7. None → pre-RQ3 behaviour:
        # register_new_server_backend immediately in _handle_compute.
        self.readiness_gate = None
        self._operation_log: list[dict] = []
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

    def attach_selective_sync_coordinator(self, coordinator) -> None:
        """Inject the Tier 1 ``PromotionCoordinator`` after construction."""
        self._coordinator = coordinator

    def attach_tier1_broadcaster(
        self, broadcast_tier1_manifest: Callable[[str, dict], None],
    ) -> None:
        """Inject the manifest-broadcast closure after construction."""
        self._broadcast_tier1_manifest_fn = broadcast_tier1_manifest

    def submit(self, alert) -> None:
        """Unified thread-safe enqueue for any alert type."""
        priority = _ALERT_PRIORITY.get(type(alert))
        if priority is None:
            logger.warning("[elasticity] unknown alert type %s \u2014 using lowest priority", type(alert).__name__)
            priority = _PRIORITY_SCALEDOWN_COMPUTE
        logger.info("[elasticity] alert submitted (priority=%d): %s", priority, alert)
        self._queue.put((priority, next(_alert_seq), alert))

    def submit_cleanup(self, mac: str) -> None:
        """Phase B dispatcher: routes on ``PendingDrain.node_type``.

        Compute → :class:`CleanupComputeAlert`; selective storage →
        :class:`CleanupSelectiveAlert`. Unknown MACs fall back to
        :class:`CleanupComputeAlert` with a warning so a stray
        ``drain_complete`` event can't wedge the queue.
        """
        pending = self._get_pending_drain(mac)
        if pending is None:
            logger.warning("[elasticity] submit_cleanup for unknown mac=%s — submitting CleanupComputeAlert", mac)
            self.submit(CleanupComputeAlert(mac=mac))
            return
        if pending.node_type == "selective_storage":
            self.submit(CleanupSelectiveAlert(mac=mac))
        else:
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

    def get_active_operations(self) -> list[dict]:
        """Return a snapshot of the operation audit trail (safe to call from any thread)."""
        with self._lock:
            return list(self._operation_log)

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
                        if (_RELEASE_MECHANISM == "immediate"
                                and getattr(alert, "reason", "scale_down") == "scale_down"):
                            self._handle_scale_down_compute_immediate(alert)
                        else:
                            self._handle_scale_down_compute(alert)
                    elif isinstance(alert, ScaleDownDataAlert):
                        if (_RELEASE_MECHANISM == "immediate"
                                and getattr(alert, "reason", "scale_down") == "scale_down"):
                            self._handle_scale_down_data_immediate(alert)
                        else:
                            self._handle_scale_down_data(alert)
                    elif isinstance(alert, CleanupComputeAlert):
                        self._handle_cleanup_compute(alert)
                    elif isinstance(alert, AbandonComputeBackendAlert):
                        self._handle_abandon_compute(alert)
                    elif isinstance(alert, CancelComputeDrainAlert):
                        self._handle_cancel_compute_drain(alert)
                    elif isinstance(alert, SelectiveSyncAlert):
                        self._handle_selective_sync(alert)
                    elif isinstance(alert, SelectiveSyncReconfigureAlert):
                        self._handle_selective_sync_reconfigure(alert)
                    elif isinstance(alert, ScaleDownSelectiveAlert):
                        self._handle_scale_down_selective(alert)
                    elif isinstance(alert, CleanupSelectiveAlert):
                        self._handle_cleanup_selective(alert)
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
        self._record({"type": "compute", "alert": alert, "name": name, "result": result})

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
        if alert.owner_primary:
            rs_seed_override = alert.owner_primary
            logger.info(
                "[elasticity] data: CROSS-REGION spawn lan=%d rs=%s seed=%s owner=%s",
                alert.lan, alert.rs_name, rs_seed_override, alert.owner_lan,
            )
        else:
            rs_seed_override = None

        name = self._next_name("edge_storage", alert.network_id)
        if alert.owner_primary:
            name = f"{name}_cs"
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
            rs_seed_host_override=rs_seed_override,
        )
        self._storage_adder.log_timings(result)
        self._record({"type": "data", "alert": alert, "name": name, "result": result})

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
                    owner_lan=alert.owner_lan or "",
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
        record_type: str,
        record_payload: dict,
    ) -> RemovalResult:
        """Best-effort reserve storage teardown — call, time, record, return.

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
        self._record({
            "type": record_type,
            **record_payload,
            "result": result,
        })
        return result

    def _handle_prepare_standby_storage(self, alert: PrepareStandbyStorageAlert) -> None:
        """Prepare one same-LAN storage reserve through the existing storage add path.

        Creates a real heartbeating SECONDARY with ``standby_reserved=True``.
        The node is held outside VIP until activated.
        """
        name = self._next_name("edge_storage", alert.network_id)
        if alert.owner_primary:
            name = f"{name}_cs"
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
            rs_seed_host_override=alert.owner_primary or None,
        )
        self._storage_adder.log_timings(result)
        self._record({"type": "prepare_standby_storage", "alert": alert, "name": name, "result": result})

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
                owner_lan=alert.owner_lan or "",
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
                record_type="prepare_standby_storage_cleanup",
                record_payload={"alert": alert, "name": name},
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
                record_type="cleanup_reserve",
                record_payload={"alert": alert},
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
            self._record({"type": "cleanup_reserve", "alert": alert})
            logger.info("[elasticity] cleanup_reserve: done %s", alert.container_name)

        # Recycled-MAC hygiene: the reserve never unregistered through the VIP
        # helpers, so drop its cached telemetry stats + discovery stamp here.
        self._topo.drop_backend_caches(alert.mac)

    def _handle_scale_down_compute(self, alert: ScaleDownComputeAlert) -> None:
        """Phase A: isolate from VIP, discover veth, send drain signal, return.

        Thread 3 returns immediately after Phase A.  Phase B is triggered by
        the drain_complete ZMQ event (or telemetry timeout fallback) which
        submits a CleanupComputeAlert.
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
            if _RELEASE_MECHANISM != "off" and alert.reason != "absent":
                append_row(
                    network_id=alert.network_id,
                    mechanism=_RELEASE_MECHANISM,
                    tier="compute",
                    container=alert.container_name,
                    mac=alert.mac,
                    trigger="scale_down",
                    event="end",
                    success="1",
                    reason="veth_discovery_failed",
                )
            return

        # Absent-cleanup is never logged; a real scale-down only gets its begin row
        # here, once the drain is pending (the failure path above returns first).
        if alert.reason != "absent" and _RELEASE_MECHANISM in ("drained", "stabilized"):
            append_row(
                network_id=alert.network_id,
                mechanism=_RELEASE_MECHANISM,
                tier="compute",
                container=alert.container_name,
                mac=alert.mac,
                trigger=alert.reason,
                event="begin",
                success="",
                reason="",
            )

        pending.ip = alert.ip
        pending.reason = alert.reason
        self._set_pending_drain(alert.mac, pending)
        self._record({"type": "scale_down_compute_phase_a", "alert": alert, "pending": pending})

        if not pending.drain_signaled:
            logger.info("[elasticity] drain failed for %s — submitting immediate CleanupComputeAlert", alert.container_name)
            self.submit(CleanupComputeAlert(mac=alert.mac))
        else:
            logger.info("[elasticity] drain initiated for %s — waiting for drain_complete event", alert.container_name)

    def _handle_scale_down_compute_immediate(self, alert: ScaleDownComputeAlert) -> None:
        """research_q3 immediate arm — compute teardown without the drain phase.

        VIP isolation + veth discovery, then straight to script teardown
        (no /drain POST, no Phase B).  Veth discovery failure is treated as
        immediate cleanup with best-effort teardown, mirroring the drained
        handler.
        """
        logger.info("[elasticity] scale_down_compute_immediate: removing %s (mac=%s)", alert.container_name, alert.mac)

        # VIP first: no new DNAT/SNAT flows while the container is torn down.
        self._topo.unregister_server_backend(alert.mac)

        pending = self._compute_adder.prepare_immediate(alert.lan, alert.container_name, alert.mac)
        if pending is None:
            # Veth discovery failed — the container netns is already gone.
            logger.warning("[elasticity] veth discovery failed for %s — attempting cleanup without veth", alert.container_name)
            if alert.ip:
                self._get_allocator(alert.lan).release(alert.ip)
            with self._removal_complete_lock:
                self._removal_complete_entries.append((alert.mac, alert.container_name))
            self._record({"type": "scale_down_compute_immediate_veth_fail", "alert": alert})
            append_row(
                network_id=alert.network_id,
                mechanism=_RELEASE_MECHANISM,
                tier="compute",
                container=alert.container_name,
                mac=alert.mac,
                trigger="scale_down",
                event="end",
                success="1",
                reason="veth_discovery_failed",
            )
            return

        pending.ip = alert.ip
        pending.reason = alert.reason
        result = self._compute_adder.cleanup_compute_node(pending)
        self._compute_adder.log_removal_timings(result)
        self._record({"type": "scale_down_compute_immediate", "alert": alert, "result": result})

        # Release IP only after the container is fully torn down
        if pending.ip:
            self._get_allocator(pending.lan).release(pending.ip)

        with self._removal_complete_lock:
            self._removal_complete_entries.append((alert.mac, alert.container_name))

        append_row(
            network_id=alert.network_id,
            mechanism=_RELEASE_MECHANISM,
            tier="compute",
            container=alert.container_name,
            mac=alert.mac,
            trigger="scale_down",
            event="end",
            success="1" if result.success else "0",
            reason="ok" if result.success else "teardown_failed",
        )

        if result.success:
            logger.info("[elasticity] scale_down_compute_immediate done: container=%s", alert.container_name)
        else:
            logger.error("[elasticity] scale_down_compute_immediate FAILED: container=%s", alert.container_name)

    def _handle_cleanup_compute(self, alert: CleanupComputeAlert) -> None:
        """Phase B: stop container, flush flows, remove OVS port/veth, docker rm.

        Triggered by drain_complete ZMQ event or telemetry timeout fallback.
        """
        pending = self._get_pending_drain(alert.mac)
        if pending is None:
            logger.warning("[elasticity] CleanupComputeAlert for unknown mac=%s — ignoring", alert.mac)
            return

        logger.info("[elasticity] cleanup_compute: container=%s mac=%s", pending.container_name, alert.mac)
        result = self._compute_adder.cleanup_compute_node(pending)
        self._compute_adder.log_removal_timings(result)
        self._record({"type": "scale_down_compute_phase_b", "alert": alert, "result": result})

        self._pop_pending_drain(alert.mac)

        # Release IP only after container is fully torn down
        if pending.ip:
            self._get_allocator(pending.lan).release(pending.ip)

        with self._removal_complete_lock:
            self._removal_complete_entries.append((alert.mac, pending.container_name))

        if pending.reason != "absent" and _RELEASE_MECHANISM in ("drained", "stabilized"):
            append_row(
                network_id=f"lan{pending.lan}",
                mechanism=_RELEASE_MECHANISM,
                tier="compute",
                container=pending.container_name,
                mac=alert.mac,
                trigger=pending.reason or "scale_down",
                event="end",
                success="1" if result.success else "0",
                reason="ok" if result.success else "teardown_failed",
            )

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
            self._record({"type": "cancel_compute_drain", "alert": alert, "pending": pending})
            if _RELEASE_MECHANISM != "off" and pending.reason == "scale_down":
                append_row(
                    network_id=f"lan{pending.lan}",
                    mechanism=_RELEASE_MECHANISM,
                    tier="compute",
                    container=pending.container_name,
                    mac=pending.mac,
                    trigger="cancel_drain",
                    event="end",
                    success="1",
                    reason="canceled",
                )
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

        # Absent-cleanup is never logged; real scale-downs get a begin row.
        if alert.reason != "absent" and _RELEASE_MECHANISM in ("drained", "stabilized"):
            append_row(
                network_id=alert.network_id,
                mechanism=_RELEASE_MECHANISM,
                tier="storage",
                container=alert.container_name,
                mac=alert.mac,
                trigger=alert.reason,
                event="begin",
                success="",
                reason="",
            )

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
        self._record({"type": "scale_down_data", "alert": alert, "result": result})

        if result.success:
            self._get_allocator(alert.lan).release(alert.ip)

        # Notify Thread 2 regardless of success so stale tracking is cleared.
        with self._removal_complete_lock:
            self._removal_complete_entries.append((alert.mac, alert.container_name))

        if alert.reason != "absent" and _RELEASE_MECHANISM in ("drained", "stabilized"):
            append_row(
                network_id=alert.network_id,
                mechanism=_RELEASE_MECHANISM,
                tier="storage",
                container=alert.container_name,
                mac=alert.mac,
                trigger=alert.reason,
                event="end",
                success="1" if result.success else "0",
                reason="ok" if result.success else "teardown_failed",
            )

        if result.success:
            logger.info("[elasticity] scale_down_data done: container=%s", alert.container_name)
        else:
            logger.error("[elasticity] scale_down_data FAILED: container=%s", alert.container_name)

    def _handle_scale_down_data_immediate(self, alert: ScaleDownDataAlert) -> None:
        """research_q3 immediate arm — storage teardown with fire-and-forget eviction.

        VIP isolation, then :meth:`remove_storage_node_immediate`: the
        rs.remove eviction is launched fire-and-forget against the primary
        while the script teardown runs.  The eviction outcome is carried in
        the eviction log file (joined by container name in analysis); the
        release-log end row carries only the teardown outcome.
        """
        logger.info("[elasticity] scale_down_data_immediate: removing %s (mac=%s)", alert.container_name, alert.mac)

        # VIP first: no new VIP_DATA flows while the container is torn down.
        self._topo.unregister_storage_backend(alert.mac, domain=f"n{alert.lan}")

        result = self._storage_adder.remove_storage_node_immediate(
            lan=alert.lan,
            name=alert.container_name,
            mac=alert.mac,
            ip=alert.ip,
            rs_name=alert.rs_name,
            primary_container=alert.primary_container,
            port=alert.port,
        )
        self._storage_adder.log_removal_timings(result)
        self._record({"type": "scale_down_data_immediate", "alert": alert, "result": result})

        if result.success:
            self._get_allocator(alert.lan).release(alert.ip)

        # Notify Thread 2 regardless of success so stale tracking is cleared.
        with self._removal_complete_lock:
            self._removal_complete_entries.append((alert.mac, alert.container_name))

        append_row(
            network_id=alert.network_id,
            mechanism=_RELEASE_MECHANISM,
            tier="storage",
            container=alert.container_name,
            mac=alert.mac,
            trigger="scale_down",
            event="end",
            success="1" if result.success else "0",
            reason="ok" if result.success else "teardown_failed",
        )

        if result.success:
            logger.info("[elasticity] scale_down_data_immediate done: container=%s", alert.container_name)
        else:
            logger.error("[elasticity] scale_down_data_immediate FAILED: container=%s", alert.container_name)

    # Tier 1 selective-sync handlers

    def _broadcast_tier1_manifest(self, network_id: str, manifest: dict) -> None:
        """Dispatch manifest fan-out via the injected broadcast callable, if any."""
        if self._broadcast_tier1_manifest_fn is None:
            logger.info(
                "[tier1] manifest broadcast skipped (no broadcaster configured) network=%s manifest=%s",
                network_id, manifest,
            )
            return
        try:
            self._broadcast_tier1_manifest_fn(network_id, manifest)
        except Exception:  
            logger.exception("[tier1] manifest broadcast failed network=%s", network_id)

    def _handle_selective_sync(self, alert: SelectiveSyncAlert) -> None:
        """Spawn a Tier 1 container and register it. Coordinator (if any) drives
        the ``SPAWNING -> ACTIVE`` flip and the first manifest broadcast."""
        name = self._next_name("sel_sync", alert.network_id)
        ip, mac = self._get_allocator(alert.lan).allocate()
        spawn_started_monotonic_s = time.monotonic()
        logger.info(
            "[elasticity] tier1: spawning %s on LAN %d (ip=%s mac=%s owner_lan=%s)",
            name, alert.lan, ip, mac, alert.owner_lan,
        )

        result = self._selective_adder.add_selective_storage_node(
            lan=alert.lan,
            name=name,
            primary_host=alert.owner_primary,
            collections=alert.collections,
            max_ttl_s=alert.max_ttl_s,
            ip=ip, mac=mac,
        )
        self._selective_adder.log_timings(result)
        self._record({"type": "selective_sync", "alert": alert, "name": name, "result": result})

        if not (result.success and result.ip):
            self._get_allocator(alert.lan).release(ip)
            logger.error("[elasticity] tier1: failed to spawn %s", name)
            # Tell the coordinator (if any) so its entry exits SPAWNING.
            if self._coordinator is not None:
                try:
                    self._coordinator.drain(alert.owner_lan, reason="spawn_failed")
                except Exception:  
                    logger.exception("[tier1] coordinator.drain(spawn_failed) raised")
            return

        effective_mac = result.mac or mac
        effective_ip  = result.ip or ip

        # Register the node so absence detection and scale-down tracking work.
        info = NodeInfo(
            mac=effective_mac, lan=alert.lan, network_id=alert.network_id,
            name=name, ip=effective_ip, node_type="selective_storage",
            owner_lan=alert.owner_lan,
            spawn_started_monotonic_s=spawn_started_monotonic_s,
        )
        with self._addition_complete_lock:
            self._addition_complete_infos.append(info)

        # Without this wait the first reconfigure POST races container startup
        # and fails (observed: 48 consecutive HTTPConnectionPool errors).
        ready = _wait_for_port(effective_ip, _ADMIN_PORT, _READINESS_MAX_WAIT_S)
        if not ready:
            logger.error(
                "[elasticity] tier1: %s admin port %s:%d not ready after %.0fs — "
                "cleaning up and draining",
                name, effective_ip, _ADMIN_PORT, _READINESS_MAX_WAIT_S,
            )
            self._selective_adder._cleanup_container(name)
            self._get_allocator(alert.lan).release(ip)
            if self._coordinator is not None:
                try:
                    self._coordinator.drain(alert.owner_lan, reason="spawn_failed")
                except Exception:
                    logger.exception("[tier1] coordinator.drain(spawn_failed) raised")
            return

        if not self._selective_adder.reconfigure(
            container_name=name,
            ip=effective_ip,
            collections=alert.collections,
        ):
            logger.error(
                "[elasticity] tier1: initial reconfigure failed for %s — "
                "cleaning up and draining",
                name,
            )
            self._selective_adder._cleanup_container(name)
            self._get_allocator(alert.lan).release(ip)
            if self._coordinator is not None:
                try:
                    self._coordinator.drain(alert.owner_lan, reason="spawn_failed")
                except Exception:
                    logger.exception("[tier1] coordinator.drain(spawn_failed) raised")
            return

        # Hand off to the coordinator: SPAWNING -> ACTIVE plus the single manifest
        # broadcast on the add path.
        if self._coordinator is not None:
            try:
                self._coordinator.on_spawned(
                    owner_lan=alert.owner_lan,
                    container=name,
                    mac=effective_mac,
                    ip=effective_ip,
                    spawn_started_monotonic_s=spawn_started_monotonic_s,
                )
            except Exception:  
                logger.exception("[tier1] coordinator.on_spawned raised")
        else:
            # No coordinator: best-effort broadcast so direct alert submission works.
            self._broadcast_tier1_manifest(alert.network_id, {
                "owner_lan":   alert.owner_lan,
                "host":        f"{effective_ip}:27018",
                "collections": {c: list(ids) for c, ids in alert.collections.items()},
            })
            log_ready_timing(
                name,
                "selective_storage",
                "tier1_active",
                time.monotonic() - spawn_started_monotonic_s,
                state="ACTIVE",
            )
        logger.info("[elasticity] tier1: %s online ip=%s mac=%s", name, effective_ip, effective_mac)

    def _handle_selective_sync_reconfigure(self, alert: SelectiveSyncReconfigureAlert) -> None:
        """Live update. Manifest first so edges stop traffic on dropped
        collections before the forwarder closes their Change Streams."""
        logger.info(
            "[elasticity] tier1: reconfigure %s collections=%d",
            alert.container_name, len(alert.collections),
        )
        self._broadcast_tier1_manifest(alert.network_id, {
            "owner_lan":   alert.owner_lan,
            "host":        f"{alert.container_ip}:27018",
            "collections": {c: list(ids) for c, ids in alert.collections.items()},
        })
        ok = self._selective_adder.reconfigure(
            container_name=alert.container_name,
            ip=alert.container_ip,
            collections=alert.collections,
        )
        self._record({"type": "selective_sync_reconfigure", "alert": alert, "ok": ok})

    def _handle_scale_down_selective(self, alert: ScaleDownSelectiveAlert) -> None:
        """Phase A: revoke manifest, POST /drain, record PendingDrain, return.

        Mirrors :meth:`_handle_scale_down_compute`. Phase B
        (:meth:`_handle_cleanup_selective`) runs on the ``drain_complete``
        ZMQ event dispatched by :class:`ControlEventDispatcher`, or on the
        existing telemetry-window timeout fallback.
        """
        if self._get_pending_drain(alert.mac) is not None:
            logger.info(
                "[elasticity] selective drain already pending for %s (%s) — ignoring duplicate request",
                alert.container_name,
                alert.mac,
            )
            return

        if not self._selective_adder.container_is_present(alert.container_name):
            logger.info(
                "[elasticity] selective node %s already absent — finalizing cleanup state",
                alert.container_name,
            )
            self._get_allocator(alert.lan).release(alert.ip)
            with self._removal_complete_lock:
                self._removal_complete_entries.append((alert.mac, alert.container_name))
            if self._coordinator and alert.owner_lan:
                self._coordinator.on_cleanup_complete(alert.owner_lan)
            return

        logger.info(
            "[elasticity] scale_down_selective: removing %s (mac=%s owner_lan=%s)",
            alert.container_name, alert.mac, alert.owner_lan,
        )

        # Single revocation site for the manifest: host=None stops edges routing
        # reads here BEFORE the forwarder closes its Change Streams.
        self._broadcast_tier1_manifest(alert.network_id, {
            "owner_lan":   alert.owner_lan,
            "host":        None,
            "collections": {},
        })

        pending = self._selective_adder.initiate_drain(
            lan=alert.lan,
            container_name=alert.container_name,
            mac=alert.mac,
            ip=alert.ip,
            owner_lan=alert.owner_lan,
        )
        if pending is None:
            logger.info(
                "[elasticity] tier1 drain could not create pending cleanup for %s",
                alert.container_name,
            )
            return

        self._set_pending_drain(alert.mac, pending)
        self._record({"type": "scale_down_selective_phase_a", "alert": alert, "pending": pending})

        if not pending.drain_signaled:
            logger.info(
                "[elasticity] tier1 drain failed for %s \u2014 immediate cleanup",
                alert.container_name,
            )
            self.submit(CleanupSelectiveAlert(mac=alert.mac))
            return

        logger.info(
            "[elasticity] tier1 drain initiated for %s \u2014 waiting for drain_complete",
            alert.container_name,
        )

    def _handle_cleanup_selective(self, alert: CleanupSelectiveAlert) -> None:
        """Phase B: OVS teardown + docker rm. Triggered by drain_complete or timeout fallback."""
        pending = self._get_pending_drain(alert.mac)
        if pending is None:
            logger.warning(
                "[elasticity] CleanupSelectiveAlert for unknown mac=%s \u2014 ignoring",
                alert.mac,
            )
            return

        logger.info(
            "[elasticity] cleanup_selective: container=%s mac=%s",
            pending.container_name, alert.mac,
        )
        result = self._selective_adder.remove_selective_storage_node(
            lan=pending.lan,
            name=pending.container_name,
            mac=pending.mac,
            ip=pending.ip,
            veth=pending.veth,
        )
        self._selective_adder.log_removal_timings(result)
        self._record({"type": "scale_down_selective_phase_b", "alert": alert, "result": result})

        self._pop_pending_drain(alert.mac)
        # Recycled-MAC hygiene (pre-emptive; SS is off in every v4 mode).
        self._topo.drop_backend_caches(alert.mac)

        if pending.ip:
            self._get_allocator(pending.lan).release(pending.ip)

        with self._removal_complete_lock:
            self._removal_complete_entries.append((alert.mac, pending.container_name))

        if result.success:
            if self._coordinator is not None and pending.owner_lan:
                try:
                    self._coordinator.on_cleanup_complete(pending.owner_lan)
                except Exception:
                    logger.exception(
                        "[tier1] coordinator.on_cleanup_complete raised owner=%s",
                        pending.owner_lan,
                    )
            logger.info("[elasticity] cleanup_selective done: container=%s", pending.container_name)
        else:
            logger.error("[elasticity] cleanup_selective FAILED: container=%s", pending.container_name)

    # Helpers

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

    def _record(self, entry: dict) -> None:
        with self._lock:
            self._operation_log.append(entry)
        logger.debug("[elasticity] recorded operation: type=%s name=%s", entry.get("type"), entry.get("name"))
