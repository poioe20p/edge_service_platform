import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib.packet import ethernet, ether_types, packet
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3
from os_ken import cfg

from .elasticity.elasticity import ComputeAlert, DataAlert, ElasticityManager, PrepareStandbyStorageAlert, CleanupReserveAlert
from .elasticity.node_common import NodeInfo
from .telemetry.consumption_publisher import ConsumptionPublisher
from .telemetry.models import ServerSummary, TelemetrySummary
from .telemetry.zmq_source import ZmqTelemetrySource
from .telemetry.polling_source import PollingTelemetrySource
from .telemetry.event_preserving_source import EventPreservingTelemetrySource
from .telemetry.delayed_source import DelayedEventPreservingTelemetrySource
from .telemetry.sampled_push_source import SampledPushTelemetrySource
from .topology.topology import TopologyMixin
from .vip_routing import VipRoutingMixin
from .scaling_policy import ScalingPolicy
from .policy_gate import PolicyGate
from .scaling_config import (
    _ACTION_BUDGET_PER_TIER,
    _CONTROL_TICK_S,
    _NODE_BIRTH_GRACE_S,
    _SCALE_DOWN_CANDIDATE_MAX_STALENESS_S,
    _STORAGE_PERSISTENT_RESERVE_ENABLED,
    _STORAGE_RESERVE_PENDING_WINDOWS,
    _STORAGE_POOL_RENEW_ENABLED,
    _READINESS_PROPAGATION,
    _STORAGE_PROPAGATION,
    _READINESS_PROBE_TIMEOUT_S,
    _READINESS_PROBE_MAX_S,
    _READINESS_PROBE_RETRY_S,
    _READINESS_EVENT_FALLBACK_S,
    _READINESS_WAKE_VERIFY_MODE,
    _DISCOVERY_POLL_INTERVAL_S,
    _EDGE_READY_PORT,
    _ADMISSION_LOG_PATH,
    _VIP_FLOW_ISOLATION,
    _HOUSEKEEPING_OVERLOAD_GATE,
    _HOUSEKEEPING_OVERLOAD_LOOKBACK,
    _HOUSEKEEPING_STALE_S,
)
from .node_registry import DynamicNodeRegistry
from .control_events import ControlEventDispatcher
from .readiness_gate import ReadinessGate

import requests

# os-ken resolves _REQUIRED_APP from sys.modules[cls.__module__], and topology.api's
# require_app() sets it on the topology module — so it must be declared here.
_REQUIRED_APP = ['os_ken.topology.switches']

logger = logging.getLogger('os_ken.main_n1')


@dataclass(frozen=True)
class _ComputeScaleDownCandidate:
    node: NodeInfo
    summary: ServerSummary
    staleness_s: float
    age_s: float


class KenLearnAndLog(VipRoutingMixin, TopologyMixin, app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        cfg.CONF.observe_links = True
        cfg.CONF.observe_hosts = True

        super(KenLearnAndLog, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.enable_reactive_learning = True
        self.datapaths = []
        self._datapath_by_id = {}
        self._lan_id = os.environ.get("LAN_ID", "lan1")
        self._lan_num = 1 if self._lan_id == "lan1" else 2

        # One worker per fan-out; a deferred admission is retried when the current
        # fan-out completes, never dropped. Inert while _STORAGE_POOL_RENEW_ENABLED is 0.
        self._pool_renew_lock = threading.Lock()
        self._pool_renew_inflight = False
        self._pool_renew_pending = 0

        _aggregator_endpoints = [
            ep.strip()
            for ep in os.environ.get(
                "AGGREGATOR_ENDPOINTS", "tcp://10.0.0.5:5556,tcp://10.0.1.5:5556"
            ).split(",")
            if ep.strip()
        ]
        _peer_endpoints = [
            ep.strip()
            for ep in os.environ.get("PEER_TOPOLOGY_ENDPOINTS", "").split(",")
            if ep.strip()
        ]
        logger.info("aggregator endpoints: %s", _aggregator_endpoints)
        logger.info("peer topology endpoints: %s", _peer_endpoints)

        # Thread 3 — must be created before telemetry source
        self._elasticity = ElasticityManager(topology_mixin=self)

        # Injected before start() so a stray early alert can never take the `off` path.
        if _READINESS_PROPAGATION != "off":
            self._readiness_gate = ReadinessGate(
                propagation=_READINESS_PROPAGATION,
                probe_timeout_s=_READINESS_PROBE_TIMEOUT_S,
                probe_max_s=_READINESS_PROBE_MAX_S,
                probe_retry_s=_READINESS_PROBE_RETRY_S,
                discovery_interval_s=_DISCOVERY_POLL_INTERVAL_S,
                ready_port=_EDGE_READY_PORT,
                admission_log_path=_ADMISSION_LOG_PATH,
                event_fallback_s=_READINESS_EVENT_FALLBACK_S,
                wake_verify_mode=_READINESS_WAKE_VERIFY_MODE,
                on_admit=lambda pb: self._elasticity._admit_compute_backend(
                    pb.mac, pb.ip, pb.name, pb.lan, pb.network_id,
                    pb.spawn_started_mono_s, "readiness_gate",
                ),
                on_abandon=lambda pb: self._elasticity.submit_abandon(
                    pb.mac, pb.ip, pb.name, pb.lan,
                ),
            )
            self._elasticity.readiness_gate = self._readiness_gate
            self._readiness_gate.start()
            logger.info(
                "RQ3 readiness gate active: propagation=%s probe_timeout=%.1fs "
                "probe_max=%.1fs probe_retry=%.1fs discovery=%.1fs ready_port=%d "
                "flow_isolation=%d",
                _READINESS_PROPAGATION, _READINESS_PROBE_TIMEOUT_S,
                _READINESS_PROBE_MAX_S, _READINESS_PROBE_RETRY_S,
                _DISCOVERY_POLL_INTERVAL_S, _EDGE_READY_PORT, _VIP_FLOW_ISOLATION,
            )
        else:
            self._readiness_gate = None

        self._elasticity.start()

        self._scaling_policy = ScalingPolicy()
        self._policy_gate = PolicyGate()

        self._node_registry = DynamicNodeRegistry()
        self._control_events = ControlEventDispatcher()

        # One frame per window carrying the instant the window was consumed,
        # so the collector can stamp consumed_at on the matching row. No-op
        # when COORDINATOR_STATE_PUB_PORT=0.
        self._consumption_publisher = ConsumptionPublisher()

        self._last_summary: TelemetrySummary | None = None
        self._last_summary_mono: float = 0.0

        _telemetry_source_mode = os.environ.get("TELEMETRY_SOURCE", "zmq")
        _http_endpoints = []
        for _ep in _aggregator_endpoints:
            _host_port = _ep.replace("tcp://", "")
            _host, _ = _host_port.rsplit(":", 1)
            _http_endpoints.append(f"http://{_host}:5558")

        def _forward_control_and_topology(summary):
            # Forwarding real windows here would bypass DELAY_S and skip the delivery log, so
            # only control mini-summaries (window_seq None) take the ZMQ control channel.
            if summary.window_seq is None:
                self._on_telemetry_update(summary)

        if _telemetry_source_mode == "poll":
            _poll_interval = float(os.environ.get("POLL_INTERVAL_S", "10"))
            self._telemetry = PollingTelemetrySource(
                endpoints=_http_endpoints,
                interval_s=_poll_interval,
                on_update=self._on_telemetry_update,
            )
        elif _telemetry_source_mode == "event_preserving":
            self._telemetry = EventPreservingTelemetrySource(
                endpoints=_http_endpoints,
                poll_interval_s=float(os.environ.get("EVENT_POLL_INTERVAL_S", "0.5")),
                on_update=self._on_telemetry_update,
            )
        elif _telemetry_source_mode == "delayed_event_preserving":
            self._telemetry = DelayedEventPreservingTelemetrySource(
                endpoints=_http_endpoints,
                delay_s=float(os.environ.get("DELAY_S", "30")),
                poll_interval_s=float(os.environ.get("EVENT_POLL_INTERVAL_S", "0.5")),
                on_update=self._on_telemetry_update,
            )
        elif _telemetry_source_mode == "sampled_push":
            self._telemetry = SampledPushTelemetrySource(
                endpoints=_http_endpoints,
                sample_every=int(os.environ.get("SAMPLE_EVERY", "3")),
                poll_interval_s=float(os.environ.get("EVENT_POLL_INTERVAL_S", "0.5")),
                on_update=self._on_telemetry_update,
            )
        elif _telemetry_source_mode == "zmq":
            self._telemetry = ZmqTelemetrySource(
                endpoints=_aggregator_endpoints + _peer_endpoints,
                on_update=self._on_telemetry_update,
                on_topology_update=self.on_topology_update,
            )
        else:
            logger.error(
                "unknown TELEMETRY_SOURCE=%s — falling back to poll",
                _telemetry_source_mode,
            )
            self._telemetry = PollingTelemetrySource(
                endpoints=_http_endpoints,
                interval_s=float(os.environ.get("POLL_INTERVAL_S", "10")),
                on_update=self._on_telemetry_update,
            )

        # Non-zmq delivery modes keep a ZMQ SUB channel for control events + topology:
        # those always arrive immediately, regardless of the mode under test.
        if _telemetry_source_mode != "zmq":
            self._control_zmq = ZmqTelemetrySource(
                endpoints=_aggregator_endpoints + _peer_endpoints,
                on_update=_forward_control_and_topology,
                on_topology_update=self.on_topology_update,
            )
            self._control_zmq.start()
        self._telemetry.start()

        self._last_scale_eval_seq: int | None = None
        self._recent_overload: deque[bool] = deque(maxlen=_HOUSEKEEPING_OVERLOAD_LOOKBACK)
        hub.spawn(self._housekeeping_loop)


    def _broadcast_pool_renew(self, domain: str) -> None:
        """POST /pool_renew on every edge server of this LAN after a storage admission.

        Called from ``_promote_storage_backend`` — the single choke point all
        admission paths funnel through. Non-blocking and lossless: one worker
        thread per fan-out, an in-flight flag in front of it, and a pending
        counter so N admissions during one fan-out are all attributable on
        the retry (census classes: broadcast now/deferred; failed logged).
        No-op while ``_STORAGE_POOL_RENEW_ENABLED`` is 0.
        """
        if not _STORAGE_POOL_RENEW_ENABLED:
            return
        with self._pool_renew_lock:
            if self._pool_renew_inflight:
                self._pool_renew_pending += 1
                logger.info("[pool_renew] broadcast deferred domain=%s pending=%d",
                            domain, self._pool_renew_pending)
                return
            self._pool_renew_inflight = True
        threading.Thread(target=self._pool_renew_worker, args=(domain,),
                         daemon=True, name="pool-renew-fanout").start()

    def _pool_renew_worker(self, domain: str) -> None:
        """Fan out /pool_renew once per pending admission (worker thread).

        Under os-ken's monkey-patched threading this worker is cooperative;
        it copies ``_local_server_macs`` per pass while ``_mac_to_ip`` is
        read live from the worker thread.
        """
        pending = True
        cleared = False
        try:
            while pending:
                with self._pool_renew_lock:
                    deferred = self._pool_renew_pending
                sent = 0
                for mac in list(self._local_server_macs):
                    ip = self._mac_to_ip.get(mac)
                    if not ip:
                        continue
                    url = f"http://{ip}:5000/pool_renew"
                    try:
                        requests.post(url, json={"lan": self._lan_id,
                                                 "reason": "storage_admission"}, timeout=1.5)
                        sent += 1
                    except requests.RequestException as exc:
                        logger.warning("[pool_renew] POST %s failed: %s", url, exc)
                logger.info("[pool_renew] broadcast domain=%s nodes=%d deferred=%d",
                            domain, sent, deferred)
                with self._pool_renew_lock:
                    # Consumed only after the pass completed, so a raising pass leaves the count
                    # pending and the next fan-out still reports it. Single worker => non-negative.
                    self._pool_renew_pending -= deferred
                    pending = self._pool_renew_pending > 0
                    if not pending:
                        self._pool_renew_inflight = False
                        cleared = True
        finally:
            # Exception path only (the normal path clears the flag under the lock above): a stranded
            # flag would defer every later admission. Never clear after a newer worker.
            if not cleared:
                with self._pool_renew_lock:
                    self._pool_renew_inflight = False

    def _promote_storage_backend(self, mac: str, domain: str) -> None:
        self.add_storage_mac(mac, domain)
        self.mark_storage_backend_warm(mac, domain)
        self._broadcast_pool_renew(domain)
        logger.info(
            "[vip_data] promoted storage mac=%s domain=%s",
            mac,
            domain,
        )

    def _on_reserve_ready(self, mac: str) -> None:
        """Callback invoked by ControlEventDispatcher when a reserved node reaches SECONDARY."""
        info = self._node_registry.get_node_info(mac)
        if info is None:
            return
        self._node_registry.mark_storage_reserve_ready(mac)

    def _try_prepare_storage_reserve(self, summary: TelemetrySummary, lan: int) -> None:
        """Submit reserve preparation when the slot is NONE and the primary is available."""
        if not _STORAGE_PERSISTENT_RESERVE_ENABLED:
            return
        if not self._node_registry.should_prepare_storage_reserve(lan):
            return
        if not any(ss.member_state == "PRIMARY" for ss in summary.storage_servers.values()):
            logger.debug("[reserve] no PRIMARY visible for lan=%d — skipping reserve prep", lan)
            return

        self._elasticity.submit(
            PrepareStandbyStorageAlert(
                lan=lan,
                network_id=summary.network_id,
                rs_name=f"rs_net{lan}",
                primary_container=f"edge_storage_server_n{lan}",
            )
        )
        self._node_registry.mark_storage_reserve_prepare_submitted(lan)

    def _handle_storage_reserve_trigger(self, summary: TelemetrySummary, lan: int, reason: str) -> str | None:
        """Route a same-LAN storage trigger through the reserve model.

        Returns:
          - "activated" — a READY reserve was consumed into active service
            (the caller MUST treat this as the storage action and not submit
            a separate DataAlert).
          - "waiting"   — the reserve is PREPARING/NONE and the trigger was
            latched; the caller MUST NOT submit a DataAlert (activation will
            fire automatically once the standby is READY).
          - None        — the reserve model is disabled; the caller should
            fall through to the normal Thread 3 (cold-spawn) submission.

        Truthiness is preserved, so call sites that only test
        ``if self._handle_storage_reserve_trigger(...)`` keep working
        unchanged.
        """
        if not _STORAGE_PERSISTENT_RESERVE_ENABLED:
            return None

        slot = self._node_registry.get_storage_reserve_slot(lan)

        if slot.state == "READY_RESERVED":
            info = self._node_registry.consume_ready_storage_reserve(lan)
            if info is None:
                logger.warning("[reserve] READY_RESERVED but consume returned None for lan=%d", lan)
                return None
            self._promote_storage_backend(info.mac, f"n{lan}")
            info.standby_reserved = False
            self._scaling_policy.record_storage_activation()
            self._log_decision("reserve_activate", f"storage_lan{lan}", summary.window_id)
            logger.info("[reserve] activated lan=%d name=%s ip=%s mac=%s reason=%s",
                        lan, info.name, info.ip, info.mac, reason)
            self._try_prepare_storage_reserve(summary, lan)
            return "activated"

        self._node_registry.latch_storage_reserve_activation(lan, reason, _STORAGE_RESERVE_PENDING_WINDOWS)
        self._try_prepare_storage_reserve(summary, lan)
        return "waiting"

    def _pick_compute_scale_down_candidate(self) -> NodeInfo | None:
        now_wall = time.time()
        now_mono = time.monotonic()
        eligible: list[_ComputeScaleDownCandidate] = []

        for node in self._node_registry.list_dynamic("compute"):
            if self._elasticity.has_pending_drain(node.mac):
                logger.debug(
                    "[scale-down] compute candidate skip name=%s mac=%s reason=pending_drain",
                    node.name,
                    node.mac,
                )
                continue

            server = self._server_stats.get(node.mac)
            if server is None:
                logger.debug(
                    "[scale-down] compute candidate skip name=%s mac=%s reason=no_cached_server_summary",
                    node.name,
                    node.mac,
                )
                continue

            staleness_s = max(0.0, now_wall - server.last_report_ts)
            if staleness_s > _SCALE_DOWN_CANDIDATE_MAX_STALENESS_S:
                logger.debug(
                    "[scale-down] compute candidate skip name=%s mac=%s reason=stale staleness=%.1fs",
                    node.name,
                    node.mac,
                    staleness_s,
                )
                continue

            if server.state != "active":
                logger.debug(
                    "[scale-down] compute candidate skip name=%s mac=%s reason=state state=%s",
                    node.name,
                    node.mac,
                    server.state,
                )
                continue

            age_s = self._node_registry.node_age_s(node.mac, now_mono)
            if age_s < _NODE_BIRTH_GRACE_S:
                logger.debug(
                    "[scale-down] compute candidate skip name=%s mac=%s reason=too_young age=%.1fs",
                    node.name,
                    node.mac,
                    age_s,
                )
                continue

            eligible.append(
                _ComputeScaleDownCandidate(
                    node=node,
                    summary=server,
                    staleness_s=staleness_s,
                    age_s=age_s,
                )
            )

        if not eligible:
            logger.info(
                "[scale-down] compute underutilisation but no graceful candidate is eligible"
            )
            return None

        eligible.sort(key=lambda item: (
            item.summary.request_count,
            item.summary.avg_cpu_percent,
            item.summary.avg_time_proc_ms,
            -item.summary.last_report_ts,
        ))

        chosen = eligible[0]
        logger.info(
            "[scale-down] compute candidate selected name=%s mac=%s req=%d cpu=%.2f proc=%.2f stale=%.1fs age=%.1fs",
            chosen.node.name,
            chosen.node.mac,
            chosen.summary.request_count,
            chosen.summary.avg_cpu_percent,
            chosen.summary.avg_time_proc_ms,
            chosen.staleness_s,
            chosen.age_s,
        )
        return chosen.node

    def _on_telemetry_update(self, summary: TelemetrySummary) -> None:
        """Thread 2 callback — thin mediator that orchestrates composed components."""
        consumed_at = time.time()
        if summary.network_id != self._lan_id:
            # Peer-LAN stats must still merge into the WSM pools: without them cross-LAN
            # candidates read 0/0, win every WSM cost, and force cross-LAN routing.
            logger.debug("ignoring telemetry for %s (this controller owns %s)",
                         summary.network_id, self._lan_id)
            self.update_server_stats(summary.servers)
            self.update_storage_stats(summary.storage_servers)
            return
        # Only real windows (window_seq set) update the ticker's latest state;
        # control mini-summaries (window_seq None) never do.
        if summary.window_seq is not None:
            self._last_summary = summary
            self._last_summary_mono = time.monotonic()
            if summary.request_evidence:
                self._recent_overload.append(summary.overload)
            self._node_registry.refresh_presence(summary)

        self._node_registry.sync(self._elasticity)

        self._control_events.process_drain_events(summary, self._elasticity)
        # RQ3 v3 storage propagation: 'discovery' suppresses the
        # rs_secondary_ready event path (telemetry-only promotion).
        if _STORAGE_PROPAGATION != "discovery":
            self._control_events.process_secondary_events(
                summary, self._node_registry, self._promote_storage_backend,
                on_reserve_ready_fn=self._on_reserve_ready,
            )
        # Runs BEFORE the mini-summary early-return below: request_complete arrives on
        # control mini-summaries, whose server dicts are empty.
        self._control_events.process_flow_events(
            summary, self, _VIP_FLOW_ISOLATION == 1, time.monotonic())
        self._control_events.process_app_ready_events(
            summary, self._readiness_gate)

        # Mini-summaries (control event pass-throughs) have empty server dicts.
        if not summary.servers and not summary.storage_servers:
            return

        if summary.domain_summary is None:
            logger.warning("non-mini summary with domain_summary=None — skipping scaling")
            return

        self._log_and_update_stats(summary)
        self._consumption_publisher.publish(
            summary.network_id,
            summary.window_end,
            consumed_at=consumed_at,
        )

        # RQ3 v3 storage propagation: 'direct' suppresses the telemetry SECONDARY
        # fallback (event-only promotion).
        if _STORAGE_PROPAGATION != "direct":
            self._control_events.promote_storage_from_telemetry(
                summary, self._node_registry,
                self._local_storage_macs_n1, self._local_storage_macs_n2,
                self._promote_storage_backend,
                on_reserve_ready_fn=self._on_reserve_ready,
            )

        try:
            lan = int(summary.network_id.replace("lan", ""))
        except ValueError:
            logger.warning("could not parse LAN from network_id=%s", summary.network_id)
            return

        # Thread 3 → Thread 2 outcome; per-LAN drain: only this controller's own failures.
        for _ in self._elasticity.drain_reserve_prepare_failures(lan):
            self._node_registry.mark_storage_reserve_prepare_failed(lan)
            logger.info("[reserve] replenish_next_cycle lan=%d after prepare failure", lan)

        self._try_prepare_storage_reserve(summary, lan)
        slot = self._node_registry.get_storage_reserve_slot(lan)

        if slot.activation_pending and slot.state != "READY_RESERVED":
            expired = self._node_registry.tick_storage_reserve_pending_activation(lan)
            if expired:
                self._node_registry.clear_storage_reserve_pending_activation(lan)
                logger.info("[reserve] pending_expired lan=%d — clearing activation intent", lan)

        if slot.state == "READY_RESERVED" and slot.activation_pending:
            self._handle_storage_reserve_trigger(summary, lan, slot.pending_reason or "pending")

        # Absent-node detection and scale-down evaluation live in the housekeeping
        # ticker (_run_housekeeping, gated by is_busy() and cooldowns).

        ds = summary.domain_summary

        dynamic_storage_count = self._node_registry.count_dynamic("storage", lan)
        registry_dynamic_compute_count = self._node_registry.count_dynamic("compute", lan)
        pending_compute_drain_count = self._elasticity.pending_compute_drain_count(
            exclude_reason="absent")
        effective_dynamic_compute_count = max(
            0,
            registry_dynamic_compute_count - pending_compute_drain_count,
        )
        peer_network_id = "lan2" if summary.network_id == "lan1" else "lan1"
        peer_summary = self._telemetry.get_latest(peer_network_id)
        peer_ds = peer_summary.domain_summary if peer_summary and peer_summary.domain_summary else None

        compute_blocked = self._elasticity.blocks_compute_scale_up()
        storage_blocked = self._elasticity.blocks_storage_scale_up()

        if self._elasticity.has_active_operation():
            logger.debug("[scale-up] elasticity manager is busy — skipping")
        else:
            if compute_blocked:
                logger.debug("[scale-up] compute blocked by active elasticity operation — skipping")
            if storage_blocked:
                logger.debug("[scale-up] storage blocked by pending storage drain — skipping")

            if self._policy_gate.mode == "dual":
                for alert in self._scaling_policy.evaluate_scale_up(
                    ds,
                    lan,
                    summary.network_id,
                    dynamic_storage_count,
                    effective_dynamic_compute_count,
                    peer_ds,
                    allow_compute=not compute_blocked,
                    allow_storage=not storage_blocked,
                ):
                    if isinstance(alert, DataAlert):
                        if self._handle_storage_reserve_trigger(summary, alert.lan, "load"):
                            continue

                    self._elasticity.submit(alert)
                    self._log_decision("scale_up", type(alert).__name__, summary.window_id)
                    if (isinstance(alert, ComputeAlert)
                            and self._elasticity.has_pending_compute_drain()):
                        logger.info(
                            "[scale-up] compute triggered with %d pending compute drain(s) — submitting lower-priority cancel",
                            pending_compute_drain_count,
                        )
                        self._elasticity.submit_cancel_compute_drain()
                        self._log_decision("cancel", "compute_drain", summary.window_id)
            else:
                # `ds` is not None is guaranteed upstream; guard kept as defence-in-depth.
                if ds is not None:
                    compute_v = self._scaling_policy.evaluate_compute_scale_up(
                        ds, effective_dynamic_compute_count, peer_ds,
                        blocked=compute_blocked)
                    storage_v = self._scaling_policy.evaluate_storage_scale_up(
                        ds, dynamic_storage_count, blocked=storage_blocked)

                    selected = list(self._policy_gate.select(compute_v, storage_v))
                    selected = [t for t in selected
                                if self._policy_gate.budget_available(t)]

                    bottleneck_class = (self._policy_gate.classify(compute_v, storage_v)
                                        if (compute_v.eligible or storage_v.eligible)
                                        else "n/a")

                    for tier in selected:
                        if tier == "compute":
                            self._scaling_policy.commit_compute_scale_up()
                            self._policy_gate.consume_budget("compute")
                            self._elasticity.submit(ComputeAlert(
                                lan=lan, network_id=summary.network_id))
                            if self._elasticity.has_pending_compute_drain():
                                # Keeps RQ1's exact row so the RQ2 decision log is a superset of RQ1's.
                                self._elasticity.submit_cancel_compute_drain()
                                self._log_decision("cancel", "compute_drain", summary.window_id)
                        elif tier == "storage":
                            # Never cold-spawn while the reserve is enabled: activate the READY standby fast, or
                            # latch and wait while PREPARING. An activation consumes the tier budget.
                            reserve_result = self._handle_storage_reserve_trigger(
                                summary, lan, "load")
                            if reserve_result is not None:
                                if reserve_result == "activated":
                                    self._policy_gate.consume_budget("storage")
                                continue
                            self._scaling_policy.commit_storage_scale_up()
                            self._policy_gate.consume_budget("storage")
                            self._elasticity.submit(DataAlert(
                                lan=lan, network_id=summary.network_id,
                                rs_name=f"rs_net{lan}",
                                primary_container=f"edge_storage_server_n{lan}"))

                    action_str = ("ComputeAlert" if selected == ["compute"]
                                  else "DataAlert" if selected == ["storage"]
                                  else "none")
                    selected_str = selected[0] if selected else "none"
                    reason = ("action" if selected
                              else "budget_exhausted"
                              if any((compute_v.fired and not self._policy_gate.budget_available("compute"),
                                      storage_v.fired and not self._policy_gate.budget_available("storage")))
                              else "none")

                    self._log_decision(
                        "scale_up",
                        action_str,
                        window_id=summary.window_id,
                        compute_score_norm=compute_v.score_norm,
                        storage_score_norm=storage_v.score_norm,
                        compute_threshold=compute_v.threshold,
                        storage_threshold=storage_v.threshold,
                        compute_fired=1 if compute_v.fired else 0,
                        storage_fired=1 if storage_v.fired else 0,
                        compute_eligible=1 if compute_v.eligible else 0,
                        storage_eligible=1 if storage_v.eligible else 0,
                        bottleneck_class=bottleneck_class,
                        selected_action=selected_str,
                        rejected_action=("" if reason == "budget_exhausted"
                                         else "storage" if selected == ["compute"] and storage_v.fired
                                         else "compute" if selected == ["storage"] and compute_v.fired
                                         else "storage" if not selected and storage_v.fired and not compute_v.fired
                                         else "compute" if not selected and compute_v.fired and not storage_v.fired
                                         else ""),
                        compute_budget_used=self._policy_gate.budget_used("compute"),
                        storage_budget_used=self._policy_gate.budget_used("storage"),
                        budget_cap=_ACTION_BUDGET_PER_TIER,
                        reason=reason,
                    )


    def _housekeeping_loop(self) -> None:
        """Fixed-clock periodic loop for time-based housekeeping.

        Runs on the same eventlet hub as the telemetry/delivery loops. The body
        must never block or yield (see _run_housekeeping) so it stays atomic
        between greenthread yield points.
        """
        while True:
            hub.sleep(_CONTROL_TICK_S)
            self._run_housekeeping()

    def _run_housekeeping(self) -> None:
        """Absent-node detection + scale-down evaluation on the fixed clock.

        Design-B semantics: the cadence is the fixed ``CONTROL_TICK_S`` clock,
        identical across ALL delivery modes (including zmq). Scale-down evaluates
        the latest delivered state, deduped per ``window_seq`` (at most once per
        window, so the sliding-window sample count is never inflated). Windows
        delivered between ticks are intentionally not individually evaluated —
        scale-down is a time-based check of current state; with CONTROL_TICK_S =
        WINDOW_S the steady-state cadence is one consideration per window.

        Concurrency: performs no blocking I/O or sleeps. The ElasticityManager
        calls here are the same ones the delivery callback already makes, so no
        additional yield sources are introduced beyond the existing path.
        """
        try:
            s = self._last_summary
            if s is None or s.window_seq is None:
                return
            if time.monotonic() - self._last_summary_mono > _HOUSEKEEPING_STALE_S:
                logger.debug(
                    "[housekeeping] no fresh window for %.0fs — skipping",
                    time.monotonic() - self._last_summary_mono,
                )
                return

            # Churn guard (hysteresis): the overload label flickers on lull windows (sparse
            # telemetry); current-window-only checks gutted the fleet (G2 calib4/calib6/rate20).
            if _HOUSEKEEPING_OVERLOAD_GATE and (s.overload or any(self._recent_overload)):
                logger.debug(
                    "[housekeeping] LAN overloaded (recent) — suppressing absent-cleanup + scale-down (churn guard)")
                return

            for mac in self._node_registry.detect_absent(s):
                if self._elasticity.has_pending_drain(mac):
                    logger.info("[scale-down] pending drain for mac=%s — submitting Phase B cleanup", mac)
                    self._elasticity.submit_cleanup(mac)
                    self._log_decision("scale_down", "absent_cleanup", s.window_id)
                else:
                    info = self._node_registry.get_node_info(mac)
                    if info and info.standby_reserved:
                        self._node_registry.mark_storage_reserve_lost(mac)
                        self._node_registry.unregister_reserved_node(mac)
                        self._elasticity.submit_cleanup_reserve(
                            CleanupReserveAlert(
                                lan=info.lan,
                                mac=info.mac,
                                container_name=info.name,
                                ip=info.ip or "",
                                rs_name=info.rs_name or "",
                                primary_container=info.primary_container or "",
                                port=info.port or 27018,
                            )
                        )
                        self._log_decision("scale_down", "reserve_loss", s.window_id)
                        logger.info("[reserve] cleanup_submitted lan=%d mac=%s", info.lan, info.mac)
                        continue
                    alert = self._node_registry.build_scale_down_alert(mac, reason="absent")
                    if alert:
                        logger.info("[scale-down] submitting alert: %s", alert)
                        self._elasticity.submit(alert)
                        self._log_decision("scale_down", "absent", s.window_id)

            if s.window_seq == self._last_scale_eval_seq:
                return
            self._last_scale_eval_seq = s.window_seq
            ds = s.domain_summary
            if ds is None or not s.request_evidence:
                # Zeroed domain summaries (no HTTP samples in this window)
                # must not read as underutilisation.
                return

            if self._elasticity.is_busy():
                logger.debug("[scale-down] elasticity manager is busy — skipping scaling evaluation")
                return

            remaining = self._scaling_policy.compute_cooldown_remaining()
            if remaining > 0:
                logger.debug("[scale-down] compute within %.0fs cooldown — skipping", remaining)
            else:
                if self._scaling_policy.evaluate_scale_down_compute(ds):
                    node = self._pick_compute_scale_down_candidate()
                    if node:
                        logger.info(
                            "[scale-down] compute underutilisation — removing %s", node.name)
                        alert = self._node_registry.build_scale_down_alert(node.mac)
                        if alert:
                            self._elasticity.submit(alert)
                            self._log_decision("scale_down", "compute", s.window_id)
                    else:
                        logger.info(
                            "[scale-down] compute underutilisation but no graceful candidate is eligible — clearing current window"
                        )
                    self._scaling_policy.clear_scale_down_compute_window()

            remaining = self._scaling_policy.storage_cooldown_remaining()
            if remaining > 0:
                logger.debug("[scale-down] storage within %.0fs cooldown — skipping", remaining)
            elif self._scaling_policy.evaluate_scale_down_storage(ds):
                node = self._node_registry.find_last_dynamic("storage")
                if node is None:
                    logger.info("[scale-down] storage underutilisation but no graceful candidate is eligible — clearing current window")
                    self._scaling_policy.clear_scale_down_storage_window()
                elif not self._node_registry.can_scale_down_storage(node.mac, self._lan_num):
                    logger.info(
                        "[scale-down] storage underutilisation but reserve floor blocks removal of %s",
                        node.name,
                    )
                    self._scaling_policy.clear_scale_down_storage_window()
                else:
                    logger.info(
                        "[scale-down] storage underutilisation — removing %s", node.name)
                    alert = self._node_registry.build_scale_down_alert(node.mac)
                    if alert:
                        self._elasticity.submit(alert)
                        self._log_decision("scale_down", "storage", s.window_id)
                    self._scaling_policy.clear_scale_down_storage_window()
        except Exception:
            logger.exception("[housekeeping] tick failed — continuing")

    def _log_decision(self, action_type: str, action: str,
                      window_id: str | None = None, *,
                      compute_score_norm: float | str = "",
                      storage_score_norm: float | str = "",
                      compute_threshold: float | str = "",
                      storage_threshold: float | str = "",
                      compute_fired: int | str = "",
                      storage_fired: int | str = "",
                      compute_eligible: int | str = "",
                      storage_eligible: int | str = "",
                      bottleneck_class: str = "",
                      selected_action: str = "",
                      rejected_action: str = "",
                      compute_budget_used: int | str = "",
                      storage_budget_used: int | str = "",
                      budget_cap: int | str = "",
                      reason: str = "") -> None:
        """Append one structured capacity-action decision row (RQ2 CSV contract).

        Format depends on the scaling mode (see rq2_preparation.md §2.4):
        - dual (pre-RQ2): RQ1's exact format — header
          ts,network_id,window_id,action_type,action + 5-column rows
          (byte-identical to pre-RQ2).
        - RQ2 arms: full 20-column format with evidence + decision columns.
        Header written on first use. No blocking/yielding calls — safe from the
        housekeeping greenthread.
        """
        path = os.environ.get("DECISION_LOG_PATH", "/tmp/decision_log.csv")
        rq2 = self._policy_gate.mode != "dual"
        try:
            write_header = (not os.path.exists(path)) or os.path.getsize(path) == 0
            with open(path, "a") as fh:
                if write_header:
                    if rq2:
                        fh.write(
                            "ts,network_id,window_id,action_type,action,"
                            "compute_score_norm,storage_score_norm,compute_threshold,"
                            "storage_threshold,compute_fired,storage_fired,"
                            "compute_eligible,storage_eligible,bottleneck_class,"
                            "selected_action,rejected_action,compute_budget_used,"
                            "storage_budget_used,budget_cap,reason\n"
                        )
                    else:
                        fh.write("ts,network_id,window_id,action_type,action\n")
                if rq2:
                    fh.write(
                        f"{time.time():.3f},{self._lan_id},{window_id or ''},{action_type},{action},"
                        f"{compute_score_norm},{storage_score_norm},{compute_threshold},"
                        f"{storage_threshold},{compute_fired},{storage_fired},"
                        f"{compute_eligible},{storage_eligible},{bottleneck_class},"
                        f"{selected_action},{rejected_action},{compute_budget_used},"
                        f"{storage_budget_used},{budget_cap},{reason}\n"
                    )
                else:
                    fh.write(
                        f"{time.time():.3f},{self._lan_id},{window_id or ''},{action_type},{action}\n"
                    )
        except OSError as exc:
            logger.warning("decision log write failed: %s", exc)

    def _log_and_update_stats(self, summary: TelemetrySummary) -> None:
        """Print domain summary metrics and push per-server stats to Thread 1."""
        ds = summary.domain_summary
        print(
            f"[telemetry] network={summary.network_id} "
            f"proc_ms={ds.avg_time_proc_ms:.1f} "
            f"db_ms={ds.avg_time_db_ms:.1f} "
            f"requests={ds.total_requests} "
            f"cpu={ds.average_cpu_percent:.1f}%"
        )
        self.update_server_stats(summary.servers)
        self.update_storage_stats(summary.storage_servers)


    def _install_flow(self, datapath, priority, match, actions, *,
                      idle_timeout=0, hard_timeout=0, cookie=0, flags=None):
        ofproto = datapath.ofproto
        if flags is None:
            flags = ofproto.OFPFF_SEND_FLOW_REM

        instructions = [
            datapath.ofproto_parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions,
            )
        ]
        mod = datapath.ofproto_parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            cookie=cookie,
            flags=flags,
            command=ofproto.OFPFC_ADD,
        )
        datapath.send_msg(mod)


    def add_flow(self, datapath, in_port, dst, src, actions):
        """Default reactive learning-switch rule installer."""
        parser = datapath.ofproto_parser
        match = parser.OFPMatch(
            in_port=in_port,
            eth_dst=dst,
            eth_src=src,
        )

        logger.debug("reactive flow: dpid=%s in_port=%s src=%s dst=%s", datapath.id, in_port, src, dst)
        self._install_flow(
            datapath,
            priority=10,
            match=match,
            actions=actions,
            flags=datapath.ofproto.OFPFF_SEND_FLOW_REM,
        )


    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, event):
        """Install the table-miss flow entry when the switch connects. 
        At first switch is configured to flood all packets in order to learn MAC addresses."""

        datapath = event.msg.datapath

        ofproto = datapath.ofproto

        parser = datapath.ofproto_parser

        # Register datapath early so proactive VIP rules can find the edge switch.
        self._datapath_by_id[datapath.id] = (datapath, datapath.id)

        if not any(getattr(dp, "id", None) == datapath.id for dp in self.datapaths):
            self.datapaths.append(datapath)

        match = parser.OFPMatch()

        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]

        instructions = [
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]
        mod = datapath.ofproto_parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            match=match,
            instructions=instructions,
            flags=ofproto.OFPFF_SEND_FLOW_REM
        )
        datapath.send_msg(mod)

        logger.info("switch connected: dpid=%s, table-miss flow installed", datapath.id)


    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, event):
        """Learn MAC-port mappings, log the event, and forward the packet."""

        msg = event.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        dst = eth.dst
        src = eth.src
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # VIP interception — runs before L2 learning so VIP-destined packets
        # are not forwarded by L2 rules before DNAT is installed.
        self.snoop_arp(pkt)
        if self.handle_vip_packet_in(datapath, in_port, pkt, eth):
            return

        dpid_int = int(datapath.id)  # int dpid — mac_to_port is keyed by datapath
        self.mac_to_port.setdefault(dpid_int, {})

        # Always update: a container replaced on a different OVS port keeps its MAC
        # (elasticity retries).
        if self.mac_to_port[dpid_int].get(src) != in_port:
            logger.debug("MAC learned/updated: dpid=%s src=%s -> port=%s", dpid_int, src, in_port)
        self.mac_to_port[dpid_int][src] = in_port

        if dst in self.mac_to_port[dpid_int]:
            out_port = self.mac_to_port[dpid_int][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        if self.enable_reactive_learning and out_port != ofproto.OFPP_FLOOD:
            self.add_flow(datapath, in_port, dst, src, actions)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )

        datapath.send_msg(out)

        logger.debug(
            "packet_in: dpid=%s src=%s dst=%s in_port=%s out_port=%s",
            datapath.id, src, dst, in_port, out_port
        )
