#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OSKEN1_PORT=${OSKEN1_PORT:-6653}
OSKEN2_PORT=${OSKEN2_PORT:-6654}
DEFAULT_OSKEN_ENV_FILE="${SCRIPT_DIR}/osken-controller.env"
OSKEN_ENV_FILE=${OSKEN_ENV_FILE:-"${DEFAULT_OSKEN_ENV_FILE}"}
OSKEN_ENV_OVERRIDE_FILE=${OSKEN_ENV_OVERRIDE_FILE:-""}
WAN_ENV_FILE=${WAN_ENV_FILE:-"${SCRIPT_DIR}/wan.env"}
MERGED_OSKEN_ENV_FILE=""
OSKEN_ENV_BASE_RESOLVED=""
OSKEN_ENV_OVERRIDE_RESOLVED=""

resolve_path_from_script_dir() {
    local path="$1"

    if [[ -z "$path" ]]; then
        printf '%s\n' ""
        return 0
    fi

    if [[ "$path" == /* ]]; then
        printf '%s\n' "$path"
        return 0
    fi

    printf '%s\n' "${SCRIPT_DIR}/${path#./}"
}

apply_env_override_file() {
    local target_path="$1"
    local override_path="$2"
    local line=""
    local key=""
    local temp_path=""

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        if [[ "$line" != *=* ]]; then
            echo "Invalid controller env override line: ${line}" >&2
            exit 1
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

validate_env_override_file() {
    local target_path="$1"
    local override_path="$2"
    local line=""

    [[ -n "$override_path" ]] || return 0

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        if ! grep -Fxq "$line" "$target_path"; then
            echo "Controller env override was not applied: ${line}" >&2
            exit 1
        fi
    done < "$override_path"
}

print_controller_env_provenance() {
    local effective_env_path="$1"

    echo "Controller env base    : ${OSKEN_ENV_BASE_RESOLVED}"
    echo "Controller env override: ${OSKEN_ENV_OVERRIDE_RESOLVED:-<none>}"
    echo "Controller env active  : ${effective_env_path}"
}

cleanup_temp_controller_env() {
    if [[ -n "$MERGED_OSKEN_ENV_FILE" && -f "$MERGED_OSKEN_ENV_FILE" ]]; then
        rm -f "$MERGED_OSKEN_ENV_FILE"
    fi
}

prepare_osken_env_file() {
    local base_env_path=""
    local override_env_path=""

    base_env_path="$(resolve_path_from_script_dir "$OSKEN_ENV_FILE")"
    override_env_path="$(resolve_path_from_script_dir "$OSKEN_ENV_OVERRIDE_FILE")"
    OSKEN_ENV_BASE_RESOLVED="$base_env_path"
    OSKEN_ENV_OVERRIDE_RESOLVED="$override_env_path"

    if [[ ! -f "$base_env_path" ]]; then
        echo "Missing controller env file: ${base_env_path}" >&2
        echo "Expected at: ${DEFAULT_OSKEN_ENV_FILE}" >&2
        exit 1
    fi

    if [[ -z "$override_env_path" ]]; then
        OSKEN_ENV_FILE="$base_env_path"
        return 0
    fi

    if [[ ! -f "$override_env_path" ]]; then
        echo "Missing controller env override file: ${override_env_path}" >&2
        exit 1
    fi

    MERGED_OSKEN_ENV_FILE="$(mktemp)"
    cp "$base_env_path" "$MERGED_OSKEN_ENV_FILE"
    apply_env_override_file "$MERGED_OSKEN_ENV_FILE" "$override_env_path"
    validate_env_override_file "$MERGED_OSKEN_ENV_FILE" "$override_env_path"
    OSKEN_ENV_FILE="$MERGED_OSKEN_ENV_FILE"
}

trap cleanup_temp_controller_env EXIT

prepare_osken_env_file
print_controller_env_provenance "$OSKEN_ENV_FILE"

# Static edges (network/build_network_1/2.sh) read HASH_WORK_FACTOR from the environment, so a
# value present only in the controller env file would reach dynamic spawns but not them.
ENV_HASH_WORK_FACTOR="$(sed -n 's/^HASH_WORK_FACTOR=//p' "$OSKEN_ENV_FILE" | tr -d '\r' | tail -n 1)"
if [[ -n "$ENV_HASH_WORK_FACTOR" && -z "${HASH_WORK_FACTOR:-}" ]]; then
    export HASH_WORK_FACTOR="$ENV_HASH_WORK_FACTOR"
fi

# Source WAN-emulation knobs (consumed by network/inject_wan_latency.sh from
# build_router.sh). All variables default to 0 if the file is absent.
if [[ -f "${WAN_ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${WAN_ENV_FILE}"
    set +a
fi


check_mongo_ok() {
    local output="$1"
    local description="$2"
    if ! echo "$output" | grep -Eq '"ok"\s*:\s*1'; then
        # Accept "already initialized" (code 23) as success
        if echo "$output" | grep -Eq '"codeName"\s*:\s*"AlreadyInitialized"'; then
            echo "${description} already initialized (accepting as ok)."
            return 0
        fi
        echo "${description} did not return ok: 1. Output:"
        printf '%s\n' "$output"
        exit 1
    fi
}

wait_for_controller_connected() {
    local bridge="$1"
    local max_retries="${2:-30}"
    local retry_delay="${3:-2}"
    local attempt

    echo "Waiting for SDN controller to connect to ${bridge}..."
    for attempt in $(seq 1 "${max_retries}"); do
        local connected
        connected=$(docker exec ovs ovs-vsctl show 2>/dev/null \
            | awk "/Bridge ${bridge}/{found=1} found && /is_connected: true/{print; exit}")
        if [[ -n "${connected}" ]]; then
            echo "Controller connected to ${bridge}."
            return 0
        fi
        echo "  Controller not yet connected to ${bridge} (${attempt}/${max_retries})..."
        sleep "${retry_delay}"
    done

    echo "Controller failed to connect to ${bridge} after ${max_retries} attempts." >&2
    exit 1
}

ensure_rs_primary() {
    local replset_name="$1"
    local container="$2"
    local host_ip="$3"
    local port="${4:-27018}"
    local max_retries="${5:-3}"
    local retry_delay="${6:-2}"

    echo "Verifying replica set '${replset_name}' reports PRIMARY..."

    local attempt
    for attempt in $(seq 1 "${max_retries}"); do
        echo "Replica set '${replset_name}' status check attempt ${attempt}/${max_retries}..."
        set +e
        local status_json
        status_json=$(docker exec -i "${container}" mongosh --quiet --host "${host_ip}" --port "${port}" --eval "
var status;
try {
    status = rs.status();
    if (status.members && status.members.some(member => member.stateStr === 'PRIMARY')) {
        print('PRIMARY');
    } else if (status.members && status.members.length > 0) {
        print(status.members[0].stateStr);
    } else {
        print('UNKNOWN');
    }
} catch (e) {
    print('ERROR:' + e);
}
")
        local status_code=$?
        set -e

        if [[ ${status_code} -ne 0 ]]; then
            echo "Failed to run rs.status() for '${replset_name}' (exit ${status_code}). Output:"
            echo "${status_json}"
        else
            local cleaned_output
            cleaned_output=$(echo "${status_json}" | tr -d '\r\n')

            if [[ "${cleaned_output}" == ERROR:* ]]; then
                echo "Replica set '${replset_name}' not ready yet (${cleaned_output})."
            elif [[ "${cleaned_output}" == "PRIMARY" ]] || echo "${cleaned_output}" | grep -Eq '"stateStr"\s*:\s*"PRIMARY"'; then
                echo "Replica set '${replset_name}' reports PRIMARY state."
                return 0
            else
                echo "Replica set '${replset_name}' status is not PRIMARY yet. Output:"
                echo "${status_json}"
            fi
        fi

        if [[ ${attempt} -lt ${max_retries} ]]; then
            echo "Retrying in ${retry_delay}s..."
            sleep "${retry_delay}"
        fi
    done

    echo "Replica set '${replset_name}' failed to become PRIMARY after ${max_retries} attempts."
    exit 1
}


echo "Cleaning up network and Docker resources..."
bash ./cleanup.sh -v


echo "Verifying iptables FORWARD policy..."
FORWARD_POLICY=$(sudo iptables -L FORWARD | grep "Chain FORWARD" | awk '{print $4}')
if [[ "$FORWARD_POLICY" != "ACCEPT" ]]; then
    echo "FORWARD policy is not ACCEPT. Changing it to ACCEPT..."
    sudo iptables --policy FORWARD ACCEPT
fi




echo "Starting OVS container..."
docker run -dit --name ovs --privileged --restart=on-failure \
  --log-opt max-size=100m --log-opt max-file=3 \
  --cap-add=NET_ADMIN --cap-add=SYS_MODULE \
  --network host \
  -v /lib/modules:/lib/modules \
  ovs-container

sleep 1


echo "Starting NAT router container..."
docker run -dit --name nat-router --privileged --network none --restart=on-failure \
  --log-opt max-size=100m --log-opt max-file=3 ubuntu-nat-router

sleep 1


echo "Building first network (network 1)..."
bash ./network/build_network_1.sh
sleep 15  # allow MongoDB at tight CPU limits time to start


echo "Initializing replica set for edge_storage_server_n1..."
set +e
RS_STATUS_CHECK=$(docker exec -i edge_storage_server_n1 mongosh --host 10.0.0.4 --port 27018 --quiet --eval "
var status;
try {
    status = rs.status();
    if (status.members && status.members.length > 0) {
        print('ALREADY_INITIALIZED');
    } else {
        print('NOT_INITIALIZED');
    }
} catch (e) {
    if (e.codeName === 'NotYetInitialized') {
        print('NOT_INITIALIZED');
    } else {
        print('STATUS_ERROR:' + e);
    }
}
")
RS_STATUS_CODE=$?
set -e


CLEAN_RS_STATUS=$(echo "${RS_STATUS_CHECK}" | tr -d '\r\n"')
if [[ ${RS_STATUS_CODE} -eq 0 && "${CLEAN_RS_STATUS}" == "ALREADY_INITIALIZED" ]]; then
    echo "Config server replica set already initialized. Skipping rs.initiate."
else
    echo "Replica set not initialized yet. Running rs.initiate..."
    set +e
    INIT_OUTPUT=$(docker exec -i edge_storage_server_n1 mongosh --host 10.0.0.4 --port 27018 --quiet --eval "
    JSON.stringify(
    rs.initiate({
        _id: 'rs_net1',
        members: [
        { _id: 0, host: '10.0.0.4:27018' }
        ]
    })
    )
    ")
    set -e
    check_mongo_ok "${INIT_OUTPUT}" "Replica set 'rs_net1' initialization"
    echo "Initialization returned ok with value -> ${INIT_OUTPUT}."
    sleep 2

fi


echo "Building second network (network 2)..."
bash ./network/build_network_2.sh
sleep 2


echo "Waiting 15s for MongoDB to start at tight CPU limits..."
sleep 15
echo "Initializing replica set for edge_storage_server_n2..."
set +e
RS_STATUS_CHECK=$(docker exec -i edge_storage_server_n2 mongosh --quiet --host 10.0.1.4 --port 27018 --quiet --eval "
var status;
try {
    status = rs.status();
    if (status.members && status.members.length > 0) {
        print('ALREADY_INITIALIZED');
    } else {
        print('NOT_INITIALIZED');
    }
} catch (e) {
    if (e.codeName === 'NotYetInitialized') {
        print('NOT_INITIALIZED');
    } else {
        print('STATUS_ERROR:' + e);
    }
}
")
RS_STATUS_CODE=$?
set -e


CLEAN_RS_STATUS=$(echo "${RS_STATUS_CHECK}" | tr -d '\r\n"')
if [[ ${RS_STATUS_CODE} -eq 0 && "${CLEAN_RS_STATUS}" == "ALREADY_INITIALIZED" ]]; then
    echo "Edge storage server replica set already initialized. Skipping rs.initiate."
else
    set +e
    INIT_OUTPUT=$(docker exec -i edge_storage_server_n2 mongosh --quiet --host 10.0.1.4 --port 27018 --quiet --eval "
    JSON.stringify(
    rs.initiate({
        _id: 'rs_net2',
        members: [
        { _id: 0, host: '10.0.1.4:27018' }
        ]
    })
    )
    ")
    set -e
    check_mongo_ok "${INIT_OUTPUT}" "Replica set 'rs_net2' initialization"
    echo "Initialization returned ok with value -> ${INIT_OUTPUT}."
    sleep 2

fi


echo "Configuring router WAN interfaces and port-forwardings..."
bash ./network/build_router.sh
sleep 2


echo "Verifying edge_storage_server replica set statuses..."
ensure_rs_primary "rs_net1" "edge_storage_server_n1" "10.0.0.4" "27018" "15" "5"
ensure_rs_primary "rs_net2" "edge_storage_server_n2" "10.0.1.4" "27018" "15" "5"


cd "${ROOT_DIR}"
echo "Current directory for OS-Ken controller: $PWD"

echo "Starting os-ken SDN controller container..."
docker rm -f osken 2>/dev/null

docker run -dit --name osken --network host --restart=on-failure \
    --log-opt max-size=100m --log-opt max-file=3 \
    --privileged --pid=host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PWD":/workspace -w /workspace -e PYTHONPATH=/workspace \
    --env-file "${OSKEN_ENV_FILE}" \
    -e LAN_ID=lan1 \
    -e TOPOLOGY_PUB_PORT=5559 \
    -e PEER_TOPOLOGY_ENDPOINTS=tcp://127.0.0.1:5560 \
    -e COORDINATOR_STATE_PUB_PORT=5561 \
    -e SERVER_MACS="00:00:00:00:00:02" \
    -e ROUTER_MAC="00:00:00:00:00:AA" \
    -e STORAGE_CPUS="${STORAGE_CPUS:-0.15}" \
    -e EDGE_CPUS="${EDGE_CPUS:-0.30}" \
    ${TELEMETRY_SOURCE:+-e TELEMETRY_SOURCE="${TELEMETRY_SOURCE}"} \
    -e POLL_INTERVAL_S="${POLL_INTERVAL_S:-10}" \
    ${HASH_WORK_FACTOR:+-e HASH_WORK_FACTOR="${HASH_WORK_FACTOR}"} \
    osken-controller --observe-links --ofp-tcp-listen-port "${OSKEN1_PORT}" \
        --log-config-file /etc/osken/logging.conf \
        sdn_controller.main_n1

docker run -dit --name osken_2 --network host --restart=on-failure \
    --log-opt max-size=100m --log-opt max-file=3 \
    --privileged --pid=host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PWD":/workspace -w /workspace -e PYTHONPATH=/workspace \
    --env-file "${OSKEN_ENV_FILE}" \
    -e LAN_ID=lan2 \
    -e TOPOLOGY_PUB_PORT=5560 \
    -e PEER_TOPOLOGY_ENDPOINTS=tcp://127.0.0.1:5559 \
    -e COORDINATOR_STATE_PUB_PORT=5562 \
    -e SERVER_MACS="00:00:00:00:00:05" \
    -e ROUTER_MAC="00:00:00:00:00:CC" \
    -e STORAGE_CPUS="${STORAGE_CPUS:-0.15}" \
    -e EDGE_CPUS="${EDGE_CPUS:-0.30}" \
    ${TELEMETRY_SOURCE:+-e TELEMETRY_SOURCE="${TELEMETRY_SOURCE}"} \
    -e POLL_INTERVAL_S="${POLL_INTERVAL_S:-10}" \
    ${HASH_WORK_FACTOR:+-e HASH_WORK_FACTOR="${HASH_WORK_FACTOR}"} \
    osken-controller --observe-links --ofp-tcp-listen-port "${OSKEN2_PORT}" \
        --log-config-file /etc/osken/logging.conf \
        sdn_controller.main_n2

cd "${SCRIPT_DIR}"


echo "Pointing OVS switches to the SDN controllers..."
docker exec ovs ovs-vsctl set-controller ovs-br0 tcp:127.0.0.1:${OSKEN1_PORT}
docker exec ovs ovs-vsctl set-controller ovs-br1 tcp:127.0.0.1:${OSKEN2_PORT}


# VIP ARP replies come from the controller's install_vip_arp_punt_rules (priority=100); a static
# OVS flow at priority=200 here would override punt and break snoop_arp MAC->IP learning.

docker exec ovs ovs-vsctl show
echo "Build and setup of networks completed successfully."

# Ping seeding needs the OpenFlow channel first: ARP seen before that is not snooped, leaving
# the controller IP<->MAC maps empty and causing intermittent VIP routing failures.
echo "Waiting for SDN controllers to establish OpenFlow connections..."
wait_for_controller_connected "ovs-br0"
wait_for_controller_connected "ovs-br1"

sleep 5

echo "Running connectivity tests to seed controller ARP tables..."
bash "${SCRIPT_DIR}/test_conectivity.sh" all || echo "WARNING: some connectivity tests failed; ARP seeding may be incomplete."

bash ./network/clients/remove_test_clients.sh --lan 1 --prefix lan1_client
bash ./network/clients/remove_test_clients.sh --lan 2 --prefix lan2_client