#!/bin/bash


set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly OVS_CONTAINER="ovs"

declare -A LAN_BRIDGE=( [1]="ovs-br0" [2]="ovs-br1" )
declare -A LAN_SUBNET=( [1]="10.0.0" [2]="10.0.1" )
declare -A LAN_GATEWAY=( [1]="10.0.0.1" [2]="10.0.1.1" )
# Service-node veth ranges: LAN1 100-149, LAN2 250-299 (test clients use 150-245 / 300-395).
declare -A VETH_RANGE_START=( [1]=100 [2]=250 )
declare -A VETH_RANGE_END=( [1]=149 [2]=299 )
# .1 = gateway, .253 = VIP_SERVER, .254 = VIP_DATA_N{lan}
declare -A RESERVED_SUFFIX=( [1]="1 253 254" [2]="1 253 254" )

LAN=""
CONTAINER_NAME=""
IP=""
MAC=""
IFACE_NAME="eth0"
PID_OVS=""

usage() {
	cat <<EOF
Usage: $SCRIPT_NAME --lan <1|2> --name <container> [OPTIONS]

Attach an already-running Docker container to an existing LAN.

Required:
  --lan <1|2>              Target LAN
  --name, -n <container>   Running Docker container name

Optional:
  --ip <x.x.x.x>           IP address (auto-assigned if omitted)
  --mac <XX:XX:XX:XX:XX:XX>
						   MAC address (auto-generated if omitted)
  --iface <name>           Interface name inside the container (default: eth0)
  -h, --help               Show this help

Examples:
  $SCRIPT_NAME --lan 1 --name test_client_1
  $SCRIPT_NAME --lan 2 --name edge_storage_server_n2_member2 --ip 10.0.1.5
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

	local state
	state=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null) \
		|| die "Container '$CONTAINER_NAME' does not exist."

	[[ "$state" == "running" ]] \
		|| die "Container '$CONTAINER_NAME' is not running (state: ${state})."

	docker inspect -f '{{.State.Pid}}' "$OVS_CONTAINER" >/dev/null 2>&1 \
		|| die "Container '$OVS_CONTAINER' is not running."

	docker exec "$OVS_CONTAINER" ovs-vsctl br-exists "${LAN_BRIDGE[$LAN]}" \
		|| die "OVS bridge '${LAN_BRIDGE[$LAN]}' does not exist." # Check if the specified LAN bridge exists in OVS
}

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
			| grep -oE "${subnet//./\\.}\.[0-9]+" || true) # Extract IP, -4 for ipv4 and -o for one-line output

		local addr
		for addr in $addrs; do
			used+=("$addr")
		done
	done
	# Also scan named network namespaces — covers namespace-based test clients
	# created by create_test_clients.sh, which are invisible to docker ps.
	local ns
	while read -r ns _rest; do
		local addrs
		addrs=$(sudo ip -n "$ns" -4 -o addr show 2>/dev/null \
			| grep -oE "${subnet//./\.}\.[0-9]+" || true)
		local addr
		for addr in $addrs; do
			used+=("$addr")
		done
	done < <(ip netns list 2>/dev/null || true)
	printf '%s\n' "${used[@]}" | sort -u
}

validate_ip_not_taken() {
	local used
	used=$(collect_used_ips)

	if echo "$used" | grep -qxF "$IP"; then
		die "IP $IP is already in use on LAN ${LAN}."
	fi
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

find_free_veth_index() {
	local start="${VETH_RANGE_START[$LAN]}"
	local end="${VETH_RANGE_END[$LAN]}"
	local idx

	for idx in $(seq "$start" "$end"); do
		if ! ip link show "veth${idx}" >/dev/null 2>&1 \
			&& ! sudo nsenter -t "$PID_OVS" -n ip link show "veth${idx}" >/dev/null 2>&1; then
			echo "$idx"
			return 0
		fi
	done

	die "No free veth index in range ${start}-${end} for LAN ${LAN}."
}

cleanup_stale_veth() {
	local ovs_veth="$1"

	if ip link show "$ovs_veth" >/dev/null 2>&1; then
		sudo ip link del "$ovs_veth" >/dev/null 2>&1 || true
	fi
}

# Remove all OVS flows referencing this MAC before del-port: flows pointing at the
# outgoing port would otherwise swallow traffic when the MAC is re-attached elsewhere.
flush_stale_mac_flows() {
	local bridge="$1"
	local mac="$2"

	echo "Flushing stale OVS flows for MAC ${mac} on ${bridge}..."
	docker exec "$OVS_CONTAINER" ovs-ofctl del-flows "$bridge" "dl_src=${mac}" 2>/dev/null || true
	docker exec "$OVS_CONTAINER" ovs-ofctl del-flows "$bridge" "dl_dst=${mac}" 2>/dev/null || true
}

main() {
	validate_requirements
	ensure_ovs_namespace

	local bridge="${LAN_BRIDGE[$LAN]}"
	local gateway="${LAN_GATEWAY[$LAN]}"

	echo "============================================================================"
	echo "Attaching container '${CONTAINER_NAME}' to LAN ${LAN}"
	echo "============================================================================"

	if [[ -n "$IP" && -n "$MAC" ]]; then
		echo "Using pre-assigned IP=${IP} MAC=${MAC} — skipping auto-assignment"
	else
		if [[ -z "$IP" ]]; then
			IP=$(auto_assign_ip)
			echo "Auto-assigned IP: ${IP}"
		else
			validate_ip_not_taken
		fi

		if [[ -z "$MAC" ]]; then
			MAC=$(auto_generate_mac "$IP")
			echo "Auto-generated MAC: ${MAC}"
		fi
	fi

	local veth_idx
	veth_idx=$(find_free_veth_index)

	local ovs_veth="veth${veth_idx}"
	local container_veth="veth${veth_idx}-peer"

	echo "Using veth pair: ${ovs_veth} <-> ${container_veth}"

	cleanup_stale_veth "$ovs_veth"

	echo "Creating veth pair..."
	sudo ip link add "$ovs_veth" type veth peer name "$container_veth"

	echo "Attaching ${ovs_veth} to OVS bridge ${bridge}..."
	sudo ip link set "$ovs_veth" netns ovs
	docker exec "$OVS_CONTAINER" ip link set "$ovs_veth" up
	docker exec "$OVS_CONTAINER" ovs-vsctl add-port "$bridge" "$ovs_veth"

	flush_stale_mac_flows "$bridge" "$MAC"

	local pid
	pid=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER_NAME")

	echo "Moving ${container_veth} into container namespace (PID ${pid})..."
	sudo ip link set "$container_veth" netns "$pid"

	echo "Configuring ${IFACE_NAME} inside '${CONTAINER_NAME}'..."
	sudo nsenter -t "$pid" -n ip link set "$container_veth" name "$IFACE_NAME"
	sudo nsenter -t "$pid" -n ip link set "$IFACE_NAME" address "$MAC"
	sudo nsenter -t "$pid" -n ip link set "$IFACE_NAME" up
	sudo nsenter -t "$pid" -n ip addr add "${IP}/24" dev "$IFACE_NAME"
	sudo nsenter -t "$pid" -n ip route add default via "$gateway"

	echo
	echo "============================================================================"
	echo "Node added successfully"
	echo "============================================================================"
	echo "  Container      : ${CONTAINER_NAME}"
	echo "  LAN            : ${LAN} (${bridge})"
	echo "  IP             : ${IP}/24"
	echo "  MAC            : ${MAC}"
	echo "  Gateway        : ${gateway}"
	echo "  Switch port    : ${ovs_veth} (inside OVS / ${bridge})"
	echo "  Container link : ${container_veth} -> ${IFACE_NAME} (inside ${CONTAINER_NAME})"
	echo "============================================================================"
	# Machine-readable summary — parsed by the Python controller (NodeAdder)
	echo "RESULT_IP=${IP}"
	echo "RESULT_MAC=${MAC}"
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
		--ip)
			IP="$2"
			shift 2
			;;
		--mac)
			MAC="$2"
			shift 2
			;;
		--iface)
			IFACE_NAME="$2"
			shift 2
			;;
		-h|--help)
			usage
			;;
		*)
			die "Unknown option: $1"
			;;
	esac
done

main
