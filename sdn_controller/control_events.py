"""control_events.py — ZMQ control event dispatcher.

Dispatches drain_complete, rs_secondary_ready, and telemetry-based
VIP promotions. Fully stateless — all dependencies passed as arguments.

Thread safety: all methods are called exclusively from Thread 2.
"""

import logging
import os
import time
from typing import Callable

from .elasticity.elasticity import ElasticityManager
from .elasticity.node_common import log_ready_timing
from .node_registry import DynamicNodeRegistry
from .scaling_config import _FLOW_ISOLATION_WARMUP_S
from .telemetry.models import TelemetrySummary

logger = logging.getLogger(__name__)

# RQ3 robustness: deterministic controller-side drop of app_ready events. "off"
# (default) keeps every other run byte-identical; "all" drops every event;
# "alternate" drops every 2nd event per controller (= per LAN). Discovery runs
# ignore app_ready events anyway, so the knob only affects direct arms.
_READINESS_EVENT_DROP_MODE = os.environ.get("READINESS_EVENT_DROP_MODE", "off")


class ControlEventDispatcher:
    """Dispatches ZMQ control events and telemetry-based VIP promotions.

    Stateless apart from the RQ3 flow-isolation counters (Thread 2 only);
    all other state is read from the node registry and topology mixin.
    """

    def __init__(self) -> None:
        # RQ3 flow-isolation counters / guard state (Thread 2 only).
        self._request_complete_count: int = 0
        self._flow_guard_warned: bool = False
        self._app_ready_arrival_count: int = 0

    def process_flow_events(self, summary, vip_routing, enabled, uptime_s) -> None:
        """Handle request_complete control events (RQ3 flow isolation).

        Deletes the client's VIP_SERVER flow pair so the next request triggers
        a fresh backend-selection event. No-op unless *enabled*
        (``VIP_FLOW_ISOLATION=1``). Misconfiguration guard: warn exactly once
        if enabled and no request_complete events have arrived after
        ``FLOW_ISOLATION_WARMUP_S`` of controller uptime.
        """
        if not enabled:
            return
        for event in summary.control_events:
            if event.get("event_type") == "request_complete":
                self._request_complete_count += 1
                client_ip = event.get("client_ip")
                client_port = event.get("client_port") or 0
                if client_ip:
                    vip_routing.delete_vip_server_client_flows(
                        client_ip, client_port)
        if (self._request_complete_count == 0
                and not self._flow_guard_warned
                and uptime_s > _FLOW_ISOLATION_WARMUP_S):
            self._flow_guard_warned = True
            logger.warning(
                "[control] VIP_FLOW_ISOLATION=1 but no request_complete events "
                "received after %.0fs — check EDGE_FLOW_ISOLATION=1 on edge "
                "containers", _FLOW_ISOLATION_WARMUP_S,
            )

    def process_app_ready_events(self, summary, readiness_gate) -> None:
        """Handle app_ready control events (RQ3 v2 direct arm).

        Routes each ``app_ready`` event (``server_id`` = backend MAC) to the
        readiness gate for event-driven admission. No-op when the gate is None
        (``READINESS_PROPAGATION == "off"``); unknown/unpending MACs are
        ignored (e.g. static servers that boot before the run).

        RQ3 robustness (default off): ``READINESS_EVENT_DROP_MODE=all`` drops
        every app_ready event at the controller, and ``alternate`` drops every
        2nd event per controller. Every drop is logged with the mode and MAC.
        """
        if readiness_gate is None:
            return
        drop_mode = _READINESS_EVENT_DROP_MODE
        for event in summary.control_events:
            if event.get("event_type") != "app_ready":
                continue
            mac = event.get("server_id")
            if not mac:
                continue
            if drop_mode == "all":
                logger.warning("[control] app_ready event dropped (mode=all) mac=%s", mac)
                continue
            if drop_mode == "alternate":
                self._app_ready_arrival_count += 1
                if self._app_ready_arrival_count % 2 == 0:
                    logger.warning(
                        "[control] app_ready event dropped (mode=alternate, "
                        "ordinal=%d) mac=%s", self._app_ready_arrival_count, mac,
                    )
                    continue
            readiness_gate.admit_on_event(mac)

    def _log_storage_ready(self, info, source: str) -> None:
        if info.ready_logged or info.spawn_started_monotonic_s <= 0:
            return
        log_ready_timing(
            info.name,
            "storage",
            source,
            time.monotonic() - info.spawn_started_monotonic_s,
        )
        info.ready_logged = True

    def process_drain_events(self, summary: TelemetrySummary,
                             elasticity: ElasticityManager) -> None:
        """Handle drain_complete control events forwarded by the aggregator.

        Routes on ``PendingDrain.node_type`` via
        :meth:`ElasticityManager.submit_cleanup` so compute and Tier 1
        selective-sync drains share the same dispatch path.
        """
        for event in summary.control_events:
            if event.get("event_type") == "drain_complete":
                mac = event.get("server_id")
                if mac and elasticity.has_pending_drain(mac):
                    logger.info("[control] drain_complete received for mac=%s — submitting Phase B cleanup", mac)
                    elasticity.submit_cleanup(mac)
                elif mac:
                    logger.info("[control] drain_complete for unknown mac=%s — ignoring", mac)
                else:
                    logger.warning("[control] drain_complete missing server_id — ignoring")

    def process_secondary_events(
        self,
        summary: TelemetrySummary,
        registry: DynamicNodeRegistry,
        add_storage_mac_fn: Callable[[str, str], None],
        on_reserve_ready_fn: Callable[[str], None] | None = None,
    ) -> None:
        """Handle rs_secondary_ready control events.

        Reserved nodes (``standby_reserved=True``) become READY_RESERVED
        instead of entering VIP. The optional *on_reserve_ready_fn* callback
        is invoked so the mediator can mark the reserve slot ready.
        """
        for event in summary.control_events:
            if event.get("event_type") == "rs_secondary_ready":
                mac = event.get("server_id")
                if not mac:
                    continue
                info = registry.get_node_info(mac)
                if info is None:
                    logger.warning("[control] rs_secondary_ready for unknown mac=%s — ignoring", mac)
                    continue
                if info.node_type != "storage":
                    logger.warning("[control] rs_secondary_ready for non-storage mac=%s — ignoring", mac)
                    continue
                if info.ready_logged:
                    logger.debug("[control] rs_secondary_ready for mac=%s already processed — ignoring", mac)
                    continue
                if info.standby_reserved:
                    if on_reserve_ready_fn:
                        on_reserve_ready_fn(mac)
                    self._log_storage_ready(info, "rs_secondary_ready")
                    logger.info(
                        "[control] rs_secondary_ready for reserved mac=%s — "
                        "marked READY_RESERVED (ip=%s, name=%s)",
                        mac, info.ip, info.name,
                    )
                    continue
                add_storage_mac_fn(mac, f"n{info.lan}")
                self._log_storage_ready(info, "rs_secondary_ready")
                logger.info(
                    "[control] rs_secondary_ready received for mac=%s — "
                    "added to VIP storage pool (ip=%s, name=%s)",
                    mac, info.ip, info.name,
                )

    def promote_storage_from_telemetry(
        self,
        summary: TelemetrySummary,
        registry: DynamicNodeRegistry,
        local_storage_macs_n1: set[str],
        local_storage_macs_n2: set[str],
        add_storage_mac_fn: Callable[[str, str], None],
        on_reserve_ready_fn: Callable[[str], None] | None = None,
    ) -> None:
        """Fallback VIP promotion: detect SECONDARY from regular telemetry.

        Reserved nodes are gated — they become READY_RESERVED instead of
        entering VIP.
        """
        for mac, ss in summary.storage_servers.items():
            if ss.member_state != "SECONDARY":
                continue
            info = registry.get_node_info(mac)
            if info is None or info.node_type != "storage":
                continue
            if info.ready_logged:
                continue
            domain = f"n{info.lan}"
            already_in = (
                mac in local_storage_macs_n1 if domain == "n1"
                else mac in local_storage_macs_n2
            )
            if already_in:
                continue
            if info.standby_reserved:
                if on_reserve_ready_fn:
                    on_reserve_ready_fn(mac)
                self._log_storage_ready(info, "telemetry_secondary")
                logger.info(
                    "[control] promoting reserved storage mac=%s via telemetry fallback "
                    "— marked READY_RESERVED (ip=%s, name=%s)",
                    mac, info.ip, info.name,
                )
                continue
            add_storage_mac_fn(mac, domain)
            self._log_storage_ready(info, "telemetry_secondary")
            logger.info(
                "[control] promoting storage mac=%s via telemetry fallback "
                "(member_state=SECONDARY, ip=%s, name=%s)",
                mac, info.ip, info.name,
            )
