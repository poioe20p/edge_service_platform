"""node_registry.py — Dynamic node lifecycle tracking (Registry pattern).

Tracks which dynamically added nodes exist, detects disappeared nodes,
and builds scale-down alerts. Does NOT submit alerts or access the
ElasticityManager beyond consuming completions.

Thread safety: all methods are called exclusively from Thread 2.
"""

import logging
import time
from dataclasses import dataclass

from .scaling_config import (
    _TELEMETRY_TIMEOUT_S,
    _NODE_BIRTH_GRACE_S,
    _STORAGE_PERSISTENT_RESERVE_ENABLED,
    _STORAGE_RESERVE_PENDING_WINDOWS,
)
from .elasticity.elasticity import (
    ElasticityManager,
    ScaleDownComputeAlert,
    ScaleDownDataAlert,
)
from .elasticity.node_common import NodeInfo
from .telemetry.models import TelemetrySummary

logger = logging.getLogger(__name__)


@dataclass
class StorageReserveSlot:
    """Controller-side state for one persistent same-LAN storage reserve.

    Owned by Thread 2. One slot per LAN.
    """
    lan: int
    state: str = "NONE"          # NONE | PREPARING | READY_RESERVED
    mac: str = ""
    ip: str = ""
    name: str = ""
    activation_pending: bool = False
    pending_reason: str = ""
    pending_windows_remaining: int = 0


class DynamicNodeRegistry:
    """Tracks dynamically added nodes for scale-down and absence detection.

    Owns: MAC tracking sets, active node map, absence counters, birth timestamps.
    Answers queries — does NOT submit alerts or touch the elasticity manager.
    """

    def __init__(self) -> None:
        self._dynamic_node_macs: set[str] = set()
        self._active: dict[str, NodeInfo] = {}     # mac → NodeInfo, insertion order = LIFO
        # Time-based absence (Design B): monotonic last-seen per node, refreshed
        # whenever a delivered window contains the node.
        self._last_seen_mono: dict[str, float] = {}
        self._birth_ts: dict[str, float] = {}
        # Persistent reserve — one slot per LAN, keyed by LAN number.
        self._reserve_slots: dict[int, StorageReserveSlot] = {}

    # Thread 3 → Thread 2 sync
    def sync(self, elasticity: ElasticityManager) -> None:
        """Consume removal and addition completions from Thread 3."""
        for mac, name in elasticity.consume_removal_completions():
            existing = self._active.get(mac)
            if existing is not None and existing.name != name:
                logger.warning(
                    "[registry] skipping stale removal for mac=%s — "
                    "expected name=%r but _active has name=%r "
                    "(MAC was recycled after removal was submitted)",
                    mac, name, existing.name,
                )
                continue
            self._dynamic_node_macs.discard(mac)
            self._last_seen_mono.pop(mac, None)
            self._active.pop(mac, None)
            self._birth_ts.pop(mac, None)
            logger.info("[registry] removed MAC %s from tracking after cleanup (name=%s)", mac, name)

        for info in elasticity.consume_addition_completions():
            self._dynamic_node_macs.add(info.mac)
            self._active[info.mac] = info
            self._birth_ts[info.mac] = time.monotonic()
            self._last_seen_mono[info.mac] = time.monotonic()
            logger.info("[registry] tracking new dynamic %s node mac=%s name=%s",
                        info.node_type, info.mac, info.name)

    def detect_absent(self, summary: TelemetrySummary) -> list[str]:
        """Return MACs whose last reported presence is older than ``_TELEMETRY_TIMEOUT_S``.

        Time-based (Design B): runs on the controller's fixed housekeeping
        clock, independent of telemetry delivery cadence. Presence is refreshed
        whenever a delivered window contains the node. Missing last-seen
        defaults to now, so a freshly re-added node is treated as just-seen
        (birth grace is a second protection layer).

        A node is only aged when the window carried evidence for its tier:
        HTTP request samples for compute nodes, storage summaries for
        storage nodes (liveness-only heartbeat entries included — they
        evidence the storage tier is alive). Windows without tier evidence —
        telemetry blackouts — must never produce absence.
        """
        now = time.monotonic()
        request_evidence = summary.request_evidence
        storage_evidence = bool(summary.storage_servers)
        timed_out: list[str] = []
        for mac in list(self._dynamic_node_macs):
            if now - self._birth_ts.get(mac, float('-inf')) < _NODE_BIRTH_GRACE_S:
                continue

            present = (mac in summary.servers) or (mac in summary.storage_servers)
            if present:
                self._last_seen_mono[mac] = now
                continue
            info = self._active.get(mac)
            if info is not None and info.node_type == "storage":
                if not storage_evidence:
                    continue
            elif not request_evidence:
                continue
            if now - self._last_seen_mono.get(mac, now) > _TELEMETRY_TIMEOUT_S:
                logger.warning(
                    "[registry] mac=%s not seen for %.0fs — triggering removal",
                    mac, now - self._last_seen_mono.get(mac, now),
                )
                timed_out.append(mac)
        return timed_out

    def refresh_presence(self, summary: TelemetrySummary) -> None:
        """Refresh liveness for every tracked dynamic node in a delivered window.

        Called on delivery, outside the housekeeping churn guard, so
        suppression can no longer freeze presence ages. Heartbeat-only
        entries count as presence — liveness is their purpose.
        """
        now = time.monotonic()
        for mac in self._dynamic_node_macs:
            if mac in summary.servers or mac in summary.storage_servers:
                self._last_seen_mono[mac] = now

    def find_last_dynamic(self, node_type: str) -> NodeInfo | None:
        """LIFO lookup for most recently added dynamic node of the given type.

        Reserved nodes (``standby_reserved=True``) are skipped — they are
        not eligible for ordinary LIFO scale-down.
        """
        for mac, info in reversed(list(self._active.items())):
            if (info.node_type == node_type and mac in self._dynamic_node_macs
                    and not info.standby_reserved):
                return info
        return None

    def list_dynamic(self, node_type: str) -> list[NodeInfo]:
        """Return tracked dynamic nodes of the given type in insertion order."""
        return [
            info
            for mac, info in self._active.items()
            if info.node_type == node_type and mac in self._dynamic_node_macs
        ]

    def count_dynamic(self, node_type: str, lan: int | None = None) -> int:
        """Count dynamic nodes of the given type.

        Reserved storage nodes (``standby_reserved=True``) are excluded —
        they do not count toward active storage thresholds.

        When *lan* is provided, only nodes on that LAN are counted.
        """
        return sum(
            1 for info in self._active.values()
            if info.node_type == node_type
            and not info.standby_reserved
            and (lan is None or info.lan == lan)
        )

    def node_age_s(self, mac: str, now: float | None = None) -> float:
        """Return monotonic age of a tracked node in seconds."""
        current = time.monotonic() if now is None else now
        return current - self._birth_ts.get(mac, current)

    def get_node_info(self, mac: str) -> NodeInfo | None:
        return self._active.get(mac)

    def _ensure_reserve_slot(self, lan: int) -> StorageReserveSlot:
        """Return (creating if necessary) the reserve slot for *lan*."""
        if lan not in self._reserve_slots:
            self._reserve_slots[lan] = StorageReserveSlot(lan=lan)
        return self._reserve_slots[lan]

    def get_storage_reserve_slot(self, lan: int) -> StorageReserveSlot:
        """Public read-only access to the reserve slot for *lan*."""
        return self._ensure_reserve_slot(lan)

    def should_prepare_storage_reserve(self, lan: int) -> bool:
        """True when the feature is on and *lan* needs a reserve preparation."""
        if not _STORAGE_PERSISTENT_RESERVE_ENABLED:
            return False
        slot = self._ensure_reserve_slot(lan)
        return slot.state == "NONE"

    def mark_storage_reserve_prepare_submitted(self, lan: int) -> None:
        """Record that a reserve preparation was submitted for *lan*."""
        slot = self._ensure_reserve_slot(lan)
        slot.state = "PREPARING"
        logger.info("[reserve] prepare_submitted lan=%d", lan)

    def mark_storage_reserve_ready(self, mac: str) -> None:
        """Move the reserve identified by *mac* to READY_RESERVED."""
        info = self.get_node_info(mac)
        if info is None:
            logger.warning(
                "[reserve] mark_storage_reserve_ready called for mac=%s "
                "but _active lookup returned None — slot NOT marked ready",
                mac,
            )
            return
        if not info.standby_reserved:
            logger.warning("[reserve] mark_storage_reserve_ready for non-reserved mac=%s — ignoring", mac)
            return
        slot = self._ensure_reserve_slot(info.lan)
        slot.state = "READY_RESERVED"
        slot.mac = info.mac
        slot.ip = info.ip
        slot.name = info.name
        logger.info("[reserve] ready_reserved lan=%d name=%s ip=%s mac=%s",
                    info.lan, info.name, info.ip, info.mac)

    def latch_storage_reserve_activation(self, lan: int, reason: str, windows: int | None = None) -> None:
        """Mark that activation is pending, bounded by *windows* telemetry cycles.

        Re-latching (when already pending) refreshes the budget so a fresh
        trigger restarts the countdown.
        """
        if windows is None:
            windows = _STORAGE_RESERVE_PENDING_WINDOWS
        slot = self._ensure_reserve_slot(lan)
        slot.activation_pending = True
        slot.pending_reason = reason
        slot.pending_windows_remaining = windows
        logger.info("[reserve] waiting_ready lan=%d reason=%s windows=%d", lan, reason, windows)

    def tick_storage_reserve_pending_activation(self, lan: int) -> bool:
        """Decrement the pending-activation budget for *lan* by one window.

        Returns True when the budget has just reached zero (expired).
        Does nothing (returns False) when no activation is pending or the
        reserve has already been activated.
        """
        slot = self._ensure_reserve_slot(lan)
        if not slot.activation_pending:
            return False
        if slot.state == "READY_RESERVED":
            # Don't count down while ready — the next activation check will consume it.
            return False
        if slot.pending_windows_remaining <= 0:
            return True  # already expired — signal again so caller can clean up
        slot.pending_windows_remaining -= 1
        if slot.pending_windows_remaining <= 0:
            logger.info("[reserve] pending_expired lan=%d reason=%s", lan, slot.pending_reason)
            return True
        return False

    def clear_storage_reserve_pending_activation(self, lan: int) -> None:
        """Clear only the pending-activation fields; leave slot state alone."""
        slot = self._ensure_reserve_slot(lan)
        slot.activation_pending = False
        slot.pending_reason = ""
        slot.pending_windows_remaining = 0

    def mark_storage_reserve_prepare_failed(self, lan: int) -> None:
        """Clear a PREPARING slot back to NONE after a failed spawn.

        Pending activation is preserved so a waiting trigger can still be
        satisfied by a future replacement reserve.
        """
        slot = self._ensure_reserve_slot(lan)
        if slot.state != "PREPARING":
            return
        slot.state = "NONE"
        slot.mac = ""
        slot.ip = ""
        slot.name = ""
        logger.info("[reserve] prepare_failed lan=%d (pending=%s)", lan, slot.activation_pending)

    def unregister_reserved_node(self, mac: str) -> None:
        """Fully remove a reserved node from all registry tracking structures.

        This is a dedicated cleanup path — not ordinary scale-down.
        Idempotent: safe to call multiple times for the same MAC.
        """
        self._dynamic_node_macs.discard(mac)
        self._last_seen_mono.pop(mac, None)
        self._active.pop(mac, None)
        self._birth_ts.pop(mac, None)
        logger.info("[reserve] unregistered mac=%s from tracking", mac)

    def consume_ready_storage_reserve(self, lan: int) -> NodeInfo | None:
        """Consume the ready reserve for *lan* and return its NodeInfo.

        Constructs NodeInfo from the slot's stored identity — does NOT
        require the node to be present in ``_active`` (survives MAC
        recycling collisions).  On success the node is re-added or
        updated in ``_active`` so scale-down and absent detection continue
        to work.  The slot is cleared ONLY on success.

        Returns None if no ready reserve exists.
        """
        slot = self._ensure_reserve_slot(lan)
        if slot.state != "READY_RESERVED" or not slot.mac:
            return None

        # Prefer the live _active entry for optional fields, but never require it —
        # the slot is the authoritative source for activation.
        existing = self.get_node_info(slot.mac)
        if existing is None:
            logger.warning(
                "[reserve] slot has mac=%s name=%s but _active lookup returned None "
                "— constructing NodeInfo from slot (likely MAC recycling collision)",
                slot.mac, slot.name,
            )
            info = NodeInfo(
                mac=slot.mac,
                lan=lan,
                network_id=f"lan{lan}",
                name=slot.name,
                ip=slot.ip,
                node_type="storage",
                standby_reserved=True,
            )
            # Re-add to _active so scale-down / absent detection work.
            self._active[slot.mac] = info
            self._dynamic_node_macs.add(slot.mac)
            self._birth_ts[slot.mac] = time.monotonic()
            self._last_seen_mono[slot.mac] = time.monotonic()
            logger.info(
                "[reserve] re-added mac=%s to _active after slot-based reconstruction",
                slot.mac,
            )
        else:
            info = existing

        activated_name = info.name
        activated_ip   = info.ip
        activated_mac  = slot.mac
        activated_reason = slot.pending_reason or "load"
        slot.state = "NONE"
        slot.mac = ""
        slot.ip = ""
        slot.name = ""
        slot.activation_pending = False
        slot.pending_reason = ""
        slot.pending_windows_remaining = 0
        logger.info("[reserve] activated lan=%d name=%s ip=%s mac=%s reason=%s",
                    lan, activated_name, activated_ip, activated_mac, activated_reason)
        return info

    def mark_storage_reserve_lost(self, mac: str) -> None:
        """Handle reserve loss — clear the slot identity so replenish can start.

        Preserves pending activation so a waiting trigger can carry forward
        to the replacement reserve.  Callers must call this **before**
        :meth:`unregister_reserved_node` so the node is still present in
        the registry when the slot is cleared.

        Includes a slot-scan fallback: if the node has already been removed
        from the registry (e.g. by an earlier unregister), scans all reserve
        slots for the matching MAC and clears the first match.
        """
        info = self.get_node_info(mac)
        if info is not None:
            lan = info.lan
            slot = self._ensure_reserve_slot(lan)
            if slot.mac == mac or slot.state == "PREPARING":
                slot.state = "NONE"
                slot.mac = ""
                slot.ip = ""
                slot.name = ""
                logger.info("[reserve] lost lan=%d mac=%s (pending=%s windows_left=%d)",
                            lan, mac, slot.activation_pending, slot.pending_windows_remaining)
                return

        for lan, slot in self._reserve_slots.items():
            if slot.mac == mac:
                slot.state = "NONE"
                slot.mac = ""
                slot.ip = ""
                slot.name = ""
                logger.info("[reserve] lost lan=%d mac=%s (fallback, pending=%s windows_left=%d)",
                            lan, mac, slot.activation_pending, slot.pending_windows_remaining)
                return

    def can_scale_down_storage(self, candidate_mac: str, lan: int) -> bool:
        """Return True only if removing *candidate_mac* is safe.

        When more than 2 dynamic (non-reserved) storage nodes exist on the
        LAN, scale-down is allowed without waiting for a READY_RESERVED slot.
        Once down to ≤2 dynamic storage nodes, the original reserve-floor
        guard is enforced to maintain resilience.
        """
        # Count dynamic (non-reserved) storage nodes on this LAN. Retained nodes are
        # counted (they physically exist), so the floor never sheds present capacity.
        dyn_on_lan = sum(
            1 for info in self._active.values()
            if info.node_type == "storage"
            and info.lan == lan
            and not info.standby_reserved
        )
        if dyn_on_lan <= 2:
            slot = self._ensure_reserve_slot(lan)
            if slot.state != "READY_RESERVED":
                return False
        info = self.get_node_info(candidate_mac)
        return info is not None and not info.standby_reserved

    def build_scale_down_alert(self, mac: str, reason: str = "scale_down") -> ScaleDownComputeAlert | ScaleDownDataAlert | None:
        """Build the appropriate scale-down alert from NodeInfo. Returns None if MAC not tracked.

        Reserved nodes (``standby_reserved=True``) do NOT produce ordinary
        scale-down alerts. Reserve loss is handled separately.
        """
        if mac not in self._dynamic_node_macs:
            logger.warning("[registry] mac=%s not in dynamic_node_macs — ignoring", mac)
            return None
        info = self._active.get(mac)
        if info is None:
            logger.warning("[registry] no NodeInfo for mac=%s — cannot build alert", mac)
            return None
        if info.standby_reserved:
            logger.info("[registry] mac=%s is standby_reserved — not building ordinary scale-down alert", mac)
            return None

        if info.node_type == "compute":
            return ScaleDownComputeAlert(
                lan=info.lan,
                network_id=info.network_id,
                container_name=info.name,
                mac=mac,
                ip=info.ip,
                reason=reason,
            )
        else:
            return ScaleDownDataAlert(
                lan=info.lan,
                network_id=info.network_id,
                container_name=info.name,
                mac=mac,
                ip=info.ip,
                rs_name=info.rs_name,
                primary_container=info.primary_container,
                port=info.port,
                reason=reason,
            )
