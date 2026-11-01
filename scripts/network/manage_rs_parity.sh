#!/bin/bash

# Keep the replica set at an odd voting-member count: an even count means either a data
# node was lost (add the arbiter) or a surplus arbiter exists (remove it).

set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly OVS_CONTAINER="ovs"
readonly ARBITER_PORT=27019
readonly ARBITER_IMAGE="edge_storage_server"

declare -A LAN_BRIDGE=(       [1]="ovs-br0"                [2]="ovs-br1"                )
declare -A LAN_SUBNET=(       [1]="10.0.0"                 [2]="10.0.1"                 )
declare -A LAN_GATEWAY=(      [1]="10.0.0.1"               [2]="10.0.1.1"               )
declare -A LAN_DEFAULT_RS=(   [1]="rs_net1"                [2]="rs_net2"                )
declare -A LAN_PRIMARY=(      [1]="edge_storage_server_n1" [2]="edge_storage_server_n2" )
# Deterministic names — one arbiter per LAN, always the same container/veth
declare -A LAN_ARBITER_NAME=( [1]="arbiter_rs_net1"        [2]="arbiter_rs_net2"        )
declare -A LAN_ARBITER_VETH=( [1]="vetharb1"               [2]="vetharb2"               )
declare -A LAN_ARBITER_PEER=( [1]="vetharb1p"              [2]="vetharb2p"              )
declare -A RESERVED_SUFFIX=(  [1]="1 252 253 254"          [2]="1 252 253 254"          )

LAN=""
PRIMARY_CONTAINER=""
RS_NAME=""
DATA_PORT=27018   # port used by data members (used when resolving the primary)
PID_OVS=""


usage() {
	cat <<EOF
Usage: $SCRIPT_NAME --lan <1|2> [OPTIONS]

Ensure a MongoDB replica set always has an odd number of voting members.
Adds an arbiter when the count becomes even; removes it when it becomes odd again.

Required:
  --lan <1|2>              Target LAN

Optional:
  --primary <container>    Container used to query rs.status()
                           (default: edge_storage_server_n[LAN])
  --port <port>            MongoDB port used by data members (default: 27018)
  -h, --help               Show this help
EOF
	exit 0
}

die() {
	echo "ERROR: $*" >&2
	exit 1
}

require_jq() {
	command -v jq >/dev/null 2>&1 || die "'jq' is required but not installed."
}

ensure_ovs_namespace() {
	PID_OVS=$(docker inspect -f '{{.State.Pid}}' "$OVS_CONTAINER")
	sudo mkdir -p /var/run/netns
	sudo ln -sf "/proc/${PID_OVS}/ns/net" /var/run/netns/ovs
}


find_primary_host() {
	local primary_host
	set +e
	primary_host=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
		--port "$DATA_PORT" --eval "
try { print(db.adminCommand({ isMaster: 1 }).primary); } catch(e) { print('ERROR:' + e); }
" 2>/dev/null | tr -d '\r\n')
	set -e

	[[ -z "$primary_host" || "$primary_host" == ERROR:* ]] \
		&& die "Could not determine primary of '${RS_NAME}' via '${PRIMARY_CONTAINER}': ${primary_host}"

	echo "$primary_host"
}

rs_status_json() {
	local primary_ip="$1"
	local primary_port="$2"
	docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
		--host "$primary_ip" --port "$primary_port" \
		--eval "JSON.stringify(rs.status())" 2>/dev/null
}

count_voting_members() {
	local status_json="$1"
	echo "$status_json" | jq '[.members[] | select(.votes == 1)] | length'
}

find_arbiter_in_status() {
	local status_json="$1"
	echo "$status_json" | jq -r '[.members[] | select(.arbiterOnly == true)] | first | .name // empty'
}

# IP / MAC helpers  (self-contained mirror of add_network_storage_node.sh)

collect_used_ips() {
	local subnet="${LAN_SUBNET[$LAN]}"
	local used=()
	local suffix
	for suffix in ${RESERVED_SUFFIX[$LAN]}; do
		used+=("${subnet}.${suffix}")
	done

	local cid
	for cid in $(docker ps -q); do
		local pid
		pid=$(docker inspect -f '{{.State.Pid}}' "$cid" 2>/dev/null) || continue
		[[ "$pid" =~ ^[0-9]+$ ]] || continue
		local addrs
		addrs=$(sudo nsenter -t "$pid" -n ip -4 -o addr show 2>/dev/null \
			| grep -oE "${subnet//./\\.}\.[0-9]+" || true)
		local addr
		for addr in $addrs; do used+=("$addr"); done
	done
	printf '%s\n' "${used[@]}" | sort -u
}

auto_assign_ip() {
	local subnet="${LAN_SUBNET[$LAN]}"
	local used
	used=$(collect_used_ips)
	local host
	for host in $(seq 2 254); do
		local candidate="${subnet}.${host}"
		if ! echo "$used" | grep -qxF "$candidate"; then
			echo "$candidate"
			return 0
		fi
	done
	die "No free IP address available in ${subnet}.0/24."
}

auto_generate_mac() {
	local ip="$1"
	local host_octet="${ip##*.}"
	printf '00:00:00:00:%02x:%02x' "$LAN" "$host_octet"
}


add_arbiter() {
	local arbiter_name="${LAN_ARBITER_NAME[$LAN]}"
	local ovs_veth="${LAN_ARBITER_VETH[$LAN]}"
	local container_veth="${LAN_ARBITER_PEER[$LAN]}"
	local bridge="${LAN_BRIDGE[$LAN]}"
	local gateway="${LAN_GATEWAY[$LAN]}"
	local primary_host="$1"
	local primary_ip="${primary_host%%:*}"
	local primary_port="${primary_host##*:}"

	echo
	echo "============================================================================"
	echo "AUTO-ADDING ARBITER '${arbiter_name}' to restore odd parity"
	echo "============================================================================"

	echo "  Starting arbiter container '${arbiter_name}'..."
	docker run -dit \
		--name "$arbiter_name" \
		--network none \
		--log-opt max-size=100m --log-opt max-file=3 \
		"$ARBITER_IMAGE" \
		mongod --replSet "$RS_NAME" --bind_ip_all --port "$ARBITER_PORT"

	local arb_ip
	arb_ip=$(auto_assign_ip)
	local arb_mac
	arb_mac=$(auto_generate_mac "$arb_ip")
	echo "  Arbiter IP  : ${arb_ip}"
	echo "  Arbiter MAC : ${arb_mac}"

	ensure_ovs_namespace

	if ip link show "$ovs_veth" >/dev/null 2>&1; then
		sudo ip link del "$ovs_veth" >/dev/null 2>&1 || true
	fi

	echo "  Wiring veth pair ${ovs_veth} <-> ${container_veth}..."
	sudo ip link add "$ovs_veth" type veth peer name "$container_veth"
	sudo ip link set "$ovs_veth" netns ovs
	docker exec "$OVS_CONTAINER" ip link set "$ovs_veth" up
	docker exec "$OVS_CONTAINER" ovs-vsctl add-port "$bridge" "$ovs_veth"

	local pid
	pid=$(docker inspect -f '{{.State.Pid}}' "$arbiter_name")
	echo "  Configuring network inside '${arbiter_name}' (PID ${pid})..."
	sudo ip link set "$container_veth" netns "$pid"
	sudo nsenter -t "$pid" -n ip link set "$container_veth" name eth0
	sudo nsenter -t "$pid" -n ip link set eth0 address "$arb_mac"
	sudo nsenter -t "$pid" -n ip link set eth0 up
	sudo nsenter -t "$pid" -n ip addr add "${arb_ip}/24" dev eth0
	sudo nsenter -t "$pid" -n ip route add default via "$gateway"

	echo "  Running rs.addArb('${arb_ip}:${ARBITER_PORT}')..."
	local output
	set +e
	output=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
		--host "$primary_ip" --port "$primary_port" \
		--eval "JSON.stringify(rs.addArb('${arb_ip}:${ARBITER_PORT}'))" 2>/dev/null)
	set -e

	if ! echo "$output" | jq -e '.ok == 1' >/dev/null 2>&1; then
		echo "rs.addArb() did not return ok:1. Output:"
		printf '%s\n' "$output"
		die "rs.addArb('${arb_ip}:${ARBITER_PORT}') failed."
	fi

	echo "  Waiting for '${arbiter_name}' to reach ARBITER state..."
	local attempt
	for attempt in $(seq 1 15); do
		echo "    Status check attempt ${attempt}/15..."
		set +e
		local state
		state=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
			--host "$primary_ip" --port "$primary_port" --eval "
try {
    var s = rs.status();
    var m = s.members.find(m => m.name === '${arb_ip}:${ARBITER_PORT}');
    print(m ? m.stateStr : 'NOT_FOUND');
} catch(e) { print('ERROR:' + e); }
" 2>/dev/null | tr -d '\r\n')
		set -e
		case "$state" in
			ARBITER)
				echo "    ✅ ${arbiter_name} is ARBITER."
				break
				;;
			STARTUP*|RECOVERING|NOT_FOUND)
				echo "    Still initialising (${state}), retrying in 3s..."
				sleep 3
				;;
			*)
				echo "    Unexpected state '${state}', retrying in 3s..."
				sleep 3
				;;
		esac
		if [[ "$attempt" -eq 15 ]]; then
			die "'${arbiter_name}' did not reach ARBITER state after 15 attempts."
		fi
	done

	echo
	echo "============================================================================"
	echo "Arbiter added — parity restored"
	echo "============================================================================"
	echo "  Container   : ${arbiter_name}"
	echo "  IP          : ${arb_ip}/24"
	echo "  MAC         : ${arb_mac}"
	echo "  RS member   : ${arb_ip}:${ARBITER_PORT} (arbiterOnly)"
	echo "  Replica set : ${RS_NAME}"
	echo "  Bridge port : ${ovs_veth} (inside OVS / ${bridge})"
	echo "============================================================================"
}


remove_arbiter() {
	local arbiter_name="${LAN_ARBITER_NAME[$LAN]}"
	local ovs_veth="${LAN_ARBITER_VETH[$LAN]}"
	local bridge="${LAN_BRIDGE[$LAN]}"
	local primary_host="$1"
	local arbiter_member="$2"   # host:port from rs.status()
	local primary_ip="${primary_host%%:*}"
	local primary_port="${primary_host##*:}"

	echo
	echo "============================================================================"
	echo "AUTO-REMOVING ARBITER '${arbiter_name}' to restore odd parity"
	echo "============================================================================"

	echo "  Running rs.remove('${arbiter_member}')..."
	local output
	set +e
	output=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
		--host "$primary_ip" --port "$primary_port" \
		--eval "JSON.stringify(rs.remove('${arbiter_member}'))" 2>/dev/null)
	set -e

	if ! echo "$output" | jq -e '.ok == 1' >/dev/null 2>&1; then
		echo "rs.remove() did not return ok:1. Output:"
		printf '%s\n' "$output"
		die "rs.remove('${arbiter_member}') failed."
	fi
	echo "  rs.remove() succeeded."

	ensure_ovs_namespace
	if docker exec "$OVS_CONTAINER" ovs-vsctl list-ports "$bridge" 2>/dev/null \
			| grep -qxF "$ovs_veth"; then
		echo "  Removing OVS port ${ovs_veth} from ${bridge}..."
		docker exec "$OVS_CONTAINER" ovs-vsctl del-port "$bridge" "$ovs_veth"
	fi
	if sudo nsenter -t "$PID_OVS" -n ip link show "$ovs_veth" >/dev/null 2>&1; then
		echo "  Deleting veth pair ${ovs_veth}..."
		sudo nsenter -t "$PID_OVS" -n ip link del "$ovs_veth"
	fi

	local container_state
	container_state=$(docker inspect -f '{{.State.Status}}' "$arbiter_name" 2>/dev/null || true)
	if [[ -n "$container_state" ]]; then
		echo "  Stopping and removing container '${arbiter_name}'..."
		docker stop "$arbiter_name" >/dev/null
		docker rm   "$arbiter_name" >/dev/null
	fi

	echo
	echo "============================================================================"
	echo "Arbiter removed — parity restored"
	echo "============================================================================"
	echo "  Container   : ${arbiter_name}"
	echo "  RS member   : ${arbiter_member}"
	echo "  Replica set : ${RS_NAME}"
	echo "============================================================================"
}


main() {
	require_jq

	local primary_host
	primary_host=$(find_primary_host)
	local primary_ip="${primary_host%%:*}"
	local primary_port="${primary_host##*:}"

	echo
	echo "--- Replica set parity check (${RS_NAME}) ---"

	local status_json
	status_json=$(rs_status_json "$primary_ip" "$primary_port")

	local vote_count
	vote_count=$(count_voting_members "$status_json")
	echo "  Voting members : ${vote_count}"

	if (( vote_count % 2 == 1 )); then
		echo "  Parity         : odd — no action required."
		return 0
	fi

	echo "  Parity         : even — action required."

	local arbiter_member
	arbiter_member=$(find_arbiter_in_status "$status_json")

	if [[ -z "$arbiter_member" ]]; then
		add_arbiter "$primary_host"
	else
		remove_arbiter "$primary_host" "$arbiter_member"
	fi
}


while [[ $# -gt 0 ]]; do
	case "$1" in
		--lan)     LAN="$2";               shift 2 ;;
		--primary) PRIMARY_CONTAINER="$2"; shift 2 ;;
		--port)    DATA_PORT="$2";         shift 2 ;;
		-h|--help) usage ;;
		*)         die "Unknown option: $1" ;;
	esac
done

[[ -n "$LAN" ]] || die "--lan is required."
[[ "$LAN" == "1" || "$LAN" == "2" ]] || die "--lan must be 1 or 2."

[[ -z "$RS_NAME"           ]] && RS_NAME="${LAN_DEFAULT_RS[$LAN]}"
[[ -z "$PRIMARY_CONTAINER" ]] && PRIMARY_CONTAINER="${LAN_PRIMARY[$LAN]}"

main
