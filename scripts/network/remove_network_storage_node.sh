#!/bin/bash


set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly OVS_CONTAINER="ovs"

declare -A LAN_BRIDGE=( [1]="ovs-br0" [2]="ovs-br1" )
declare -A LAN_SUBNET=( [1]="10.0.0" [2]="10.0.1" )
declare -A LAN_DEFAULT_RS=( [1]="rs_net1" [2]="rs_net2" )
declare -A LAN_DEFAULT_PRIMARY_CONTAINER=( [1]="edge_storage_server_n1" [2]="edge_storage_server_n2" )

LAN=""
CONTAINER_NAME=""
IFACE_NAME="eth0"
PORT=27018
RS_NAME=""
PRIMARY_CONTAINER=""
KEEP_VOLUME=false
SKIP_RS=false    # skip rs.remove() step (controller already handled it in Python)
PID_OVS=""

usage() {
	cat <<EOF
Usage: $SCRIPT_NAME --lan <1|2> --name <container> [OPTIONS]

Remove a MongoDB container from its replica set, detach it from the OVS LAN,
then stop and remove the container (and optionally its data volume).

Required:
  --lan <1|2>                  Target LAN
  --name, -n <container>       Docker container name

Optional:
  --iface <name>               Interface name inside the container (default: eth0)
  --port <port>                MongoDB port (default: 27018)
  --rs-name <name>             Replica set name (default: rs_net1 / rs_net2 based on LAN)
  --primary <container>        Container to run rs.remove() on (default: edge_storage_server_n[LAN])
  --keep-volume                Do not remove the Docker data volume (default: remove it)
  --skip-rs                    Skip rs.remove() step (controller already ran it in Python)
  -h, --help                   Show this help

Notes:
  - If the container being removed is the current primary, step it down first:
      docker exec <container> mongosh --port <port> --eval "rs.stepDown()"
  - RS removal happens before container stop to avoid unnecessary elections.

Examples:
  $SCRIPT_NAME --lan 1 --name edge_storage_server_n1_member2
  $SCRIPT_NAME --lan 2 --name edge_storage_server_n2_member2 --keep-volume
EOF
	exit 0
}

die() {
	echo "ERROR: $*" >&2
	exit 1
}

ensure_ovs_namespace() {
	PID_OVS=$(docker inspect -f '{{.State.Pid}}' "$OVS_CONTAINER")
	sudo mkdir -p /var/run/netns
	sudo ln -sf "/proc/${PID_OVS}/ns/net" /var/run/netns/ovs
}

validate_requirements() {
	[[ -n "$LAN" ]] || die "--lan is required."
	[[ "$LAN" == "1" || "$LAN" == "2" ]] || die "--lan must be 1 or 2."
	[[ -n "$CONTAINER_NAME" ]] || die "--name is required."

	docker inspect "$CONTAINER_NAME" >/dev/null 2>&1 \
		|| die "Container '$CONTAINER_NAME' does not exist."

	local state
	state=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME")
	[[ "$state" == "running" ]] \
		|| die "Container '$CONTAINER_NAME' is not running (state: ${state}). RS removal requires the container to be up."

	if [[ "$SKIP_RS" == "false" ]]; then
		state=$(docker inspect -f '{{.State.Status}}' "$PRIMARY_CONTAINER" 2>/dev/null) \
			|| die "Primary container '$PRIMARY_CONTAINER' does not exist."
		[[ "$state" == "running" ]] \
			|| die "Primary container '$PRIMARY_CONTAINER' is not running (state: ${state})."
	fi

	docker inspect -f '{{.State.Pid}}' "$OVS_CONTAINER" >/dev/null 2>&1 \
		|| die "Container '$OVS_CONTAINER' is not running."

	docker exec "$OVS_CONTAINER" ovs-vsctl br-exists "${LAN_BRIDGE[$LAN]}" \
		|| die "OVS bridge '${LAN_BRIDGE[$LAN]}' does not exist."
}

# Discover the OVS-side veth name for this container's interface.
# Must be called while the container is still running (its netns must be live).
discover_ovs_veth() {
	local pid="$1"

	local peer_ifindex
	peer_ifindex=$(sudo nsenter -t "$pid" -n ip link show "$IFACE_NAME" 2>/dev/null \
		| head -1 | grep -oP '@if\K[0-9]+') \
		|| die "Could not read peer ifindex from '${IFACE_NAME}' in '${CONTAINER_NAME}'."

	[[ -n "$peer_ifindex" ]] \
		|| die "Interface '${IFACE_NAME}' in '${CONTAINER_NAME}' has no peer ifindex — already detached?"

	local ovs_veth
	ovs_veth=$(sudo nsenter -t "$PID_OVS" -n ip link show \
		| grep -E "^${peer_ifindex}:" | awk -F'[@: ]+' '{print $2}')

	[[ -n "$ovs_veth" ]] \
		|| die "No OVS veth found for ifindex ${peer_ifindex} in OVS netns."

	echo "$ovs_veth"
}

discover_container_ip() {
	local pid="$1"
	local subnet="${LAN_SUBNET[$LAN]}"

	local ip
	ip=$(sudo nsenter -t "$pid" -n ip -4 -o addr show "$IFACE_NAME" 2>/dev/null \
		| grep -oE "${subnet//./\\.}\.[0-9]+") \
		|| die "Could not read IP from '${IFACE_NAME}' in '${CONTAINER_NAME}'."

	[[ -n "$ip" ]] \
		|| die "No IP in subnet ${subnet}.0/24 found on '${IFACE_NAME}' in '${CONTAINER_NAME}'."

	echo "$ip"
}

# Remove all OVS flows referencing this MAC before del-port: flows pointing at the
# outgoing port would otherwise swallow traffic when the MAC is re-attached elsewhere.
flush_stale_mac_flows() {
	local bridge="$1"
	local mac="$2"

	echo "Flushing OVS flows for MAC ${mac} on ${bridge}..."
	docker exec "$OVS_CONTAINER" ovs-ofctl del-flows "$bridge" "dl_src=${mac}" 2>/dev/null || true
	docker exec "$OVS_CONTAINER" ovs-ofctl del-flows "$bridge" "dl_dst=${mac}" 2>/dev/null || true
}

discover_data_volume() {
	local vol
	vol=$(docker inspect -f \
		'{{range .Mounts}}{{if eq .Destination "/data/db"}}{{.Name}}{{end}}{{end}}' \
		"$CONTAINER_NAME")
	echo "$vol"
}

find_primary_host() {
	local primary_host
	set +e
	primary_host=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet --port "$PORT" --eval "
try {
    var m = db.adminCommand({ isMaster: 1 });
    print(m.primary);
} catch(e) {
    print('ERROR:' + e);
}
" 2>/dev/null | tr -d '\r\n')
	set -e

	[[ -z "$primary_host" || "$primary_host" == ERROR:* ]] \
		&& die "Could not determine primary of '${RS_NAME}' via '${PRIMARY_CONTAINER}': ${primary_host}"

	echo "$primary_host"
}

rs_remove_member() {
	local member_host="$1"  # IP:PORT of the member to remove
	local primary_host="$2"
	local primary_ip="${primary_host%%:*}"
	local primary_port="${primary_host##*:}"

	echo "Running rs.remove('${member_host}') via primary ${primary_host}..."
	set +e
	local output
	output=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
		--host "$primary_ip" --port "$primary_port" --eval "
JSON.stringify(rs.remove('${member_host}'))
" 2>/dev/null)
	set -e

	if ! echo "$output" | grep -Eq '"ok"\s*:\s*1'; then
		echo "rs.remove() did not return ok:1. Output:"
		printf '%s\n' "$output"
		die "rs.remove('${member_host}') failed."
	fi

	echo "rs.remove('${member_host}') succeeded."
}

ensure_rs_removed() {
	local member_host="$1"  # IP:PORT of the removed member
	local primary_host="$2"
	local primary_ip="${primary_host%%:*}"
	local primary_port="${primary_host##*:}"
	local max_retries="${3:-10}"
	local retry_delay="${4:-3}"

	echo "Waiting for '${member_host}' to be removed from replica set..."

	local attempt
	for attempt in $(seq 1 "${max_retries}"); do
		echo "  Check attempt ${attempt}/${max_retries}..."
		set +e
		local found
		found=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
			--host "$primary_ip" --port "$primary_port" --eval "
try {
    var status = rs.status();
    var member = status.members.find(m => m.name === '${member_host}');
    print(member ? 'FOUND' : 'REMOVED');
} catch(e) {
    print('ERROR:' + e);
}
" 2>/dev/null | tr -d '\r\n')
		set -e

		case "$found" in
			REMOVED)
				echo "  ✅ ${member_host} is no longer in the replica set."
				return 0
				;;
			FOUND)
				echo "  Still visible in rs.status(), retrying in ${retry_delay}s..."
				;;
			ERROR:*)
				echo "  rs.status() error: ${found}, retrying in ${retry_delay}s..."
				;;
			*)
				echo "  Unexpected result '${found}', retrying in ${retry_delay}s..."
				;;
		esac

		sleep "${retry_delay}"
	done

	die "'${member_host}' still appears in replica set after ${max_retries} attempts."
}

get_rs_members() {
	local primary_host="$1"
	local primary_ip="${primary_host%%:*}"
	local primary_port="${primary_host##*:}"

	set +e
	local output
	output=$(docker exec -i "$PRIMARY_CONTAINER" mongosh --quiet \
		--host "$primary_ip" --port "$primary_port" --eval "
var status = rs.status();
status.members.forEach(function(m) { print(m.name + ' ' + m.stateStr); });
" 2>/dev/null)
	set -e

	echo "$output"
}

main() {
	validate_requirements
	ensure_ovs_namespace

	local bridge="${LAN_BRIDGE[$LAN]}"

	echo "============================================================================"
	echo "Removing storage container '${CONTAINER_NAME}' from LAN ${LAN}"
	echo "============================================================================"

	local pid
	pid=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER_NAME")

	echo "Discovering OVS veth for '${IFACE_NAME}' in '${CONTAINER_NAME}'..."
	local ovs_veth
	ovs_veth=$(discover_ovs_veth "$pid")
	echo "  OVS port: ${ovs_veth}"

	local container_ip
	container_ip=$(discover_container_ip "$pid")
	echo "  Container IP: ${container_ip}"

	local mac
	mac=$(sudo nsenter -t "$pid" -n ip link show "$IFACE_NAME" 2>/dev/null \
		| awk '/link\/ether/{print $2}')

	local member_host="${container_ip}:${PORT}"

	local data_volume
	data_volume=$(discover_data_volume)
	if [[ -n "$data_volume" ]]; then
		echo "  Data volume: ${data_volume}"
	else
		echo "  ⚠️  No named volume found at /data/db — nothing to remove." >&2
	fi

	local rs_members=""
	if [[ "$SKIP_RS" == "false" ]]; then
		echo
		echo "============================================================================"
		echo "Removing '${member_host}' from replica set '${RS_NAME}'"
		echo "============================================================================"

		local primary_host
		primary_host=$(find_primary_host)
		echo "  Primary: ${primary_host}"

		rs_remove_member "$member_host" "$primary_host"
		ensure_rs_removed "$member_host" "$primary_host"

		rs_members=$(get_rs_members "$primary_host")
	else
		echo "[--skip-rs] rs.remove() already handled by controller — skipping."
	fi

	echo
	echo "============================================================================"
	echo "Tearing down network"
	echo "============================================================================"

	if [[ "$SKIP_RS" == "true" ]]; then
# Flush DNAT flows BEFORE stopping the container: this actively breaks any surviving
# data-plane connections, so no client can reach this mongod once rs.remove() dropped it.
		if [[ -n "$mac" ]]; then
			flush_stale_mac_flows "$bridge" "$mac"
		fi
	fi

	echo "Stopping container '${CONTAINER_NAME}'..."
# --time 15 covers MongoDB's SIGTERM quiesce; after the flow flush and rs.remove() there
# are no clients and no RS obligations, so the quiesce completes almost instantly.
	docker stop --time 15 "$CONTAINER_NAME" >/dev/null

	if [[ "$SKIP_RS" == "false" ]]; then
		# Normal path: flush flows after container stop (existing behaviour).
		if [[ -n "$mac" ]]; then
			flush_stale_mac_flows "$bridge" "$mac"
		fi
	fi

	echo "Removing OVS port '${ovs_veth}' from bridge '${bridge}'..."
	docker exec "$OVS_CONTAINER" ovs-vsctl del-port "$bridge" "$ovs_veth" \
		|| echo "  ⚠️  del-port failed — port may already be gone." >&2

	echo "Deleting veth pair (${ovs_veth})..."
	sudo nsenter --net=/var/run/netns/ovs ip link del "$ovs_veth" 2>/dev/null \
		|| echo "  ⚠️  veth already removed." >&2

	echo "Removing container '${CONTAINER_NAME}'..."
	docker rm "$CONTAINER_NAME" >/dev/null

	if [[ -n "$data_volume" ]]; then
		if [[ "$KEEP_VOLUME" == "false" ]]; then
			echo "Removing data volume '${data_volume}'..."
			docker volume rm "$data_volume" >/dev/null
		else
			echo "Keeping data volume '${data_volume}' (--keep-volume set)."
		fi
	fi

	echo
	echo "============================================================================"
	echo "Storage node removed successfully"
	echo "============================================================================"
	echo "  Container      : ${CONTAINER_NAME}"
	echo "  LAN            : ${LAN} (${bridge})"
	echo "  OVS port       : ${ovs_veth} (removed)"
	echo "  Member removed : ${member_host}"
	echo "  Replica set    : ${RS_NAME}"
	if [[ -n "$data_volume" ]]; then
		if [[ "$KEEP_VOLUME" == "false" ]]; then
			echo "  Data volume    : ${data_volume} (removed)"
		else
			echo "  Data volume    : ${data_volume} (kept)"
		fi
	fi
	echo "  Remaining members:"
	if [[ -n "$rs_members" ]]; then
		while IFS= read -r line; do
			[[ -z "$line" ]] && continue
			local host state
			host="${line%% *}"
			state="${line##* }"
			printf '    %-25s %s\n' "$host" "$state"
		done <<< "$rs_members"
	else
		echo "    ⚠️  Could not retrieve member list"
	fi
	echo "============================================================================"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--lan)
			LAN="$2"
			shift 2
			;;
		--name|-n)
			CONTAINER_NAME="$2"
			shift 2
			;;
		--iface)
			IFACE_NAME="$2"
			shift 2
			;;
		--port)
			PORT="$2"
			shift 2
			;;
		--rs-name)
			RS_NAME="$2"
			shift 2
			;;
		--primary)
			PRIMARY_CONTAINER="$2"
			shift 2
			;;
		--keep-volume)
			KEEP_VOLUME=true
			shift
			;;
		--skip-rs)
			SKIP_RS=true
			shift
			;;
		-h|--help)
			usage
			;;
		*)
			die "Unknown option: $1"
			;;
	esac
done

# Apply defaults that depend on LAN (must be after arg parsing)
[[ -n "$LAN" ]] || die "--lan is required."
[[ -z "$RS_NAME" ]] && RS_NAME="${LAN_DEFAULT_RS[$LAN]}"
[[ -z "$PRIMARY_CONTAINER" ]] && PRIMARY_CONTAINER="${LAN_DEFAULT_PRIMARY_CONTAINER[$LAN]}"

main
