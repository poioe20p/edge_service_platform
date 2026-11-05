#!/bin/bash


set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly OVS_CONTAINER="ovs"

declare -A LAN_BRIDGE=( [1]="ovs-br0" [2]="ovs-br1" )

LAN=""
CONTAINER_NAME=""
IFACE_NAME="eth0"
PID_OVS=""
OVS_VETH=""      # pre-discovered veth from Phase A (skips nsenter if provided)
CONTAINER_MAC="" # pre-discovered MAC from Phase A (skips nsenter if provided)

usage() {
	cat <<EOF
Usage: $SCRIPT_NAME --lan <1|2> --name <container> [OPTIONS]

Detach a Docker container from an OVS-managed LAN, then stop and remove it.

Required:
  --lan <1|2>              Target LAN
  --name, -n <container>   Docker container name

Optional:
  --iface <name>           Interface name inside the container (default: eth0)
  --veth <veth>            Pre-discovered OVS-side veth (skips nsenter discovery)
  --mac <mac>              Pre-discovered container MAC (skips nsenter discovery)
  -h, --help               Show this help

Examples:
  $SCRIPT_NAME --lan 1 --name test_client_1
  $SCRIPT_NAME --lan 2 --name edge_node_n2
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

	docker inspect -f '{{.State.Pid}}' "$OVS_CONTAINER" >/dev/null 2>&1 \
		|| die "Container '$OVS_CONTAINER' is not running."

	docker exec "$OVS_CONTAINER" ovs-vsctl br-exists "${LAN_BRIDGE[$LAN]}" \
		|| die "OVS bridge '${LAN_BRIDGE[$LAN]}' does not exist."
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

# Discover the OVS-side veth name for this container's interface.
# Must be called while the container is still running (its netns must be live).
discover_ovs_veth() {
	local pid="$1"

	# The container's iface shows its peer's ifindex as "@ifN"
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

main() {
	validate_requirements
	ensure_ovs_namespace

	local bridge="${LAN_BRIDGE[$LAN]}"

	echo "============================================================================"
	echo "Removing container '${CONTAINER_NAME}' from LAN ${LAN}"
	echo "============================================================================"

	local state
	state=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME")

	local ovs_veth=""
	local pid=""
	local mac=""

	if [[ -n "$OVS_VETH" && -n "$CONTAINER_MAC" ]]; then
		# Phase B fast path: veth and MAC were pre-discovered in Phase A while
		# the container was still running.  Skip nsenter (netns may be gone).
		ovs_veth="$OVS_VETH"
		mac="$CONTAINER_MAC"
		echo "Using pre-discovered veth=${ovs_veth} mac=${mac}"
	elif [[ "$state" == "running" ]]; then
		pid=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER_NAME")

		echo "Discovering OVS veth for '${IFACE_NAME}' in '${CONTAINER_NAME}'..."
		ovs_veth=$(discover_ovs_veth "$pid")
		echo "  OVS port: ${ovs_veth}"
		mac=$(sudo nsenter -t "$pid" -n ip link show "$IFACE_NAME" 2>/dev/null \
			| awk '/link\/ether/{print $2}')
	else
		echo "  ⚠️  Container is not running (state: ${state}); veth discovery skipped." >&2
		echo "  OVS port may need manual cleanup: ovs-vsctl del-port ${bridge} <vethN>" >&2
	fi

	if [[ "$state" != "removed" ]]; then
		echo "Stopping container '${CONTAINER_NAME}'..."
		docker stop --time 5 "$CONTAINER_NAME" >/dev/null 2>/dev/null || true
	fi

	if [[ -n "$ovs_veth" ]]; then
		if [[ -n "$mac" ]]; then
			flush_stale_mac_flows "$bridge" "$mac"
		fi

		echo "Removing OVS port '${ovs_veth}' from bridge '${bridge}'..."
		docker exec "$OVS_CONTAINER" ovs-vsctl del-port "$bridge" "$ovs_veth" \
			|| echo "  ⚠️  del-port failed — port may already be gone." >&2

		echo "Deleting veth pair (${ovs_veth})..."
		sudo nsenter --net=/var/run/netns/ovs ip link del "$ovs_veth" 2>/dev/null \
			|| echo "  ⚠️  veth already removed." >&2
	fi

	echo "Removing container '${CONTAINER_NAME}'..."
	docker rm "$CONTAINER_NAME" >/dev/null

	echo
	echo "============================================================================"
	echo "Node removed successfully"
	echo "============================================================================"
	echo "  Container  : ${CONTAINER_NAME}"
	echo "  LAN        : ${LAN} (${bridge})"
	if [[ -n "$ovs_veth" ]]; then
		echo "  OVS port   : ${ovs_veth} (removed)"
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
		--veth)
			OVS_VETH="$2"
			shift 2
			;;
		--mac)
			CONTAINER_MAC="$2"
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
