#!/bin/bash

# Each client is a netns + veth pair on the OVS bridge (no container image). Client veth
# ranges must not overlap service nodes or the controller's find_free_veth_index() runs out.

set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly OVS_CONTAINER="ovs"

declare -A LAN_BRIDGE=(   [1]="ovs-br0"  [2]="ovs-br1"  )
declare -A LAN_SUBNET=(   [1]="10.0.0"   [2]="10.0.1"   )
declare -A LAN_GATEWAY=(  [1]="10.0.0.1" [2]="10.0.1.1" )
# Client veth ranges: separate from add_network_node.sh service-node ranges (100-149, 250-299).
declare -A VETH_RANGE_START=( [1]=150 [2]=300 )
declare -A VETH_RANGE_END=(   [1]=245 [2]=395 )
# .1 = gateway, .252 = VIP_DATA recovery, .253 = VIP_SERVER, .254 = VIP_DATA_N{lan}
declare -A RESERVED_SUFFIX=( [1]="1 252 253 254" [2]="1 252 253 254" )

LAN=""
COUNT=""
PREFIX="test_client"
SYN_RETRIES=""
PID_OVS=""

usage() {
	cat <<EOF
Usage: $SCRIPT_NAME --lan <1|2> --count <N> [OPTIONS]

Create N namespace-based test clients and attach them to an OVS LAN.
Each client gets its own Linux network namespace, IP, MAC, and veth pair.

Required:
  --lan <1|2>        Target LAN
  --count <N>        Number of clients to create

Optional:
  --prefix <name>    Namespace name prefix (default: test_client)
  --syn-retries <N>  Set net.ipv4.tcp_syn_retries in each client netns (opt-in;
                     unset = kernel default, RQ2/RQ3 unchanged)
  -h, --help         Show this help

Examples:
  $SCRIPT_NAME --lan 1 --count 4
  $SCRIPT_NAME --lan 2 --count 2 --prefix bench_client
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
	[[ -n "$LAN" ]]   || die "--lan is required."
	[[ -n "$COUNT" ]] || die "--count is required."
	[[ "$LAN" == "1" || "$LAN" == "2" ]] || die "--lan must be 1 or 2."
	[[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || die "--count must be a positive integer."

	docker inspect -f '{{.State.Pid}}' "$OVS_CONTAINER" >/dev/null 2>&1 \
		|| die "Container '$OVS_CONTAINER' is not running."
	docker exec "$OVS_CONTAINER" ovs-vsctl br-exists "${LAN_BRIDGE[$LAN]}" \
		|| die "OVS bridge '${LAN_BRIDGE[$LAN]}' does not exist."
}

collect_used_ips() {
	local subnet="${LAN_SUBNET[$LAN]}"
	local used=()

	for suffix in ${RESERVED_SUFFIX[$LAN]}; do
		used+=("${subnet}.${suffix}")
	done

	local cid
	for cid in $(docker ps -q 2>/dev/null); do
		local pid
		pid=$(docker inspect -f '{{.State.Pid}}' "$cid" 2>/dev/null) || continue
		[[ "$pid" =~ ^[0-9]+$ ]] || continue
		local addrs
		addrs=$(sudo nsenter -t "$pid" -n ip -4 -o addr show 2>/dev/null \
			| grep -oE "${subnet//./\\.}\.[0-9]+" || true)
		local addr
		for addr in $addrs; do
			used+=("$addr")
		done
	done

	local ns
	while read -r ns _rest; do
		local addrs
		addrs=$(sudo ip -n "$ns" -4 -o addr show 2>/dev/null \
			| grep -oE "${subnet//./\\.}\.[0-9]+" || true)
		local addr
		for addr in $addrs; do
			used+=("$addr")
		done
	done < <(ip netns list 2>/dev/null || true)

	printf '%s\n' "${used[@]}" | sort -u
}

auto_assign_ip() {
	local subnet="${LAN_SUBNET[$LAN]}"
	local used
	used=$(collect_used_ips)

	local host
# Start from .56: .2-.55 are reserved for dynamic service nodes added by add_network_node.sh /
# add_network_storage_node.sh; .56-.151 gives 96 clients per LAN.
	for host in $(seq 56 151); do
		local candidate="${subnet}.${host}"
		if ! echo "$used" | grep -qxF "$candidate"; then
			echo "$candidate"
			return 0
		fi
	done

	die "No free IP address available in ${subnet}.56-151 (test client range)."}
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

create_client() {
	local ns_name="$1"
	local bridge="${LAN_BRIDGE[$LAN]}"
	local gateway="${LAN_GATEWAY[$LAN]}"

	if ip netns list 2>/dev/null | awk '{print $1}' | grep -qxF "$ns_name"; then
		echo "  Skipping '${ns_name}' — namespace already exists."
		return 0
	fi

	local ip
	ip=$(auto_assign_ip)
	local mac
	mac=$(auto_generate_mac "$ip")
	local veth_idx
	veth_idx=$(find_free_veth_index)

	local ovs_veth="veth${veth_idx}"
	local ns_veth="veth${veth_idx}-peer"

	echo "  Creating '${ns_name}' — IP=${ip} MAC=${mac} veth=${ovs_veth}"

	sudo ip netns add "$ns_name"
	sudo ip netns exec "$ns_name" ip link set lo up

	sudo ip link add "$ovs_veth" type veth peer name "$ns_veth"

	sudo ip link set "$ovs_veth" netns ovs
	docker exec "$OVS_CONTAINER" ip link set "$ovs_veth" up
	docker exec "$OVS_CONTAINER" ovs-vsctl add-port "$bridge" "$ovs_veth"

	sudo ip link set "$ns_veth" netns "$ns_name"
	sudo ip netns exec "$ns_name" ip link set "$ns_veth" name eth0
	sudo ip netns exec "$ns_name" ip link set eth0 address "$mac"
	sudo ip netns exec "$ns_name" ip link set eth0 up
	sudo ip netns exec "$ns_name" ip addr add "${ip}/24" dev eth0
	sudo ip netns exec "$ns_name" ip route add default via "$gateway"

# Opt-in SYN-retry ceiling: the default 6 abandons a stalled handshake at ~127 s, before the
# aiohttp 300 s cap, so edge-overload stalls showed as drops; 9 -> ~511 s instead.
	if [[ -n "$SYN_RETRIES" ]]; then
		sudo ip netns exec "$ns_name" sysctl -w net.ipv4.tcp_syn_retries="$SYN_RETRIES" >/dev/null
	fi

	# Machine-readable result — one line per client
	echo "RESULT_NS=${ns_name} RESULT_IP=${ip} RESULT_MAC=${mac}"
}

main() {
	validate_requirements
	ensure_ovs_namespace

	echo "============================================================================"
	echo "Creating ${COUNT} test client(s) on LAN ${LAN} (prefix: ${PREFIX})"
	echo "============================================================================"

	local i
	for i in $(seq 1 "$COUNT"); do
		create_client "${PREFIX}_${i}"
	done

	echo "============================================================================"
	echo "Done. Created ${COUNT} client(s) on LAN ${LAN}."
	echo "To send requests: sudo ip netns exec ${PREFIX}_1 curl http://${LAN_GATEWAY[$LAN]%.*}.100:80/"
	echo "============================================================================"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--lan)
			LAN="$2"
			shift 2
			;;
		--count)
			COUNT="$2"
			shift 2
			;;
		--prefix)
			PREFIX="$2"
			shift 2
			;;
		--syn-retries)
			SYN_RETRIES="$2"
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
