#!/bin/bash

# Full experiment orchestration for the phased edge content-discovery workload.
#
# Usage:
#   sudo ./run_experiment.sh [--batch-dir batch4] [--run-label c0] [--skip-clients] [--skip-seed] [--skip-snapshot]
#   sudo ./run_experiment.sh --phases-config phases_custom.json --fault-plan fault_plan.json
#   sudo ./run_experiment.sh --clients-per-lan 6 --seed-content-items 600 --seed-users 100 \
#       --phases-config testing/phases_override/phases_tier1_smoke.json --run-label tier1_smoke
#
# Flags (all optional):
#   --batch-dir DIR     Place the run folder under metrics/DIR/ (example: metrics/batch4/20260501_153012_c0)
#   --run-label LABEL    Append a config label to the metrics folder name (example: 20260501_153012_c0)
#   --skip-clients       Skip step 1 (test namespaces already created)
#   --skip-seed          Skip step 2 (data already seeded)
#   --skip-snapshot      Skip step 3 (snapshot already exported)
#   --clients-per-lan N  Override the number of client namespaces created per LAN
#   --seed-content-items N Override the number of content items seeded per LAN
#   --seed-users N       Override the number of user profiles seeded per LAN
#   --snapshot-dir DIR   Override snapshot directory (default: REPO_ROOT/data/workload_snapshot)
#   --phases-config FILE Override the traffic-generator phases file
#   --fault-plan FILE    Optional fault-injection plan consumed by fault_injector.py
#   --dry-run            Pass --dry-run to traffic_generator (no real requests)
# ============================================================================

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly DEFAULT_CONTROLLER_ENV_SOURCE="${SCRIPTS_DIR}/osken-controller.env"
readonly CONTROLLER_ENV_BASE_SOURCE="${OSKEN_ENV_FILE:-${DEFAULT_CONTROLLER_ENV_SOURCE}}"
readonly CONTROLLER_ENV_OVERRIDE_SOURCE="${OSKEN_ENV_OVERRIDE_FILE:-}"
readonly RUN_TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
TEMP_CONTROLLER_ENV_SOURCE=""

# ---------------------------------------------------------------------------
# Configuration — edit these to match your experiment
# ---------------------------------------------------------------------------

CLIENTS_PER_LAN="${CLIENTS_PER_LAN:-3}"

# Namespace prefixes — produces lan1_client_1, lan1_client_2 … lan2_client_1, lan2_client_2 …
PREFIX_LAN1="lan1_client_"
PREFIX_LAN2="lan2_client_"

SEED_CONTENT_ITEMS="${SEED_CONTENT_ITEMS:-100}"
SEED_USERS="${SEED_USERS:-40}"

MONGO_LAN1="mongodb://10.0.0.4:27018/"
MONGO_LAN2="mongodb://10.0.1.4:27018/"

SNAPSHOT_DIR="${SCRIPT_DIR}/data/workload_snapshot"

RANDOM_SEED="${RANDOM_SEED:-}"

VIP_LAN1="10.0.0.253:5000"
VIP_LAN2="10.0.1.253:5000"

LAN1_PUB="tcp://10.0.0.5:5556"
LAN2_PUB="tcp://10.0.1.5:5556"
# SDN controller coordinator-state PUB (5561=lan1, 5562=lan2) mirrors COORDINATOR_STATE_PUB_PORT
# in build_network_setup.sh; both controllers run --network host, so 127.0.0.1 reaches them.
LAN1_COORD_PUB="tcp://127.0.0.1:5561"
LAN2_COORD_PUB="tcp://127.0.0.1:5562"

CONTAINER_EVENTS_INTERVAL="${CONTAINER_EVENTS_INTERVAL:-1.0}"
CONTAINER_EVENTS_FILTER="${CONTAINER_EVENTS_FILTER:-^(edge_|sel_sync_|nat-router|osken|local_state_)}"

STATS_PID=""

CONTROLLER_STATS_PID=""

CONTROLLER_LOG_PID1=""
CONTROLLER_LOG_PID2=""

CONTAINER_EVENTS_PID=""

SERVICE_LOG_PID=""

FAULT_INJECTOR_PID=""


SKIP_CLIENTS=false
SKIP_SEED=false
SKIP_SNAPSHOT=false
DRY_RUN=false
BATCH_DIR=""
RUN_LABEL=""
PHASES_CONFIG_OVERRIDE=""
FAULT_PLAN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --batch-dir)    shift; BATCH_DIR="$1" ;;
        --batch-dir=*)  BATCH_DIR="${1#*=}"    ;;
        --run-label)     shift; RUN_LABEL="$1"  ;;
        --run-label=*)   RUN_LABEL="${1#*=}"    ;;
        --skip-clients)   SKIP_CLIENTS=true  ;;
        --skip-seed)      SKIP_SEED=true     ;;
        --skip-snapshot)  SKIP_SNAPSHOT=true ;;
        --clients-per-lan) shift; CLIENTS_PER_LAN="$1" ;;
        --clients-per-lan=*) CLIENTS_PER_LAN="${1#*=}" ;;
        --seed-content-items)   shift; SEED_CONTENT_ITEMS="$1" ;;
        --seed-content-items=*) SEED_CONTENT_ITEMS="${1#*=}" ;;
        --seed-users)     shift; SEED_USERS="$1" ;;
        --seed-users=*)   SEED_USERS="${1#*=}" ;;
        --dry-run)        DRY_RUN=true       ;;
        --snapshot-dir)   shift; SNAPSHOT_DIR="$1" ;;
        --snapshot-dir=*) SNAPSHOT_DIR="${1#*=}" ;;
        --phases-config)   shift; PHASES_CONFIG_OVERRIDE="$1" ;;
        --phases-config=*) PHASES_CONFIG_OVERRIDE="${1#*=}" ;;
        --fault-plan)      shift; FAULT_PLAN="$1" ;;
        --fault-plan=*)    FAULT_PLAN="${1#*=}" ;;
        -h|--help)
            sed -n '/^# Usage/,/^# -----/p' "$0" | grep '^#' | sed 's/^# *//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown flag: $1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ -n "$RUN_LABEL" ]] && [[ ! "$RUN_LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
    echo "ERROR: invalid --run-label '$RUN_LABEL' (allowed: letters, numbers, '_' and '-')" >&2
    exit 1
fi

if [[ -n "$BATCH_DIR" ]] && [[ ! "$BATCH_DIR" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$ ]]; then
    echo "ERROR: invalid --batch-dir '$BATCH_DIR' (allowed: relative path segments with letters, numbers, '.', '_' and '-')" >&2
    exit 1
fi

step() { echo; echo "==> $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

require_positive_int() {
    local name="$1"
    local value="$2"

    [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "invalid ${name} '${value}' (expected positive integer)"
}

require_positive_int "--clients-per-lan" "$CLIENTS_PER_LAN"
require_positive_int "--seed-content-items" "$SEED_CONTENT_ITEMS"
require_positive_int "--seed-users" "$SEED_USERS"

resolve_path_from_scripts_dir() {
    local path="$1"

    if [[ -z "$path" ]]; then
        printf '%s\n' ""
        return 0
    fi

    if [[ "$path" == /* ]]; then
        printf '%s\n' "$path"
        return 0
    fi

    printf '%s\n' "${SCRIPTS_DIR}/${path#./}"
}

apply_env_override_file() {
    local target_path="$1"
    local override_path="$2"
    local line=""
    local key=""
    local temp_path=""

    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        if [[ "$line" != *=* ]]; then
            die "invalid controller env override line: ${line}"
        fi

        key="${line%%=*}"
        temp_path="$(mktemp)"
        awk -v key="$key" -v newline="$line" '
            $0 ~ "^" key "=" { next }
            { print }
            END { print newline }
        ' "$target_path" > "$temp_path"
        mv "$temp_path" "$target_path"
    done < "$override_path"
}

prepare_controller_env_source() {
    local base_env_path=""
    local override_env_path=""

    base_env_path="$(resolve_path_from_scripts_dir "$CONTROLLER_ENV_BASE_SOURCE")"
    override_env_path="$(resolve_path_from_scripts_dir "$CONTROLLER_ENV_OVERRIDE_SOURCE")"

    [[ -f "$base_env_path" ]] || die "Controller env not found: $base_env_path"

    if [[ -z "$override_env_path" ]]; then
        CONTROLLER_ENV_SOURCE="$base_env_path"
        return 0
    fi

    [[ -f "$override_env_path" ]] || die "Controller env override not found: $override_env_path"
    TEMP_CONTROLLER_ENV_SOURCE="$(mktemp)"
    cp "$base_env_path" "$TEMP_CONTROLLER_ENV_SOURCE"
    apply_env_override_file "$TEMP_CONTROLLER_ENV_SOURCE" "$override_env_path"
    CONTROLLER_ENV_SOURCE="$TEMP_CONTROLLER_ENV_SOURCE"
}

cleanup_temp_controller_env() {
    if [[ -n "$TEMP_CONTROLLER_ENV_SOURCE" && -f "$TEMP_CONTROLLER_ENV_SOURCE" ]]; then
        rm -f "$TEMP_CONTROLLER_ENV_SOURCE"
    fi
}

_write_run_status() {
    local status="$1"
    local exit_code="${2:-}"
    local phase="${3:-}"
    local completed_at="null"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "unknown")"

    [ "$status" = "completed" ] || [ "$status" = "failed" ] && completed_at="\"$ts\""

    mkdir -p "$(dirname "$RUN_STATUS_FILE")" 2>/dev/null || true

    cat > "$RUN_STATUS_FILE" <<JSON
{
  "status": "${status}",
  "run_label": "${RUN_LABEL:-}",
  "run_id": "${RUN_ID}",
  "started_at": "${RUN_STARTED_AT}",
  "completed_at": ${completed_at},
  "exit_code": ${exit_code:-null},
  "current_phase": "${phase}"
}
JSON

    cp "$RUN_STATUS_FILE" "$ACTIVE_RUN_FILE" 2>/dev/null || true

    # Sentinel file — eliminates need for JSON parsing in failure handler
    if [ "$status" = "completed" ]; then
        touch "${RUN_DIR}/.run_completed" 2>/dev/null || true
    fi
}

_write_run_status_failed() {
    local code=$?
    [ -z "${RUN_STATUS_FILE:-}" ] && return 0
    [ -z "${RUN_DIR:-}" ] && return 0
    [ -f "${RUN_DIR}/.run_completed" ] && return 0
    _write_run_status "failed" "$code" "unknown"
}

prepare_controller_env_source

readonly RUN_ID="${RUN_TIMESTAMP}${RUN_LABEL:+_${RUN_LABEL}}"
readonly METRICS_ROOT="${SCRIPT_DIR}/metrics"
readonly RUN_PARENT_DIR="${METRICS_ROOT}${BATCH_DIR:+/${BATCH_DIR}}"
readonly RUN_DIR="${RUN_PARENT_DIR}/${RUN_ID}"
readonly RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
readonly RUN_STATUS_FILE="${RUN_DIR}/run_status.json"
readonly ACTIVE_RUN_FILE="${METRICS_ROOT}/active_run.json"
readonly CONTROLLER_ENV_SNAPSHOT_OUTPUT="${RUN_DIR}/controller_env_snapshot.env"

PHASES_CONFIG="${SCRIPT_DIR}/phases.json"
PHASES_SNAPSHOT_OUTPUT="${RUN_DIR}/phases_snapshot.json"
METRICS_OUTPUT="${RUN_DIR}/client_requests.csv"

if [[ -n "$PHASES_CONFIG_OVERRIDE" ]]; then
    PHASES_CONFIG="$PHASES_CONFIG_OVERRIDE"
fi

RESOURCE_STATS_OUTPUT="${RUN_DIR}/resource_stats.csv"
RESOURCE_STATS_DEBUG_OUTPUT="${RUN_DIR}/resource_stats_debug.csv"
PHASE_FILE="${RUN_DIR}/current_phase.txt"

POLICY_STATE_OUTPUT="${RUN_DIR}/policy_state.csv"
ELASTICITY_EVENTS_OUTPUT="${RUN_DIR}/elasticity_events.csv"

CONTAINER_EVENTS_OUTPUT="${RUN_DIR}/container_events.csv"

CONTROLLER_LOG_LAN1="${RUN_DIR}/controller_lan1.log"
CONTROLLER_LOG_LAN2="${RUN_DIR}/controller_lan2.log"

SERVICE_LOG_DIR="${RUN_DIR}/service_logs"

FAULT_PLAN_SNAPSHOT_OUTPUT="${RUN_DIR}/fault_plan_snapshot.json"
FAULT_EVENTS_OUTPUT="${RUN_DIR}/experiment_fault_events.csv"


prepare_run_outputs() {
    step "Preparing run output folder"
    mkdir -p "$RUN_DIR"
    _write_run_status "running" "" "setup"
    [[ -f "$PHASES_CONFIG" ]] || die "Phase config not found: $PHASES_CONFIG"
    [[ -f "$CONTROLLER_ENV_SOURCE" ]] || die "Controller env not found: $CONTROLLER_ENV_SOURCE"
    if [[ -n "$FAULT_PLAN" ]]; then
        [[ -f "$FAULT_PLAN" ]] || die "Fault plan not found: $FAULT_PLAN"
    fi
    cp "$PHASES_CONFIG" "$PHASES_SNAPSHOT_OUTPUT"
    cp "$CONTROLLER_ENV_SOURCE" "$CONTROLLER_ENV_SNAPSHOT_OUTPUT"
    if [[ -n "$FAULT_PLAN" ]]; then
        cp "$FAULT_PLAN" "$FAULT_PLAN_SNAPSHOT_OUTPUT"
    fi
    echo "  Run dir    : ${RUN_DIR}"
    echo "  Batch dir  : ${BATCH_DIR:-<none>}"
    echo "  Run label  : ${RUN_LABEL:-<none>}"
    echo "  Phase copy : ${PHASES_SNAPSHOT_OUTPUT}"
    echo "  Env copy   : ${CONTROLLER_ENV_SNAPSHOT_OUTPUT}"
    if [[ -n "$FAULT_PLAN" ]]; then
        echo "  Fault copy : ${FAULT_PLAN_SNAPSHOT_OUTPUT}"
        echo "  Fault log  : ${FAULT_EVENTS_OUTPUT}"
    fi
}

build_client_lists() {
    local lan1="" lan2="" i
    for ((i = 1; i <= CLIENTS_PER_LAN; i++)); do
        lan1+="${lan1:+,}${PREFIX_LAN1}${i}"
        lan2+="${lan2:+,}${PREFIX_LAN2}${i}"
    done
    CLIENTS_LAN1="$lan1"
    CLIENTS_LAN2="$lan2"
}


run_create_clients() {
    step "Creating test client namespaces"
    local create_script="${REPO_ROOT}/source/scripts/network/clients/create_test_clients.sh"
    [[ -f "$create_script" ]] || die "Not found: $create_script"

    echo "  LAN 1: ${CLIENTS_PER_LAN} clients (prefix: ${PREFIX_LAN1})"
    bash "$create_script" --lan 1 --count "$CLIENTS_PER_LAN" --prefix "$PREFIX_LAN1"

    echo "  LAN 2: ${CLIENTS_PER_LAN} clients (prefix: ${PREFIX_LAN2})"
    bash "$create_script" --lan 2 --count "$CLIENTS_PER_LAN" --prefix "$PREFIX_LAN2"
}


run_seed() {
    step "Seeding MongoDB"

    echo "  content_items (${SEED_CONTENT_ITEMS} content items/region)"
    python3 "${SCRIPT_DIR}/seed_content_items.py" \
        --mongo-lan1 "$MONGO_LAN1" --mongo-lan2 "$MONGO_LAN2" \
        --content-items "$SEED_CONTENT_ITEMS"

    echo "  user_profiles (${SEED_USERS} users/region, ${SEED_CONTENT_ITEMS} content IDs)"
    python3 "${SCRIPT_DIR}/seed_user_profiles.py" \
        --mongo-lan1 "$MONGO_LAN1" --mongo-lan2 "$MONGO_LAN2" \
        --users "$SEED_USERS" --content-items "$SEED_CONTENT_ITEMS"

    echo "  create_indexes"
    python3 "${SCRIPT_DIR}/create_indexes.py" \
        --mongo-lan1 "$MONGO_LAN1" --mongo-lan2 "$MONGO_LAN2"
}


run_snapshot() {
    step "Exporting workload snapshot → ${SNAPSHOT_DIR}"
    python3 "${SCRIPT_DIR}/export_workload_snapshot.py" \
        --mongo-lan1 "$MONGO_LAN1" --mongo-lan2 "$MONGO_LAN2" \
        --output-dir "$SNAPSHOT_DIR"
}


run_collect_stats() {
    step "Starting resource stats collector"
    echo "  LAN1 PUB : ${LAN1_PUB}"
    echo "  LAN2 PUB : ${LAN2_PUB}"
    echo "  LAN1 coord: ${LAN1_COORD_PUB}"
    echo "  LAN2 coord: ${LAN2_COORD_PUB}"
    echo "  Output   : ${RESOURCE_STATS_OUTPUT}"
    echo "  Debug    : ${RESOURCE_STATS_DEBUG_OUTPUT}"
    python3 "${SCRIPT_DIR}/collect_resource_stats.py" \
        --lan1-pub "$LAN1_PUB" \
        --lan2-pub "$LAN2_PUB" \
        --lan1-coord-pub "$LAN1_COORD_PUB" \
        --lan2-coord-pub "$LAN2_COORD_PUB" \
        --output       "$RESOURCE_STATS_OUTPUT" \
        --output-debug "$RESOURCE_STATS_DEBUG_OUTPUT" \
        --phase-file "$PHASE_FILE" &
    STATS_PID=$!
    echo "  Collector PID: ${STATS_PID}"
    sleep 1  # allow ZMQ subscriber handshake to complete before traffic starts
}

stop_collect_stats() {
    [[ -z "${STATS_PID:-}" ]] && return 0
    echo; echo "==> Stopping resource stats collector (PID ${STATS_PID})"
    kill -TERM "$STATS_PID" 2>/dev/null || true
    wait "$STATS_PID" 2>/dev/null || true
    STATS_PID=""
}


CONTROLLER_STATS_OUTPUT="${RUN_DIR}/controller_stats.csv"

run_sample_controller_stats() {
    step "Starting controller overhead sampler"
    echo "  Containers : osken, osken_2"
    echo "  Interval   : 5s"
    echo "  Output     : ${CONTROLLER_STATS_OUTPUT}"
    python3 "${SCRIPT_DIR}/sample_controller_stats.py" \
        --output "${CONTROLLER_STATS_OUTPUT}" \
        --phase-file "${PHASE_FILE}" \
        --interval 5 \
        --containers osken,osken_2 &
    CONTROLLER_STATS_PID=$!
    echo "  Sampler PID: ${CONTROLLER_STATS_PID}"
}

stop_sample_controller_stats() {
    [[ -z "${CONTROLLER_STATS_PID:-}" ]] && return 0
    echo; echo "==> Stopping controller overhead sampler (PID ${CONTROLLER_STATS_PID})"
    kill -TERM "$CONTROLLER_STATS_PID" 2>/dev/null || true
    wait "$CONTROLLER_STATS_PID" 2>/dev/null || true
    CONTROLLER_STATS_PID=""
}


run_capture_controller_logs() {
    step "Capturing controller logs"
    echo "  LAN1 → ${CONTROLLER_LOG_LAN1}"
    echo "  LAN2 → ${CONTROLLER_LOG_LAN2}"
    docker logs -f osken   > "$CONTROLLER_LOG_LAN1" 2>&1 &
    CONTROLLER_LOG_PID1=$!
    docker logs -f osken_2 > "$CONTROLLER_LOG_LAN2" 2>&1 &
    CONTROLLER_LOG_PID2=$!
    echo "  PIDs: ${CONTROLLER_LOG_PID1}, ${CONTROLLER_LOG_PID2}"
}

stop_capture_controller_logs() {
    for pid_var in CONTROLLER_LOG_PID1 CONTROLLER_LOG_PID2; do
        local pid="${!pid_var}"
        [[ -z "${pid:-}" ]] && continue
        kill -TERM "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        eval "$pid_var="
    done
}


run_capture_service_logs() {
    step "Capturing service logs"
    echo "  Output   : ${SERVICE_LOG_DIR}"
    python3 "${SCRIPT_DIR}/capture_service_logs.py" \
        --output-dir "$SERVICE_LOG_DIR" &
    SERVICE_LOG_PID=$!
    echo "  Helper PID: ${SERVICE_LOG_PID}"
}

stop_capture_service_logs() {
    [[ -z "${SERVICE_LOG_PID:-}" ]] && return 0
    echo; echo "==> Stopping service log capture (PID ${SERVICE_LOG_PID})"
    kill -TERM "$SERVICE_LOG_PID" 2>/dev/null || true
    wait "$SERVICE_LOG_PID" 2>/dev/null || true
    SERVICE_LOG_PID=""
}


run_fault_injector() {
    [[ -z "$FAULT_PLAN" ]] && return 0

    step "Starting fault injector"
    echo "  Plan     : ${FAULT_PLAN}"
    echo "  Phase    : ${PHASE_FILE}"
    echo "  Output   : ${FAULT_EVENTS_OUTPUT}"
    python3 "${SCRIPT_DIR}/fault_injector.py" \
        --plan "$FAULT_PLAN" \
        --phase-file "$PHASE_FILE" \
        --controller-log-lan1 "$CONTROLLER_LOG_LAN1" \
        --controller-log-lan2 "$CONTROLLER_LOG_LAN2" \
        --output "$FAULT_EVENTS_OUTPUT" &
    FAULT_INJECTOR_PID=$!
    echo "  Helper PID: ${FAULT_INJECTOR_PID}"
}

stop_fault_injector() {
    [[ -z "${FAULT_INJECTOR_PID:-}" ]] && return 0
    echo; echo "==> Stopping fault injector (PID ${FAULT_INJECTOR_PID})"
    kill -TERM "$FAULT_INJECTOR_PID" 2>/dev/null || true
    wait "$FAULT_INJECTOR_PID" 2>/dev/null || true
    FAULT_INJECTOR_PID=""
}


run_poll_container_events() {
    step "Starting container event poller"
    echo "  Interval : ${CONTAINER_EVENTS_INTERVAL}s"
    echo "  Filter   : ${CONTAINER_EVENTS_FILTER}"
    echo "  Output   : ${CONTAINER_EVENTS_OUTPUT}"
    python3 "${SCRIPT_DIR}/poll_container_events.py" \
        --interval     "$CONTAINER_EVENTS_INTERVAL" \
        --filter-regex "$CONTAINER_EVENTS_FILTER" \
        --phase-file   "$PHASE_FILE" \
        --output       "$CONTAINER_EVENTS_OUTPUT" &
    CONTAINER_EVENTS_PID=$!
    echo "  Poller PID: ${CONTAINER_EVENTS_PID}"
}

stop_poll_container_events() {
    [[ -z "${CONTAINER_EVENTS_PID:-}" ]] && return 0
    echo; echo "==> Stopping container event poller (PID ${CONTAINER_EVENTS_PID})"
    kill -TERM "$CONTAINER_EVENTS_PID" 2>/dev/null || true
    wait "$CONTAINER_EVENTS_PID" 2>/dev/null || true
    CONTAINER_EVENTS_PID=""
}


run_traffic() {
    step "Running traffic generator"
    echo "  LAN1 clients : ${CLIENTS_LAN1}"
    echo "  LAN2 clients : ${CLIENTS_LAN2}"
    echo "  Config       : ${PHASES_CONFIG}"
    echo "  Output       : ${METRICS_OUTPUT}"

    # Fail-fast gate for the open-loop driver (hard requirement for RQ2 and the final RQ
    # campaigns; see rq2_v2_rework_plan.md Phase 1).
    if [[ "${TRAFFIC_DRIVER_MODE:-}" == "open_loop" ]]; then
        echo "  Driver mode  : open_loop (window=${INFLIGHT_WINDOW:-1024}, drain=${DRAIN_S:-30}s)"
        python3 "${SCRIPT_DIR}/openloop_p1_01_driver_selftest.py" || {
            echo "ERROR: open-loop driver self-test FAILED — refusing to run the experiment" >&2
            exit 1
        }
    fi

    local extra_flags=()
    "$DRY_RUN" && extra_flags+=("--dry-run")
    [[ -n "$RANDOM_SEED" ]] && extra_flags+=("--random-seed" "$RANDOM_SEED")
    [[ -n "${TRAFFIC_DRIVER_MODE:-}" ]] && extra_flags+=("--driver-mode" "$TRAFFIC_DRIVER_MODE")
    [[ -n "${INFLIGHT_WINDOW:-}" ]] && extra_flags+=("--in-flight-window" "$INFLIGHT_WINDOW")
    [[ -n "${DRAIN_S:-}" ]] && extra_flags+=("--drain-s" "$DRAIN_S")

    if ! python3 "${SCRIPT_DIR}/traffic_generator.py" \
        --config        "$PHASES_CONFIG" \
        --clients-lan1  "$CLIENTS_LAN1" \
        --clients-lan2  "$CLIENTS_LAN2" \
        --snapshot-dir  "$SNAPSHOT_DIR" \
        --output        "$METRICS_OUTPUT" \
        --vip-lan1      "$VIP_LAN1" \
        --vip-lan2      "$VIP_LAN2" \
        "${extra_flags[@]}"; then
        echo "ERROR: traffic generator failed — run will be marked failed" >&2
        return 1
    fi
}


generate_elasticity_events() {
    step "Generating elasticity events from controller logs"
    echo "  LAN1 log : ${CONTROLLER_LOG_LAN1}"
    echo "  LAN2 log : ${CONTROLLER_LOG_LAN2}"
    echo "  Output   : ${ELASTICITY_EVENTS_OUTPUT}"
    python3 "${SCRIPTS_DIR}/tools/parse_elasticity_logs.py" \
        "$CONTROLLER_LOG_LAN1" \
        "$CONTROLLER_LOG_LAN2" \
        -o "$ELASTICITY_EVENTS_OUTPUT" \
        || echo "  WARNING: elasticity event generation failed (non-fatal)" >&2
}

generate_policy_state() {
    step "Reconstructing policy state from run artifacts"
    echo "  Resource stats  : ${RESOURCE_STATS_OUTPUT}"
    echo "  Resource debug  : ${RESOURCE_STATS_DEBUG_OUTPUT}"
    echo "  Per-node stats  : ${RUN_DIR}/per_node_stats.csv"
    echo "  Container events: ${CONTAINER_EVENTS_OUTPUT}"
    echo "  Controller env  : ${CONTROLLER_ENV_SNAPSHOT_OUTPUT}"
    echo "  Elasticity evts : ${ELASTICITY_EVENTS_OUTPUT}"
    echo "  Controller logs : ${CONTROLLER_LOG_LAN1}, ${CONTROLLER_LOG_LAN2}"
    echo "  Output          : ${POLICY_STATE_OUTPUT}"
    python3 "${SCRIPTS_DIR}/tools/reconstruct_policy_state.py" \
        --resource-stats       "$RESOURCE_STATS_OUTPUT" \
        --resource-stats-debug "$RESOURCE_STATS_DEBUG_OUTPUT" \
        --per-node-stats       "${RUN_DIR}/per_node_stats.csv" \
        --container-events     "$CONTAINER_EVENTS_OUTPUT" \
        --controller-env       "$CONTROLLER_ENV_SNAPSHOT_OUTPUT" \
        --elasticity-events    "$ELASTICITY_EVENTS_OUTPUT" \
        --controller-log-lan1  "$CONTROLLER_LOG_LAN1" \
        --controller-log-lan2  "$CONTROLLER_LOG_LAN2" \
        --output               "$POLICY_STATE_OUTPUT" \
        || echo "  WARNING: policy state reconstruction failed (non-fatal)" >&2
}

# RQ1 telemetry delivery artifacts — collected BEFORE any external cleanup runs.
collect_rq1_artifacts() {
    step "Collecting RQ1 telemetry delivery artifacts"
    local _wl1 _wl2 _al1 _al2 _dl1 _dl2 _dec1 _dec2
    _wl1="$(docker exec aggregator_n1 printenv WINDOW_LOG_PATH 2>/dev/null || echo /tmp/window_log.jsonl)"
    _wl2="$(docker exec aggregator_n2 printenv WINDOW_LOG_PATH 2>/dev/null || echo /tmp/window_log.jsonl)"
    _al1="$(docker exec aggregator_n1 printenv ACK_LOG_PATH 2>/dev/null || echo /tmp/ack_log.jsonl)"
    _al2="$(docker exec aggregator_n2 printenv ACK_LOG_PATH 2>/dev/null || echo /tmp/ack_log.jsonl)"
    _dl1="$(docker exec osken printenv DELIVERY_LOG_PATH 2>/dev/null || echo /tmp/telemetry_delivery_log.csv)"
    _dl2="$(docker exec osken_2 printenv DELIVERY_LOG_PATH 2>/dev/null || echo /tmp/telemetry_delivery_log.csv)"
    _dec1="$(docker exec osken printenv DECISION_LOG_PATH 2>/dev/null || echo /tmp/decision_log.csv)"
    _dec2="$(docker exec osken_2 printenv DECISION_LOG_PATH 2>/dev/null || echo /tmp/decision_log.csv)"

    docker cp "aggregator_n1:${_wl1}"  "${RUN_DIR}/window_log_lan1.jsonl"       2>/dev/null || echo "  WARNING: window_log_lan1 unavailable" >&2
    docker cp "aggregator_n2:${_wl2}"  "${RUN_DIR}/window_log_lan2.jsonl"       2>/dev/null || echo "  WARNING: window_log_lan2 unavailable" >&2
    docker cp "aggregator_n1:${_al1}"  "${RUN_DIR}/ack_log_lan1.jsonl"          2>/dev/null || echo "  WARNING: ack_log_lan1 unavailable" >&2
    docker cp "aggregator_n2:${_al2}"  "${RUN_DIR}/ack_log_lan2.jsonl"          2>/dev/null || echo "  WARNING: ack_log_lan2 unavailable" >&2
    docker cp "osken:${_dl1}"          "${RUN_DIR}/telemetry_delivery_log_lan1.csv" 2>/dev/null || echo "  WARNING: telemetry_delivery_log_lan1 unavailable" >&2
    docker cp "osken_2:${_dl2}"        "${RUN_DIR}/telemetry_delivery_log_lan2.csv" 2>/dev/null || echo "  WARNING: telemetry_delivery_log_lan2 unavailable" >&2
    docker cp "osken:${_dec1}"         "${RUN_DIR}/decision_log_lan1.csv"       2>/dev/null || echo "  WARNING: decision_log_lan1 unavailable" >&2
    docker cp "osken_2:${_dec2}"       "${RUN_DIR}/decision_log_lan2.csv"       2>/dev/null || echo "  WARNING: decision_log_lan2 unavailable" >&2

    # Aggregator env snapshot — per container so a run-level override on one
    # aggregator is not misrepresented by the other's values.
    {
        echo "# Aggregator env snapshot (per container) — RQ1 telemetry delivery"
        for _v in WINDOW_S OVERLOAD_CPU_PCT OVERLOAD_PEAK_LATENCY_MS OVERLOAD_ERROR_RATE \
                   OVERLOAD_MIN_REQUESTS \
                   WINDOW_LOG_RETENTION WINDOW_LOG_PATH ACK_LOG_PATH; do
            _v1="$(docker exec aggregator_n1 printenv "$_v" 2>/dev/null || true)"
            _v2="$(docker exec aggregator_n2 printenv "$_v" 2>/dev/null || true)"
            echo "${_v}_n1=${_v1}"
            echo "${_v}_n2=${_v2}"
        done
    } > "${RUN_DIR}/aggregator_env_snapshot.env"
    echo "  Aggregator env : ${RUN_DIR}/aggregator_env_snapshot.env"
}

# RQ3 admission logs — collected BEFORE any external cleanup runs.
# cleanup runs. Non-RQ3 runs produce no admission log → warning only.
collect_rq3_artifacts() {
    step "Collecting RQ3 readiness-propagation artifacts"
    local _adm1 _adm2
    _adm1="$(docker exec osken printenv ADMISSION_LOG_PATH 2>/dev/null || echo /tmp/admission_log.csv)"
    _adm2="$(docker exec osken_2 printenv ADMISSION_LOG_PATH 2>/dev/null || echo /tmp/admission_log.csv)"

    docker cp "osken:${_adm1}"   "${RUN_DIR}/admission_log_lan1.csv" 2>/dev/null \
        || echo "  WARNING: admission_log_lan1 unavailable (non-RQ3 run?)" >&2
    docker cp "osken_2:${_adm2}" "${RUN_DIR}/admission_log_lan2.csv" 2>/dev/null \
        || echo "  WARNING: admission_log_lan2 unavailable (non-RQ3 run?)" >&2
    echo "  Admission logs : ${RUN_DIR}/admission_log_lan1.csv, ${RUN_DIR}/admission_log_lan2.csv"
}

collect_research_q3_artifacts() {
    step "Collecting research_q3 release artifacts"
    local _rl1 _rl2
    _rl1="$(docker exec osken printenv RELEASE_LOG_PATH 2>/dev/null || echo /tmp/release_log.csv)"
    _rl2="$(docker exec osken_2 printenv RELEASE_LOG_PATH 2>/dev/null || echo /tmp/release_log.csv)"
    docker cp "osken:${_rl1}"   "${RUN_DIR}/release_log_lan1.csv" 2>/dev/null \
        || echo "  WARNING: release_log_lan1 unavailable (non-research_q3 run?)" >&2
    docker cp "osken_2:${_rl2}" "${RUN_DIR}/release_log_lan2.csv" 2>/dev/null \
        || echo "  WARNING: release_log_lan2 unavailable (non-research_q3 run?)" >&2
    docker cp "osken:/tmp/rs_evict" "${RUN_DIR}/rs_evict_logs_lan1" 2>/dev/null \
        || echo "  WARNING: rs_evict_logs_lan1 unavailable" >&2
    docker cp "osken_2:/tmp/rs_evict" "${RUN_DIR}/rs_evict_logs_lan2" 2>/dev/null \
        || echo "  WARNING: rs_evict_logs_lan2 unavailable" >&2
    echo "  Release logs  : ${RUN_DIR}/release_log_lan1.csv, ${RUN_DIR}/release_log_lan2.csv"
}

# RQ3 v2 validity gates (plan §4.3): min-admissions per LAN; flow-validation exit 1 = hard
# fail, 2 = coverage < 0.9 degraded; runs AFTER artifact collection, BEFORE run-folder cleanup.
verify_rq3_run() {
    local _snap="${RUN_DIR}/controller_env_snapshot.env"
    [[ -f "$_snap" ]] || { echo "  [rq3] no env snapshot — skipping RQ3 gate"; return 0; }
    local _prop
    _prop="$(grep -E '^READINESS_PROPAGATION=' "$_snap" | head -1 | cut -d= -f2 || true)"
    case "$_prop" in
        direct|discovery) ;;
        *) echo "  [rq3] READINESS_PROPAGATION=$_prop — non-RQ3 run, skipping gate"; return 0 ;;
    esac

    local _fail=0 _lan _adm _f
    # RQ3 measurement runs are identified by VIP_FLOW_ISOLATION=1; readiness-gate runs that
    # keep per-client flows (RQ2, the D5 design) must not be held to the flow-validation checks.
    local _flowiso _rq3_meas
    _flowiso="$(grep -E '^VIP_FLOW_ISOLATION=' "$_snap" | head -1 | cut -d= -f2 || true)"
    _rq3_meas=0
    [[ "$_flowiso" == "1" ]] && _rq3_meas=1
    if [[ "$_rq3_meas" -eq 1 ]]; then
        echo "  [rq3] VIP_FLOW_ISOLATION=1 — full RQ3 flow-isolation run"
    else
        echo "  [rq3] VIP_FLOW_ISOLATION=0 — readiness-gate-only run (RQ3 flow-isolation checks skipped)"
    fi
    for _lan in 1 2; do
        _f="${RUN_DIR}/admission_log_lan${_lan}.csv"
        if [[ -f "$_f" ]]; then
            _adm="$(grep -c ',admitted,' "$_f" 2>/dev/null || true)"
            _adm="${_adm:-0}"
            echo "  [rq3] lan${_lan} admitted backends: ${_adm}"
            if [[ "$_adm" -lt 1 ]]; then
                echo "  [rq3] FAIL: lan${_lan} has < 1 admitted backend (min-admissions gate)" >&2
                _fail=1
            fi
        else
            echo "  [rq3] WARN: admission_log_lan${_lan}.csv missing" >&2
        fi
    done

    # Flow-validation exit codes: 0 = valid, 1 = hard violation, 2 = instrumentation-degraded.
    if [[ "$_rq3_meas" -eq 1 ]]; then
        if [[ -f "${RUN_DIR}/client_requests.csv" ]]; then
            python3 "${REPO_ROOT}/docs/research_questions/v2/rq3/rq3_flow_validation.py" "${RUN_DIR}"
            local _rc=$?
            if [[ $_rc -eq 1 ]]; then
                echo "  [rq3] FAIL: flow-validation hard violation (exit 1)" >&2
                _fail=1
            elif [[ $_rc -eq 2 ]]; then
                echo "  [rq3] DEGRADED: flow-isolation coverage < 0.9 (recorded, not a hard fail)" >&2
            fi
        else
            echo "  [rq3] WARN: client_requests.csv missing — flow-validation skipped" >&2
        fi
    else
        echo "  [rq3] flow-validation skipped (not an RQ3 flow-isolation run)" >&2
    fi

    local _e1 _e2 _dyn _de
    _e1="$(docker exec osken printenv EDGE_APP_READY_EVENT 2>/dev/null || echo 0)"
    _e2="$(docker exec osken printenv EDGE_FLOW_ISOLATION 2>/dev/null || echo 0)"
    echo "  [rq3] controller env: EDGE_APP_READY_EVENT=$_e1 EDGE_FLOW_ISOLATION=$_e2"
    if [[ "$_rq3_meas" -eq 1 && "$_e2" != "1" ]]; then
        echo "  [rq3] FAIL: EDGE_FLOW_ISOLATION=1 required for RQ3 flow-isolation runs" >&2
        _fail=1
    elif [[ "$_rq3_meas" -eq 0 && "$_e2" != "0" ]]; then
        echo "  [rq3] WARN: EDGE_FLOW_ISOLATION=$_e2 — non-zero in a readiness-gate-only run" >&2
    fi
    if [[ "$_prop" == "direct" && "$_e1" != "1" ]]; then
        echo "  [rq3] FAIL: direct arm requires EDGE_APP_READY_EVENT=1 (controller env)" >&2
        _fail=1
    fi
    _dyn="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^edge_server_lan[12]_dyn' | head -1 || true)"
    if [[ -n "$_dyn" ]]; then
        _de="$(docker exec "$_dyn" printenv EDGE_APP_READY_EVENT 2>/dev/null || echo 0)"
        echo "  [rq3] dynamic edge ${_dyn}: EDGE_APP_READY_EVENT=$_de"
        if [[ "$_prop" == "direct" && "$_de" != "1" ]]; then
            echo "  [rq3] FAIL: dynamic edge lacks EDGE_APP_READY_EVENT=1" >&2
            _fail=1
        fi
    else
        echo "  [rq3] note: no live dynamic edge to verify EDGE_APP_READY_EVENT at post-run"
    fi

    if [[ $_fail -eq 1 ]]; then
        _write_run_status_failed
        echo "  [rq3] run marked FAILED by the RQ3 gate" >&2
        return 1
    fi
    return 0
}


build_client_lists

echo "======================================================"
echo " Edge Content Discovery Experiment — Full Run"
echo "======================================================"
echo " Batch dir   : ${BATCH_DIR:-<none>}"
echo " Clients/LAN : ${CLIENTS_PER_LAN}  (${CLIENTS_LAN1} | ${CLIENTS_LAN2})"
echo " Content/LAN : ${SEED_CONTENT_ITEMS}"
echo " Users/LAN   : ${SEED_USERS}"
echo " Snapshot    : ${SNAPSHOT_DIR}"
echo " Output      : ${METRICS_OUTPUT}"
echo " Resource    : ${RESOURCE_STATS_OUTPUT}"
echo " Phase file  : ${PHASE_FILE}"
echo " VIP LAN1    : ${VIP_LAN1}"
echo " VIP LAN2    : ${VIP_LAN2}"
echo " Phases cfg  : ${PHASES_CONFIG}"
echo " Fault plan  : ${FAULT_PLAN:-<none>}"
echo " Dry-run     : ${DRY_RUN}"
echo "======================================================"

trap '_write_run_status_failed; stop_fault_injector; stop_capture_service_logs; stop_capture_controller_logs; stop_poll_container_events; stop_sample_controller_stats; stop_collect_stats; cleanup_temp_controller_env' EXIT

prepare_run_outputs
"$SKIP_CLIENTS"  || run_create_clients
"$SKIP_SEED"     || run_seed
"$SKIP_SNAPSHOT" || run_snapshot
run_collect_stats
run_sample_controller_stats
run_capture_controller_logs
run_capture_service_logs
run_fault_injector
run_poll_container_events
run_traffic
stop_fault_injector
stop_poll_container_events
stop_capture_service_logs
stop_sample_controller_stats
stop_collect_stats

generate_elasticity_events
generate_policy_state
collect_rq1_artifacts
collect_rq3_artifacts
collect_research_q3_artifacts
python3 "${SCRIPT_DIR}/rq3rel_p4_rs_probe.py" --run-dir "${RUN_DIR}" --label "${RUN_LABEL:-post}" \
    || echo "  WARNING: rs probe failed" >&2
verify_rq3_run

step "Experiment complete"
echo "Results          : ${METRICS_OUTPUT}"
echo "Resource stats   : ${RESOURCE_STATS_OUTPUT}"
echo "Resource debug   : ${RESOURCE_STATS_DEBUG_OUTPUT}"
echo "Policy state     : ${POLICY_STATE_OUTPUT}"
echo "Elasticity events: ${ELASTICITY_EVENTS_OUTPUT}"
echo "Container events : ${CONTAINER_EVENTS_OUTPUT}"
echo "Phase config     : ${PHASES_SNAPSHOT_OUTPUT}"
if [[ -n "$FAULT_PLAN" ]]; then
    echo "Fault plan     : ${FAULT_PLAN_SNAPSHOT_OUTPUT}"
    echo "Fault events   : ${FAULT_EVENTS_OUTPUT}"
fi
echo "Controller logs: ${CONTROLLER_LOG_LAN1}"
echo "               : ${CONTROLLER_LOG_LAN2}"
echo "Service logs   : ${SERVICE_LOG_DIR}"
echo "RQ1 window log : ${RUN_DIR}/window_log_lan1.jsonl, ${RUN_DIR}/window_log_lan2.jsonl"
echo "RQ1 delivery   : ${RUN_DIR}/telemetry_delivery_log_lan1.csv, ${RUN_DIR}/telemetry_delivery_log_lan2.csv"
echo "RQ1 decisions  : ${RUN_DIR}/decision_log_lan1.csv, ${RUN_DIR}/decision_log_lan2.csv"
echo "RQ3 admission  : ${RUN_DIR}/admission_log_lan1.csv, ${RUN_DIR}/admission_log_lan2.csv"
echo "research_q3 rel: ${RUN_DIR}/release_log_lan1.csv, ${RUN_DIR}/release_log_lan2.csv"

_write_run_status "completed" 0 "idle"