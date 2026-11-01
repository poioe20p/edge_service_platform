"""Consumer-side Tier 1 promotion coordinator.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable

from ..elasticity.elasticity import (
    ScaleDownSelectiveAlert,
    SelectiveSyncAlert,
    SelectiveSyncReconfigureAlert,
)
from ..elasticity.node_common import log_ready_timing
from ..scaling_config import (
    _SS_BREACH_WINDOWS_M,
    _SS_BREACH_WINDOWS_N,
    _SS_COOLDOWN_S,
    _SS_ENABLED,
    _SS_HOT_DOC_LIMIT,
    _SS_MAX_TTL_S,
    _SS_MIN_READS_PER_WINDOW,
    _SS_PROMOTION_CROSS_REGION_THRESHOLD,
    _SS_SCALEDOWN_THRESHOLD,
    _SS_SCALEDOWN_WINDOW,
    _SS_STALENESS_LIMIT_S,
    _SS_WRITE_RATIO_MAX,
)
from ..telemetry.models import TelemetrySummary
from .hotness import (
    breach_this_window,
    merge_edge_access,
    write_ratio,
)

if TYPE_CHECKING:
    from .state_publisher import Tier1OwnerState

logger = logging.getLogger("os_ken.selective_sync.promotion")


class _State(Enum):
    NONE = auto()
    SPAWNING = auto()
    ACTIVE = auto()
    DRAINING = auto()


@dataclass
class _Entry:
    """Per-``owner_lan`` state on the consumer side."""
    state:          _State = _State.NONE
    hot:            dict[str, tuple[str, ...]] = field(default_factory=dict)
    owner_rs:       str | None = None
    primary_host:   str | None = None
    container:      str | None = None
    mac:            str | None = None
    ip:             str | None = None
    cooldown_until: float = 0.0
    cold_windows:   dict[str, int] = field(default_factory=dict)
    # M-of-N debounce ring for the sustained-breach gate; cleared on transitions
    # back to NONE so stale history can't short-circuit the next promotion cycle.
    breach_ring:    deque = field(
        default_factory=lambda: deque(maxlen=_SS_BREACH_WINDOWS_N))


# Type alias for the resolver closure injected from ``main_n*.py``: given an
# ``owner_lan``, returns ``(rs_name, "ip:27018")`` of the peer RS primary or None.
_Resolver = Callable[[str], "tuple[str, str] | None"]

# Type alias for the manifest-broadcast closure injected from ``main_n*.py``.
_Broadcaster = Callable[[str, dict], None]


class PromotionCoordinator:
    """Drives the NONE → SPAWNING → ACTIVE → DRAINING state machine per
    ``owner_lan`` for this controller's LAN. All decisions are local — no
    cross-controller events; the peer RS primary is looked up from cached
    topology via ``resolve_owner_primary``.
    """

    def __init__(self,
                 my_lan: str,
                 elasticity,
                 broadcast_tier1_manifest: _Broadcaster,
                 resolve_owner_primary: _Resolver):
        self._my_lan     = my_lan
        self._elasticity = elasticity
        self._broadcast  = broadcast_tier1_manifest
        self._resolve    = resolve_owner_primary
        self._by_owner: dict[str, _Entry] = {}

    def evaluate(self, summary: TelemetrySummary) -> None:
        """Run the promotion predicate against one telemetry window.

        Called from the Thread 2 telemetry callback *after* the
        ``network_id != my_lan`` guard has passed.
        """
        if not _SS_ENABLED:
            return

        now = time.monotonic()

        # Signal A: sustained QoE breach — append the per-owner observation up-front
        # so owners failing the footprint gate still accumulate fresh history.
        owner_lans_in_access = {
            stat.owner_lan
            for srv in summary.servers.values()
            for stat in srv.access
        }
        for owner_lan in owner_lans_in_access:
            entry = self._by_owner.setdefault(owner_lan, _Entry())
            entry.breach_ring.append(breach_this_window(summary, owner_lan))

        hot = merge_edge_access(summary, top_n=_SS_HOT_DOC_LIMIT)

        by_owner: dict[str, dict[str, tuple[str, ...]]] = {}
        for (owner_lan, coll), agg in hot.items():
            if agg["total"] < _SS_MIN_READS_PER_WINDOW:
                continue
            if agg["total"] <= 0:
                continue
            if agg["xregion"] / agg["total"] < _SS_PROMOTION_CROSS_REGION_THRESHOLD:
                continue
            if write_ratio(summary, owner_lan, coll) > _SS_WRITE_RATIO_MAX:
                continue
            entry = self._by_owner.setdefault(owner_lan, _Entry())
            # Sustained-breach debounce only gates the *first* promotion;
            # an already-ACTIVE entry can reconfigure without re-checking.
            if (entry.state is _State.NONE
                    and sum(entry.breach_ring) < _SS_BREACH_WINDOWS_M):
                continue
            by_owner.setdefault(owner_lan, {})[coll] = tuple(
                d for d, _ in agg["top_docs"]
            )

        for owner_lan, hot_set in by_owner.items():
            entry = self._by_owner[owner_lan]
            if entry.state is _State.NONE and now >= entry.cooldown_until:
                self._spawn(owner_lan, hot_set, entry)
            elif entry.state is _State.ACTIVE and hot_set != entry.hot:
                self._reconfigure(owner_lan, hot_set, entry)

        # Local drain signals — cold-set + staleness. Tier 2 supersede is driven
        # from scaling_policy and is dormant (no cross-LAN DataAlert — event_protocol.md §2.4).
        for owner_lan, entry in list(self._by_owner.items()):
            if entry.state is not _State.ACTIVE:
                continue
            if self._is_cold(summary, owner_lan, entry):
                self.drain(owner_lan, reason="cold_set")
            elif self._is_stale(summary, owner_lan, entry):
                self.drain(owner_lan, reason="staleness")

    def on_spawned(self, owner_lan: str, container: str,
                   mac: str, ip: str,
                   spawn_started_monotonic_s: float | None = None) -> None:
        """Complete SPAWNING → ACTIVE and broadcast the first manifest."""
        entry = self._by_owner.get(owner_lan)
        if entry is None or entry.state is not _State.SPAWNING:
            logger.warning(
                "[tier1] on_spawned owner=%s in state=%s, ignoring",
                owner_lan, entry.state if entry else None,
            )
            return
        entry.container = container
        entry.mac       = mac
        entry.ip        = ip
        entry.state     = _State.ACTIVE
        self._safe_broadcast(self._my_lan, {
            "owner_lan":   owner_lan,
            "host":        f"{ip}:27018",
            "collections": {c: list(ids) for c, ids in entry.hot.items()},
        })
        if spawn_started_monotonic_s is not None and spawn_started_monotonic_s > 0:
            log_ready_timing(
                container,
                "selective_storage",
                "tier1_active",
                time.monotonic() - spawn_started_monotonic_s,
                state="ACTIVE",
            )
        logger.info("[tier1] ACTIVE owner=%s container=%s ip=%s",
                    owner_lan, container, ip)

    def drain(self, owner_lan: str, reason: str) -> None:
        """Idempotent teardown trigger — submits a ScaleDownSelectiveAlert
        and transitions ACTIVE → DRAINING (or NONE if mid-spawn).
        """
        entry = self._by_owner.get(owner_lan)
        if entry is None or entry.state in (_State.NONE, _State.DRAINING):
            return
        if entry.container is None or entry.mac is None or entry.ip is None:
            # Mid-SPAWNING: the alert is in flight but on_spawned hasn't run yet.
            # Abort and wait for cooldown; the spawn-failure path owns the container.
            logger.warning(
                "[tier1] drain owner=%s in %s before on_spawned — aborting",
                owner_lan, entry.state,
            )
            entry.state = _State.NONE
            entry.cooldown_until = time.monotonic() + _SS_COOLDOWN_S
            entry.breach_ring.clear()
            return
        logger.info("[tier1] drain owner=%s reason=%s", owner_lan, reason)
        entry.state = _State.DRAINING
        try:
            self._elasticity.submit(ScaleDownSelectiveAlert(
                lan=self._lan_number(),
                network_id=self._my_lan,
                owner_lan=owner_lan,
                container_name=entry.container,
                mac=entry.mac,
                ip=entry.ip,
            ))
        finally:
            entry.cooldown_until = time.monotonic() + _SS_COOLDOWN_S
            entry.breach_ring.clear()

    def on_cleanup_complete(self, owner_lan: str) -> None:
        """Complete DRAINING → NONE after successful Phase B cleanup."""
        entry = self._by_owner.get(owner_lan)
        if entry is None:
            logger.warning(
                "[tier1] cleanup_complete owner=%s in state=%s, ignoring",
                owner_lan, None,
            )
            return
        if entry.state is _State.NONE:
            logger.debug(
                "[tier1] cleanup_complete owner=%s ignored — state already NONE",
                owner_lan,
            )
            return
        if entry.state is not _State.DRAINING:
            logger.warning(
                "[tier1] cleanup_complete owner=%s in state=%s, ignoring",
                owner_lan, entry.state,
            )
            return

        entry.state = _State.NONE
        entry.hot = {}
        entry.owner_rs = None
        entry.primary_host = None
        entry.container = None
        entry.mac = None
        entry.ip = None
        entry.cold_windows.clear()
        entry.breach_ring.clear()
        logger.info("[tier1] cleanup complete owner=%s", owner_lan)

    def snapshot(self) -> dict[str, "Tier1OwnerState"]:
        """Return a per-``owner_lan`` snapshot of state-machine internals.

        Read-only; safe to call after :meth:`evaluate`. Consumed by
        :class:`CoordinatorStatePublisher` to feed observability into
        ``resource_stats.csv``. Returns an empty dict when no owner_lan has
        ever been observed (e.g. baseline runs with ``SS_ENABLED=0``).
        """
        # Local import: the dataclass is only needed at this boundary.
        from .state_publisher import Tier1OwnerState

        now = time.monotonic()
        out: dict[str, Tier1OwnerState] = {}
        for owner_lan, entry in self._by_owner.items():
            out[owner_lan] = Tier1OwnerState(
                state=entry.state.name,
                breach_ring_filled=sum(entry.breach_ring),
                breach_ring_capacity=entry.breach_ring.maxlen or 0,
                cooldown_remaining_s=max(0.0, entry.cooldown_until - now),
                hot_collections=tuple(entry.hot.keys()),
                hot_doc_total=sum(len(v) for v in entry.hot.values()),
                container=entry.container,
            )
        return out

    def _lan_number(self) -> int:
        try:
            return int(self._my_lan.replace("lan", ""))
        except ValueError:
            return 0

    def _safe_broadcast(self, lan: str, manifest: dict) -> None:
        try:
            self._broadcast(lan, manifest)
        except Exception:
            logger.exception("[tier1] manifest broadcast raised")

    def _spawn(self, owner_lan: str,
               hot: dict[str, tuple[str, ...]], entry: _Entry) -> None:
        resolved = self._resolve(owner_lan)
        if resolved is None:
            logger.warning(
                "[tier1] no primary known for owner=%s — skipping promotion",
                owner_lan,
            )
            return
        rs_name, primary_host = resolved
        entry.owner_rs     = rs_name
        entry.primary_host = primary_host
        entry.hot          = hot
        entry.state        = _State.SPAWNING
        self._elasticity.submit(SelectiveSyncAlert(
            lan=self._lan_number(),
            network_id=self._my_lan,
            owner_lan=owner_lan,
            owner_rs=rs_name,
            owner_primary=primary_host,
            collections=hot,
            max_ttl_s=_SS_MAX_TTL_S,
        ))
        logger.info(
            "[tier1] promote owner=%s rs=%s primary=%s collections=%s",
            owner_lan, rs_name, primary_host, list(hot),
        )

    def _reconfigure(self, owner_lan: str,
                     hot: dict[str, tuple[str, ...]], entry: _Entry) -> None:
        entry.hot = hot
        if entry.container is None or entry.ip is None:
            return  # shouldn't happen in ACTIVE, but guard anyway
        self._elasticity.submit(SelectiveSyncReconfigureAlert(
            lan=self._lan_number(),
            network_id=self._my_lan,
            container_name=entry.container,
            container_ip=entry.ip,
            owner_lan=owner_lan,
            collections=hot,
        ))
        logger.info("[tier1] reconfigure owner=%s collections=%s",
                    owner_lan, list(hot))

    def _is_cold(self, summary: TelemetrySummary,
                 owner_lan: str, entry: _Entry) -> bool:
        """True when **every** collection in ``entry.hot`` has been cold for
        ``_SS_SCALEDOWN_WINDOW`` consecutive windows (per-collection rings).

        Partial cold sets fall through; the next ``evaluate`` tick will
        submit a reconfigure via the delta path if the hot set shrinks.
        """
        hot = merge_edge_access(summary, top_n=_SS_HOT_DOC_LIMIT)
        all_cold = True
        for coll in entry.hot:
            xregion = hot.get((owner_lan, coll), {}).get("xregion", 0)
            if xregion < _SS_SCALEDOWN_THRESHOLD:
                entry.cold_windows[coll] = entry.cold_windows.get(coll, 0) + 1
            else:
                entry.cold_windows[coll] = 0
                all_cold = False
            if entry.cold_windows.get(coll, 0) < _SS_SCALEDOWN_WINDOW:
                all_cold = False
        return all_cold

    def _is_stale(self, summary: TelemetrySummary,
                  owner_lan: str, entry: _Entry) -> bool:
        """True if *any* collection's Change Stream lag exceeds the ceiling.

        Shared mongod + shared remote connection → one bad collection
        implicates the whole container, so this triggers teardown rather
        than reconfigure.
        """
        if entry.mac is None:
            return False
        ss = summary.storage_servers.get(entry.mac)
        if ss is None or not ss.selective_sync_per_collection:
            return False
        return any(
            stats.lag_s > _SS_STALENESS_LIMIT_S
            for stats in ss.selective_sync_per_collection.values()
        )
