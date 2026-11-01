#!/bin/bash

# ============================================================================
# capture_reverse_hotspot_probe.sh
#
# Read-only helper for the reverse_hotspot probe run. Waits for the active
# run to reach reverse_hotspot, then collects packet captures, OVS flow
# snapshots, and conntrack state into a user-owned capture directory.
# ============================================================================

set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly DEFAULT_CLIENT_NS="lan2_client_1"
readonly DEFAULT_OVS_CONTAINER="ovs"
readonly DEFAULT_CLIENT_FILTER="host 10.0.0.253 or host 10.0.1.254 or host 10.0.1.252"
readonly DEFAULT_HOST_FILTER="tcp port 27018 and (host 10.0.1.254 or host 10.0.1.252 or net 10.0.1.0/24)"
readonly DEFAULT_CAPTURE_SECONDS="240"
readonly DEFAULT_SAMPLE_SECONDS="210"
readonly DEFAULT_POLL_INTERVAL="5"

RUN_DIR=""
CAPTURE_ROOT=""
CLIENT_NS="$DEFAULT_CLIENT_NS"
OVS_CONTAINER="$DEFAULT_OVS_CONTAINER"
CLIENT_FILTER="$DEFAULT_CLIENT_FILTER"
HOST_FILTER="$DEFAULT_HOST_FILTER"
CAPTURE_SECONDS="$DEFAULT_CAPTURE_SECONDS"
SAMPLE_SECONDS="$DEFAULT_SAMPLE_SECONDS"
POLL_INTERVAL="$DEFAULT_POLL_INTERVAL"
PREFLIGHT_ONLY=false

CLIENT_PCAP_PID=""
HOST_PCAP_PID=""

usage() {
    cat <<EOF
Usage:
  $SCRIPT_NAME --preflight --capture-root <path> [options]
  $SCRIPT_NAME --run-dir <path> --capture-root <path> [options]

Options:
  --run-dir PATH              Active metrics run directory to watch
  --capture-root PATH         User-owned output directory for probe artifacts
  --client-ns NAME            Client namespace to capture (default: $DEFAULT_CLIENT_NS)
  --ovs-container NAME        OVS container name (default: $DEFAULT_OVS_CONTAINER)
  --client-filter EXPR        tcpdump filter for namespace capture
  --host-filter EXPR          tcpdump filter for host capture
  --capture-seconds N         Packet-capture timeout in seconds (default: $DEFAULT_CAPTURE_SECONDS)
  --sample-seconds N          Flow/conntrack sampling window in seconds (default: $DEFAULT_SAMPLE_SECONDS)
  --poll-interval N           Sampling interval in seconds (default: $DEFAULT_POLL_INTERVAL)
  --preflight                 Validate prerequisites and exit without waiting for phases
  -h, --help                  Show this help

Examples:
  $SCRIPT_NAME --preflight \
    --capture-root "\$HOME/probe_captures/preflight_reverse_hotspot_probe"

  nohup bash source/scripts/testing/$SCRIPT_NAME \
    --run-dir "source/scripts/testing/metrics/<run_id>" \
    --capture-root "\$HOME/probe_captures/<run_id>" \
    > "\$HOME/probe_captures/<run_id>/helper.log" 2>&1 < /dev/null &
EOF
}

log() {
    printf '[probe] %s %s\n' "$(date -Is)" "$*"
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$CLIENT_PCAP_PID" ]]; then
        kill "$CLIENT_PCAP_PID" >/dev/null 2>&1 || true
    fi

    if [[ -n "$HOST_PCAP_PID" ]]; then
        kill "$HOST_PCAP_PID" >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT

have_root_privileges() {
    [[ "${EUID:-$(id -u)}" -eq 0 ]]
}

run_privileged() {
    if have_root_privileges; then
        "$@"
    else
        sudo -n "$@"
    fi
}

require_positive_int() {
    local name="$1"
    local value="$2"

    [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "invalid ${name} '${value}' (expected positive integer)"
}

require_command() {
    local command_name="$1"

    command -v "$command_name" >/dev/null 2>&1 || die "required command not found: $command_name"
}

validate_common_args() {
    [[ -n "$CAPTURE_ROOT" ]] || die "--capture-root is required"

    require_positive_int "--capture-seconds" "$CAPTURE_SECONDS"
    require_positive_int "--sample-seconds" "$SAMPLE_SECONDS"
    require_positive_int "--poll-interval" "$POLL_INTERVAL"

    if [[ -n "$RUN_DIR" ]]; then
        case "$CAPTURE_ROOT" in
            "$RUN_DIR"|"$RUN_DIR"/*)
                die "--capture-root must be outside --run-dir"
                ;;
        esac
    fi
}

check_namespace() {
    ip netns list | awk '{print $1}' | grep -Fx "$CLIENT_NS" >/dev/null 2>&1 \
        || die "client namespace not found: $CLIENT_NS"
}

check_ovs_container() {
    docker ps --format '{{.Names}}' | grep -Fx "$OVS_CONTAINER" >/dev/null 2>&1 \
        || die "OVS container not running: $OVS_CONTAINER"
}

check_sudo_prereqs() {
    if have_root_privileges; then
        timeout 1 tcpdump --version >/dev/null 2>&1 \
            || die "root preflight cannot run tcpdump"

        if ! conntrack -L -p tcp >/dev/null 2>&1; then
            conntrack -L >/dev/null 2>&1 \
                || die "root preflight cannot run conntrack"
        fi

        ip netns exec "$CLIENT_NS" true >/dev/null 2>&1 \
            || die "root preflight cannot enter namespace: $CLIENT_NS"
        return 0
    fi

    sudo -n timeout 1 tcpdump --version >/dev/null 2>&1 \
        || die "non-interactive sudo cannot run tcpdump; rerun the helper with sudo or extend sudo -n coverage"

    if ! sudo -n conntrack -L -p tcp >/dev/null 2>&1; then
        sudo -n conntrack -L >/dev/null 2>&1 \
            || die "non-interactive sudo cannot run conntrack; rerun the helper with sudo or extend sudo -n coverage"
    fi

    sudo -n ip netns exec "$CLIENT_NS" true >/dev/null 2>&1 \
        || die "non-interactive sudo cannot enter namespace: $CLIENT_NS; rerun the helper with sudo or extend sudo -n coverage"
}

prepare_capture_root() {
    mkdir -p "$CAPTURE_ROOT/flows" "$CAPTURE_ROOT/pcap" "$CAPTURE_ROOT/conntrack"
}

preflight() {
    validate_common_args
    require_command docker
    require_command ip
    require_command timeout
    require_command tcpdump
    require_command conntrack

    if ! have_root_privileges; then
        require_command sudo
    fi

    check_namespace
    check_ovs_container
    prepare_capture_root
    check_sudo_prereqs

    docker exec "$OVS_CONTAINER" ovs-ofctl dump-flows ovs-br0 >/dev/null 2>&1 \
        || die "ovs-ofctl dump-flows ovs-br0 failed in container: $OVS_CONTAINER"
    docker exec "$OVS_CONTAINER" ovs-ofctl dump-flows ovs-br1 >/dev/null 2>&1 \
        || die "ovs-ofctl dump-flows ovs-br1 failed in container: $OVS_CONTAINER"

    if have_root_privileges; then
        log "preflight ok mode=root"
    else
        log "preflight ok mode=sudo-n"
    fi

}

wait_for_reverse_hotspot() {
    local phase_file="$RUN_DIR/current_phase.txt"
    local last_phase=""
    local phase_now=""

    log "watching phase marker $phase_file"

    while true; do
        phase_now="$(cat "$phase_file" 2>/dev/null || true)"

        if [[ "$phase_now" != "$last_phase" && -n "$phase_now" ]]; then
            log "phase=$phase_now"
            last_phase="$phase_now"
        fi

        case "$phase_now" in
            reverse_hotspot)
                log "reverse_hotspot reached"
                return 0
                ;;
            idle)
                die "phase marker reached idle before reverse_hotspot"
                ;;
        esac

        sleep 1
    done
}

start_packet_captures() {
    local client_pcap="$CAPTURE_ROOT/pcap/${CLIENT_NS}_reverse_hotspot.pcap"
    local host_pcap="$CAPTURE_ROOT/pcap/host_backend_reverse_hotspot.pcap"

    run_privileged timeout "$CAPTURE_SECONDS" ip netns exec "$CLIENT_NS" \
        tcpdump -ni eth0 -s 0 -w "$client_pcap" "$CLIENT_FILTER" >/dev/null 2>&1 &
    CLIENT_PCAP_PID=$!

    run_privileged timeout "$CAPTURE_SECONDS" \
        tcpdump -ni any -s 0 -w "$host_pcap" "$HOST_FILTER" >/dev/null 2>&1 &
    HOST_PCAP_PID=$!

    sleep 1

    kill -0 "$CLIENT_PCAP_PID" >/dev/null 2>&1 \
        || die "client namespace tcpdump exited immediately"
    kill -0 "$HOST_PCAP_PID" >/dev/null 2>&1 \
        || die "host tcpdump exited immediately"
}

capture_snapshots() {
    local end_ts=""
    local ts=""

    end_ts=$(( $(date +%s) + SAMPLE_SECONDS ))

    while [[ $(date +%s) -lt $end_ts ]]; do
        ts="$(date +%Y%m%d_%H%M%S)"

        docker exec "$OVS_CONTAINER" ovs-ofctl dump-flows ovs-br0 > \
            "$CAPTURE_ROOT/flows/ovs-br0_${ts}.txt" 2>/dev/null || true
        docker exec "$OVS_CONTAINER" ovs-ofctl dump-flows ovs-br1 > \
            "$CAPTURE_ROOT/flows/ovs-br1_${ts}.txt" 2>/dev/null || true
        run_privileged conntrack -L -p tcp > \
            "$CAPTURE_ROOT/conntrack/tcp_${ts}.txt" 2>/dev/null || true

        sleep "$POLL_INTERVAL"
    done
}

write_run_pointer() {
    printf '%s\n' "$RUN_DIR" > "$CAPTURE_ROOT/run_dir.txt"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --run-dir)
            shift
            RUN_DIR="$1"
            ;;
        --run-dir=*)
            RUN_DIR="${1#*=}"
            ;;
        --capture-root)
            shift
            CAPTURE_ROOT="$1"
            ;;
        --capture-root=*)
            CAPTURE_ROOT="${1#*=}"
            ;;
        --client-ns)
            shift
            CLIENT_NS="$1"
            ;;
        --client-ns=*)
            CLIENT_NS="${1#*=}"
            ;;
        --ovs-container)
            shift
            OVS_CONTAINER="$1"
            ;;
        --ovs-container=*)
            OVS_CONTAINER="${1#*=}"
            ;;
        --client-filter)
            shift
            CLIENT_FILTER="$1"
            ;;
        --client-filter=*)
            CLIENT_FILTER="${1#*=}"
            ;;
        --host-filter)
            shift
            HOST_FILTER="$1"
            ;;
        --host-filter=*)
            HOST_FILTER="${1#*=}"
            ;;
        --capture-seconds)
            shift
            CAPTURE_SECONDS="$1"
            ;;
        --capture-seconds=*)
            CAPTURE_SECONDS="${1#*=}"
            ;;
        --sample-seconds)
            shift
            SAMPLE_SECONDS="$1"
            ;;
        --sample-seconds=*)
            SAMPLE_SECONDS="${1#*=}"
            ;;
        --poll-interval)
            shift
            POLL_INTERVAL="$1"
            ;;
        --poll-interval=*)
            POLL_INTERVAL="${1#*=}"
            ;;
        --preflight)
            PREFLIGHT_ONLY=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
    shift
done

preflight

if [[ "$PREFLIGHT_ONLY" == true ]]; then
    exit 0
fi

[[ -n "$RUN_DIR" ]] || die "--run-dir is required unless --preflight is used"

prepare_capture_root
write_run_pointer
if have_root_privileges; then
    log "helper started mode=root"
else
    log "helper started mode=sudo-n"
fi
wait_for_reverse_hotspot
start_packet_captures
capture_snapshots
wait "$CLIENT_PCAP_PID" 2>/dev/null || true
CLIENT_PCAP_PID=""
wait "$HOST_PCAP_PID" 2>/dev/null || true
HOST_PCAP_PID=""
log "debug capture complete"