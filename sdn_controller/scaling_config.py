"""scaling_config.py — environment-based scaling constants.

Shared by ScalingPolicy, DynamicNodeRegistry, and the mediator.
"""

import logging
import os

logger = logging.getLogger("os_ken.scaling_config")

# Scale-up: weighted degradation score.
#  score = W_CPU * max(0, cpu - FLOOR) / SPAN  +  W_T * max(0, lat - FLOOR) / SPAN
# W_T is _W_T_PROC (compute) or _W_T_DB (storage) depending on the tier.
# Score ≥ THRESHOLD for at least REQUIRED of the last WINDOW_SIZE windows triggers scale-up.

# Storage score weights (CPU-dominant — scaling fixes CPU, not T_db). Floors/spans
# are calibrated for container-level CPU readings (typical edge range 0–6 %) and t_db.
_W_STORAGE_CPU     = float(os.environ.get("SCALEUP_W_STORAGE_CPU",     "0.7"))
_W_T_DB            = float(os.environ.get("SCALEUP_W_T_DB",            "0.3"))
_STORAGE_CPU_FLOOR = float(os.environ.get("SCALEUP_STORAGE_CPU_FLOOR", "5"))
_STORAGE_CPU_SPAN  = float(os.environ.get("SCALEUP_STORAGE_CPU_SPAN",  "10"))
_T_DB_FLOOR        = float(os.environ.get("SCALEUP_T_DB_FLOOR",        "150"))
_T_DB_SPAN         = float(os.environ.get("SCALEUP_T_DB_SPAN",         "600"))

_W_CPU        = float(os.environ.get("SCALEUP_W_CPU",      "0.40"))
_W_T_PROC     = float(os.environ.get("SCALEUP_W_T_PROC",   "0.60"))
_CPU_FLOOR    = float(os.environ.get("SCALEUP_CPU_FLOOR",  "5"))
_CPU_SPAN     = float(os.environ.get("SCALEUP_CPU_SPAN",   "10"))
_T_PROC_FLOOR = float(os.environ.get("SCALEUP_T_PROC_FLOOR", "20"))
_T_PROC_SPAN  = float(os.environ.get("SCALEUP_T_PROC_SPAN",  "80"))

_SCALE_UP_WINDOW_SIZE = int(os.environ.get("SCALEUP_WINDOW_SIZE", "5"))
_SCALE_UP_REQUIRED    = int(os.environ.get("SCALEUP_REQUIRED",    "3"))

_SCALEUP_COMPUTE_BASE_THRESHOLD = float(
	os.environ.get("SCALEUP_COMPUTE_BASE_THRESHOLD", "0.45")
)
_SCALEUP_COMPUTE_THRESHOLD_INCREMENT = float(
	os.environ.get("SCALEUP_COMPUTE_THRESHOLD_INCREMENT", "0.10")
)
_SCALEUP_COMPUTE_MAX_THRESHOLD = float(
	os.environ.get("SCALEUP_COMPUTE_MAX_THRESHOLD", "0.85")
)
_SCALEUP_COMPUTE_COOLDOWN_S = float(
	os.environ.get("SCALEUP_COMPUTE_COOLDOWN_S", "45")
)
_SCALEUP_COMPUTE_PEER_RELIEF = float(
	os.environ.get("SCALEUP_COMPUTE_PEER_RELIEF", "0.03")
)
_SCALEUP_COMPUTE_PEER_HEALTH_THRESHOLD = float(
	os.environ.get("SCALEUP_COMPUTE_PEER_HEALTH_THRESHOLD", "0.35")
)

# Adaptive storage scale-up threshold (diminishing increment per dynamic node)
_SCALEUP_STORAGE_BASE_THRESHOLD      = float(os.environ.get("SCALEUP_STORAGE_BASE_THRESHOLD",      "0.25"))
_SCALEUP_STORAGE_THRESHOLD_INCREMENT = float(os.environ.get("SCALEUP_STORAGE_THRESHOLD_INCREMENT",  "0.10"))
_SCALEUP_STORAGE_MIN_INCREMENT       = float(os.environ.get("SCALEUP_STORAGE_MIN_INCREMENT",        "0.05"))
_SCALEUP_STORAGE_MAX_THRESHOLD       = float(os.environ.get("SCALEUP_STORAGE_MAX_THRESHOLD",        "0.55"))
_SCALEUP_STORAGE_WINDOW_SIZE         = int(os.environ.get("SCALEUP_STORAGE_WINDOW_SIZE",             "5"))
_SCALEUP_STORAGE_REQUIRED            = int(os.environ.get("SCALEUP_STORAGE_REQUIRED",                "2"))

_MAX_DYNAMIC_STORAGE = int(os.environ.get("MAX_DYNAMIC_STORAGE", "5"))
_MAX_DYNAMIC_COMPUTE = int(os.environ.get("MAX_DYNAMIC_COMPUTE", "4"))

# Which per-window latency statistic feeds the decision signals (scale-up
# score and scale-down below/ceiling checks). Latency is right-skewed with
# unbounded outliers, so "median" is robust while "mean" can be dominated by a
# single slow request in a low-volume window — see
# docs/operation/testing/experiment/v2/mean_vs_median_signal_finding.md.
#   "mean"   → avg_time_* (legacy; default — keeps RQ1 byte-identical).
#   "median" → median_time_* (control group / RQ2+, robust). Falls back to the
#              mean when the aggregator did not publish a median.
_LATENCY_SIGNAL_MODE = os.environ.get("LATENCY_SIGNAL_MODE", "mean").strip().lower()

# CPU-aware storage scale-down gate (control group / RQ2+ composite signal).
# When enabled, storage scale-down additionally requires the storage CPU to be
# idle (below _TAU_STORAGE_CPU_DOWN), protecting the plateau from teardown when
# the median latency alone is low (the median's plateau-vs-demand_drop range is
# only ~1.4 ms in the control workload — see
# docs/operation/testing/experiment/v2/mean_vs_median_signal_finding.md §6).
# Default OFF → pure latency scale-down, RQ1 byte-identical.
_STORAGE_SCALE_DOWN_CPU_AWARE = os.environ.get(
    "STORAGE_SCALE_DOWN_CPU_AWARE", "0"
).strip().lower() in ("1", "true", "yes")

# Scale-down thresholds — mirrored against the scale-up *_FLOOR values so
# scale-up and scale-down cannot disagree about a window's health (a window
# below the floor is healthy by construction). Calibrated for container-level
# CPU readings.
_TAU_CPU_DOWN              = float(os.environ.get("TAU_CPU_DOWN",              "15"))
_TAU_PROC_DOWN_MS          = float(os.environ.get("TAU_PROC_DOWN_MS",          "20"))
_TAU_STORAGE_CPU_DOWN      = float(os.environ.get("TAU_STORAGE_CPU_DOWN",      "15"))
_TAU_DB_DOWN_MS            = float(os.environ.get("TAU_DB_DOWN_MS",            "150"))
_TELEMETRY_TIMEOUT_WINDOWS = int(os.environ.get("TELEMETRY_TIMEOUT_WINDOWS",   "18"))

# Timeout ceiling — indeterminate windows (neither increment nor reset)
_SCALE_DOWN_PROC_TIMEOUT_CEILING_MS = float(os.environ.get("SCALE_DOWN_PROC_TIMEOUT_CEILING_MS", "5000"))
_SCALE_DOWN_DB_TIMEOUT_CEILING_MS   = float(os.environ.get("SCALE_DOWN_DB_TIMEOUT_CEILING_MS",   "5000"))

_SCALE_DOWN_COMPUTE_WINDOW_SIZE = int(os.environ.get("SCALE_DOWN_COMPUTE_WINDOW_SIZE", "12"))
_SCALE_DOWN_COMPUTE_REQUIRED    = int(os.environ.get("SCALE_DOWN_COMPUTE_REQUIRED",     "7"))
_SCALE_DOWN_STORAGE_WINDOW_SIZE = int(os.environ.get("SCALE_DOWN_STORAGE_WINDOW_SIZE", "12"))
_SCALE_DOWN_STORAGE_REQUIRED    = int(os.environ.get("SCALE_DOWN_STORAGE_REQUIRED",     "7"))
_SCALE_DOWN_CANDIDATE_MAX_STALENESS_S = float(
	os.environ.get("SCALE_DOWN_CANDIDATE_MAX_STALENESS_S", "90")
)

# Cooldowns — suppress evaluation for a grace period after scale-up
_SCALEDOWN_STORAGE_COOLDOWN_S = float(os.environ.get("SCALEDOWN_STORAGE_COOLDOWN_S", "120"))
_SCALEDOWN_COMPUTE_COOLDOWN_S = float(os.environ.get("SCALEDOWN_COMPUTE_COOLDOWN_S",  "40"))
_SCALEUP_STORAGE_COOLDOWN_S   = float(os.environ.get("SCALEUP_STORAGE_COOLDOWN_S",   "120"))

# Birth grace — skip absent-node detection for newly spawned nodes
_NODE_BIRTH_GRACE_S = float(os.environ.get("NODE_BIRTH_GRACE_S", "60"))

# Time-based housekeeping (absent-node detection, scale-down evaluation)
# runs on this fixed internal ticker instead of on telemetry arrival.
_CONTROL_TICK_S = float(os.environ.get("CONTROL_TICK_S", "10"))
# Time-based absence timeout — comfortably above the longest heartbeat so
# idle-but-alive nodes are not falsely flagged absent. Scales with the
# housekeeping clock (semantic change from per-window counts).
# research_q3: TELEMETRY_TIMEOUT_S lets a campaign raise the timeout when a
# low-rate phase starves local dynamic nodes of traffic (cross-region burst)
# so their telemetry goes stale and absent-cleanup eats them before the
# release mechanism acts.
_TELEMETRY_TIMEOUT_S = float(
    os.environ.get("TELEMETRY_TIMEOUT_S")
    or max(
        _TELEMETRY_TIMEOUT_WINDOWS * _CONTROL_TICK_S,
        3.0 * float(os.environ.get("HEARTBEAT_INTERVAL_S", "60")),
    )
)

# Churn guard (G2 calib4/calib6 finding). During an overload episode the LAN must NOT shed capacity: absent-node
# cleanup and scale-down both remove LIVE nodes when telemetry presence is
# sparse (bursty completions under load), which reconfigures the replica set
# and stalls in-flight DB ops — the self-amplifying collapse seen at open-loop
# rate 3.0 / 70% DB mix (pool 12 did NOT fix it; the churn did the damage).
# Default ON (correct behavior for all RQs); set HOUSEKEEPING_OVERLOAD_GATE=0
# to reproduce the pre-fix behavior.
_HOUSEKEEPING_OVERLOAD_GATE = os.environ.get(
    "HOUSEKEEPING_OVERLOAD_GATE", "1"
).strip().lower() in ("1", "true", "yes")
# Hysteresis lookback: the producer overload label (D3) flickers on lull
# windows under sparse telemetry, so a current-window-only gate lets
# absent-cleanup/scale-down fire mid-episode. Suppress while ANY of the last N
# delivered windows was overloaded.
_HOUSEKEEPING_OVERLOAD_LOOKBACK = int(os.environ.get(
    "HOUSEKEEPING_OVERLOAD_LOOKBACK", "5"))

# Housekeeping freshness bound: judgments need a window delivered recently
# (empty windows still refresh delivery). With no delivery for this long the
# controller cannot distinguish a dead node from a dead telemetry path, so
# it must not act. At least twice the longest delivery period past the last
# delivery — sampled push delivers every 3 x WINDOW_S — plus one tick of
# slack. Defaults (CONTROL_TICK_S=10, WINDOW_S=10): 70 s. WINDOW_S is the
# aggregator's window knob (absent from the controller env by default): when
# a deployment changes it, set WINDOW_S here too or set
# HOUSEKEEPING_STALE_S explicitly.
_HOUSEKEEPING_STALE_S = float(os.environ.get(
    "HOUSEKEEPING_STALE_S",
    str(max(6.0 * _CONTROL_TICK_S,
            6.0 * float(os.environ.get("WINDOW_S", "10"))) + _CONTROL_TICK_S)))

# SCALEUP_POLICY selects the scale-up selection policy:
#   "dual"                 → pre-RQ2 behavior (both tiers may fire + submit
#                            independently, no budget, RQ1 decision log) —
#                            DEFAULT. Keeps canonical / RQ1 runs byte-identical.
#   "fixed_compute_first"  → RQ2 arm: always compute, never storage.
#   "fixed_storage_first"  → RQ2 arm: always storage, never compute.
#   "bottleneck_aware"     → RQ2 arm: classify the bottleneck from tier-specific
#                            telemetry and select the matching tier.
# Unknown value → log error + fall back to "dual" (deliberate, documented).
_SCALEUP_POLICY = os.environ.get("SCALEUP_POLICY", "dual")

# RQ2 action budget: hard ceiling on scale-up (spawn) submissions per tier per
# controller (per LAN). 0 disables enforcement (dual). Scale-down does not
# consume budget. See docs/telemetry-and-scaling.md.
_ACTION_BUDGET_PER_TIER = int(os.environ.get("ACTION_BUDGET_PER_TIER", "4"))

# RQ2 bottleneck classifier margin (D3): |compute_score_norm - storage_score_norm|
# <= margin → classify as storage (documented tie-break; storage is the
# higher-urgency tier). Pre-registered per run, identical across arms.
_BOTTLENECK_CLASSIFY_MARGIN = float(
    os.environ.get("BOTTLENECK_CLASSIFY_MARGIN", "0.05")
)

# READINESS_PROPAGATION selects compute-backend admission timing:
#   "off"       → current pre-RQ3 behavior: register into the VIP_SERVER pool
#                 immediately after spawn (no probe, no admission log, no
#                 readiness worker thread) — DEFAULT. Canonical / RQ1 / RQ2
#                 runs byte-identical.
#   "direct"    → RQ3 v2 arm (approach A, event-driven): admit on the edge's
#                 `app_ready` control event (no probe before admission);
#                 /ready is used only for the post-admission identity check,
#                 the event-absence safety net, and abandonment detection.
#   "discovery" → RQ3 arm: probe /ready only on DISCOVERY_POLL_INTERVAL_S
#                 cadence; admit when a discovery pass sees 200 (periodic
#                 discovery).
# Unknown value → log error + fall back to "off" (deliberate, documented).
_READINESS_PROPAGATION = os.environ.get("READINESS_PROPAGATION", "off")

# STORAGE_PROPAGATION selects storage-replica VIP read-pool promotion timing:
#   "both"      → current behavior (DEFAULT): promote on the rs_secondary_ready
#                 control event AND on telemetry-detected SECONDARY fallback.
#   "direct"    → promote ONLY on the `rs_secondary_ready` control event
#                 (lifecycle notification; no telemetry promotion).
#   "discovery" → promote ONLY on telemetry `member_state == SECONDARY`
#                 (periodic discovery; quantization = telemetry cadence).
# Unknown value → log error + fall back to "both" (deliberate, documented).
_STORAGE_PROPAGATION = os.environ.get("STORAGE_PROPAGATION", "both")

# Per-attempt /ready HTTP timeout (seconds).
_READINESS_PROBE_TIMEOUT_S = float(os.environ.get("READINESS_PROBE_TIMEOUT_S", "5.0"))
# Abandon a pending backend that is not ready within this many seconds of
# spawn completion (full teardown + IP release). Must exceed
# DISCOVERY_POLL_INTERVAL_S and app startup time.
_READINESS_PROBE_MAX_S = float(os.environ.get("READINESS_PROBE_MAX_S", "120.0"))
# direct-mode probe retry interval (seconds). Must be << DISCOVERY_POLL_INTERVAL_S.
_READINESS_PROBE_RETRY_S = float(os.environ.get("READINESS_PROBE_RETRY_S", "1.0"))
# direct-mode event-absence safety net: no /ready probing until this many
# seconds after spawn-complete, giving the `app_ready` event time to arrive.
# A lost event cannot strand a ready backend — probing resumes afterwards
# (admit_source="probe_fallback"), which the analyzer's event-fraction gate
# surfaces as instrumentation-degraded.
_READINESS_EVENT_FALLBACK_S = float(os.environ.get("READINESS_EVENT_FALLBACK_S", "5.0"))
# RQ3 soundness (optional arm): "probe" defers event-driven admission until a
# successful /ready probe (wake_verify); "off" (default) keeps the plain
# event-driven contract. Only consulted when READINESS_PROPAGATION=direct.
_READINESS_WAKE_VERIFY_MODE = os.environ.get("READINESS_WAKE_VERIFY_MODE", "off")
# discovery-mode scan cadence (seconds). Pre-registered per run.
_DISCOVERY_POLL_INTERVAL_S = float(os.environ.get("DISCOVERY_POLL_INTERVAL_S", "10.0"))
# Edge-server /ready port. Must equal the edge server's BIND_PORT (5000) in RQ3 runs.
_EDGE_READY_PORT = int(os.environ.get("EDGE_READY_PORT", "5000"))
# RQ3 admission log (per controller / per LAN). Written only when the gate is active.
_ADMISSION_LOG_PATH = os.environ.get("ADMISSION_LOG_PATH", "/tmp/admission_log.csv")
# Flow-isolation mode: 1 = delete a client's VIP_SERVER flows after each
# response (one fresh backend-selection event per request). RQ3 measurement
# instrumentation; 0 elsewhere.
_VIP_FLOW_ISOLATION = int(os.environ.get("VIP_FLOW_ISOLATION", "0"))
# Per-connection VIP_SERVER flow matching (RQ3 flow isolation).
# 0 (default) = per-CLIENT flows (one DNAT/SNAT pair per client, keyed by
#   client_mac); preserves the canonical/RQ1/RQ2 per-client flow design (D5).
#   NOTE: selection behaviour itself changed after P2.6 (default-active
#   spread) — this knob only controls the flow layout.
# 1 = per-CONNECTION flows keyed on tcp_src (the client's ephemeral port).
#   Each fresh request connection gets its own flow pair, so the async
#   request_complete delete for one request can never collide with the next
#   request's flow -> Check C delete coverage approaches 1.0 under the
#   calibrated spike rate (per-client flows shared a generation whenever the
#   delete landed after the next SYN). RQ3 arm envs set this to 1.
_VIP_SERVER_PER_CONNECTION = int(
    os.environ.get("VIP_SERVER_PER_CONNECTION_FLOWS", "0"))
# Misconfiguration guard: warn (once) if flow isolation is enabled but no
# request_complete events have arrived within this many seconds of startup.
_FLOW_ISOLATION_WARMUP_S = float(os.environ.get("FLOW_ISOLATION_WARMUP_S", "120.0"))

# RQ2 v4 P2.6 — backend-selection spread over near-equal costs.
# Costs within this epsilon of the pool minimum are treated as a tie band and
# distributed across clients (stateless deterministic hash in
# _vip_routing/selection.select_server) instead of collapsing to one winner
# per selection burst. Without it, byte-identical within-burst costs + the
# synchronized 60 s flow hard-timeout funnel every lane's traffic onto ONE
# serving unit per epoch (one-hot; added replicas rotate the hot seat instead
# of adding concurrency). 0 = legacy argmin/exact-tie behaviour.
# Repair (like the P1 liveness fix), not a mode: active by default, so a
# re-run of RQ1/RQ3 campaigns on this commit selects differently than their
# archives — reproductions mean the pre-P2.6 commit.
# Band membership can include a cross-LAN unit only when that unit is itself
# within eps of the global minimum; the typical hop gap is
# W_HOPS * delta_hops / hops_max (about 0.14-0.28 in the canonical topology)
# — keep eps well below it. Applies in every policy mode when eps > 0;
# under topology_slowstart the ramp penalty adds to costs, so a unit can
# join the band late in its ramp.
_BACKEND_SELECTION_SPREAD_EPS = float(
    os.environ.get("BACKEND_SELECTION_SPREAD_EPS", "0.03"))

# RQ2 v4 P2.8 — minimum spread-band size (floor).
# The eps-band alone still collapses to one member whenever the cheapest unit
# wins by more than eps — the common case under load — so most selection
# bursts stayed one-hot after P2.6 (8/9 lan2 bursts in the P2.7 verification)
# and relief failed. With this floor k > 1, a band smaller than k is topped
# up to k members with the cheapest eligible LOCAL units (finite cost, known
# IP, not peer, slow-start penalty fully decayed; MAC tie-break keeps the cut
# deterministic across pool rebuilds), clamped to the number of eligible
# locals. The band then holds at least min(k, eligible locals) members and
# per-unit load falls to about rate/k once >= k locals are eligible;
# per-burst fan-out is hash-determined from
# the band (expected = band size). k <= 1 disables the floor (P2.6
# semantics). Applies only when the spread is active (eps > 0). Same repair
# discipline as P2.6: default-active; pre-P2.8 reproduction baseline = the
# P2.6 commit 57f58c3.
_BACKEND_SELECTION_SPREAD_MIN_BAND = int(
    os.environ.get("BACKEND_SELECTION_SPREAD_MIN_BAND", "4"))

# D21b: env-gated epsilon-band for STORAGE selection (select_storage),
# mirroring the compute P2.6/P2.8 band construction but with three explicit
# deviations (round-robin WITHIN the band reusing the per-domain rr counter;
# the warm-claim early return is kept; the log token is `storage_band=`, never
# compute's `spread band=`). Defaults 0.0/0 => exact v4 `select_storage`
# (strict argmin + exact-tie round-robin) — v5 pins EPS=0.03 / MIN_BAND=2.
# See docs/operation/testing/experiment/v5/rq2/rq2v5_plan.md S3.2b.
_STORAGE_SELECTION_SPREAD_EPS = float(
    os.environ.get("STORAGE_SELECTION_SPREAD_EPS", "0.0"))
_STORAGE_SELECTION_SPREAD_MIN_BAND = int(
    os.environ.get("STORAGE_SELECTION_SPREAD_MIN_BAND", "0"))

# Controller half of the double gate for the admission-time epoch renewal
# (S3.4/S3.5): 1 => POST /pool_renew to every edge of this LAN after a storage
# admission; 0 (default) => no broadcast at all (v4 behaviour unchanged).
_STORAGE_POOL_RENEW_ENABLED = os.environ.get("STORAGE_POOL_RENEW_ENABLED", "0").strip() == "1"

# 1 = maintain one ready same-LAN storage reserve per LAN; 0 = off.
_STORAGE_PERSISTENT_RESERVE_ENABLED = int(
    os.environ.get("STORAGE_PERSISTENT_RESERVE_ENABLED", "0")
)
# Telemetry-window budget for pending reserve activation.
# A trigger that latches while the reserve is PREPARING carries forward
# across reserve replacement but expires after this many telemetry windows.
_STORAGE_RESERVE_PENDING_WINDOWS = int(
    os.environ.get("STORAGE_RESERVE_PENDING_WINDOWS", "6")
)

_VIP_WARM_STORAGE_SECONDS = float(
	os.environ.get("VIP_WARM_STORAGE_SECONDS", "30")
)
_VIP_WARM_SERVER_SECONDS = float(
	os.environ.get("VIP_WARM_SERVER_SECONDS", "45")
)

