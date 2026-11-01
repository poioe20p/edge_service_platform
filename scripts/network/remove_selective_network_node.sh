#!/bin/bash

# Tier 1 containers run a standalone mongod (no replica set) and /drain stops it, so an
# exited container is a normal cleanup state and --veth may arrive pre-discovered from Phase A.

set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly OVS_CONTAINER="ovs"

declare -A LAN_BRIDGE=( [1]="ovs-br0" [2]="ovs-br1" )

LAN=""
CONTAINER_NAME=""
IFACE_NAME="eth0"
OVS_VETH=""
CONTAINER_MAC=""
CONTAINER_IP=""
KEEP_VOLUME=false
PID_OVS=""

usage() {
	cat <<EOF
Usage: $SCRIPT_NAME --lan <1|2> --name <container> [OPTIONS]

Remove a Tier 1 selective-sync container from an OVS-managed LAN.

Required:
  --lan <1|2>              Target LAN
  --name, -n <container>   Docker container name

Optional:
  --iface <name>           Interface name inside the container (default: eth0)
  --veth <veth>            OVS-side veth discovered before /drain
  --mac <mac>              Container MAC, used for stale-flow cleanup
  --ip <ip>                Container IP, used for reporting only
  --keep-volume            Do not remove the Docker data volume
  -h, --help               Show this help

Examples:
  $SCRIPT_NAME --lan 1 --name sel_sync_lan1_dyn3 --veth veth102 --mac 00:00:00:00:01:06
  $SCRIPT_NAME --lan 2 --name sel_sync_lan2_dyn4 --keep-volume
EOF
	exit 0
}

die() {
	echo "ERROR: $*" >&2
	exit 1
}

container_exists() {
	docker inspect "$CONTAINER_NAME" >/dev/null 2>&1
}

container_state() {
	docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "missing"
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

	docker inspect -f '{{.State.Pid}}' "$OVS_CONTAINER" >/dev/null 2>&1 \
		|| die "Container '$OVS_CONTAINER' is not running."

	docker exec "$OVS_CONTAINER" ovs-vsctl br-exists "${LAN_BRIDGE[$LAN]}" \
		|| die "OVS bridge '${LAN_BRIDGE[$LAN]}' does not exist."
}

discover_ovs_veth() {
	local pid="$1"

	local peer_ifindex
	peer_ifindex=$(sudo nsenter -t "$pid" -n ip link show "$IFACE_NAME" 2>/dev/null \
		| head -1 | grep -oP '@if\K[0-9]+') \
		|| die "Could not read peer ifindex from '${IFACE_NAME}' in '${CONTAINER_NAME}'."

	[[ -n "$peer_ifindex" ]] \
		|| die "Interface '${IFACE_NAME}' in '${CONTAINER_NAME}' has no peer ifindex."

	local ovs_veth
	ovs_veth=$(sudo nsenter -t "$PID_OVS" -n ip link show \
		| grep -E "^${peer_ifindex}:" | awk -F'[@: ]+' '{print $2}')

	[[ -n "$ovs_veth" ]] \
		|| die "No OVS veth found for ifindex ${peer_ifindex} in OVS netns."

	echo "$ovs_veth"
}

discover_container_mac() {
	local pid="$1"
	sudo nsenter -t "$pid" -n ip link show "$IFACE_NAME" 2>/dev/null \
		| awk '/link\/ether/{print $2}'
}

discover_data_volume() {
	local vol=""
	if container_exists; then
		vol=$(docker inspect -f \
			'{{range .Mounts}}{{if eq .Destination "/data/db"}}{{.Name}}{{end}}{{end}}' \
			"$CONTAINER_NAME")
	fi
	if [[ -z "$vol" ]] && docker volume inspect "${CONTAINER_NAME}-data" >/dev/null 2>&1; then
		vol="${CONTAINER_NAME}-data"
	fi
	echo "$vol"
}

flush_stale_mac_flows() {
	local bridge="$1"
	local mac="$2"

	echo "Flushing OVS flows for MAC ${mac} on ${bridge}..."
	docker exec "$OVS_CONTAINER" ovs-ofctl del-flows "$bridge" "dl_src=${mac}" 2>/dev/null || true
	docker exec "$OVS_CONTAINER" ovs-ofctl del-flows "$bridge" "dl_dst=${mac}" 2>/dev/null || true
}

remove_ovs_port() {
	local bridge="$1"
	local ovs_veth="$2"

	echo "Removing OVS port '${ovs_veth}' from bridge '${bridge}'..."
	docker exec "$OVS_CONTAINER" ovs-vsctl --if-exists del-port "$bridge" "$ovs_veth"

	echo "Deleting veth pair (${ovs_veth})..."
	sudo nsenter --net=/var/run/netns/ovs ip link del "$ovs_veth" 2>/dev/null \
		|| echo "  veth already removed."
}

main() {
	validate_requirements
	ensure_ovs_namespace

	local bridge="${LAN_BRIDGE[$LAN]}"
	local state="missing"
	local data_volume

	echo "============================================================================"
	echo "Removing Tier 1 selective-sync container '${CONTAINER_NAME}' from LAN ${LAN}"
	echo "============================================================================"

	data_volume=$(discover_data_volume)
	if container_exists; then
		state=$(container_state)
	fi

	if [[ -z "$OVS_VETH" && "$state" == "running" ]]; then
		local pid
		pid=$(docker inspect -f '{{.State.Pid}}' "$CONTAINER_NAME")
		echo "Discovering OVS veth for '${IFACE_NAME}' in '${CONTAINER_NAME}'..."
		OVS_VETH=$(discover_ovs_veth "$pid")
		[[ -z "$CONTAINER_MAC" ]] && CONTAINER_MAC=$(discover_container_mac "$pid")
	fi

	if [[ -z "$OVS_VETH" && "$state" != "running" ]]; then
		die "Container is ${state}; --veth is required for Tier 1 cleanup after drain."
	fi

	if [[ -n "$CONTAINER_MAC" ]]; then
		flush_stale_mac_flows "$bridge" "$CONTAINER_MAC"
	fi

	if [[ -n "$OVS_VETH" ]]; then
		remove_ovs_port "$bridge" "$OVS_VETH"
	fi

	if container_exists; then
		if [[ "$state" == "running" ]]; then
			echo "Stopping container '${CONTAINER_NAME}'..."
			docker stop --time 5 "$CONTAINER_NAME" >/dev/null 2>/dev/null || true
		fi

		echo "Removing container '${CONTAINER_NAME}'..."
		docker rm "$CONTAINER_NAME" >/dev/null
	else
		echo "Container '${CONTAINER_NAME}' already removed."
	fi

	if [[ -n "$data_volume" ]]; then
		if [[ "$KEEP_VOLUME" == "false" ]]; then
			echo "Removing data volume '${data_volume}'..."
			docker volume rm "$data_volume" >/dev/null 2>/dev/null || true
		else
			echo "Keeping data volume '${data_volume}' (--keep-volume set)."
		fi
	fi

	echo
	echo "============================================================================"
	echo "Tier 1 selective-sync node removed successfully"
	echo "============================================================================"
	echo "  Container   : ${CONTAINER_NAME}"
	echo "  LAN         : ${LAN} (${bridge})"
	[[ -n "$CONTAINER_IP" ]] && echo "  IP          : ${CONTAINER_IP}"
	[[ -n "$CONTAINER_MAC" ]] && echo "  MAC         : ${CONTAINER_MAC}"
	[[ -n "$OVS_VETH" ]] && echo "  OVS port    : ${OVS_VETH} (removed)"
	if [[ -n "$data_volume" ]]; then
		if [[ "$KEEP_VOLUME" == "false" ]]; then
			echo "  Data volume : ${data_volume} (removed)"
		else
			echo "  Data volume : ${data_volume} (kept)"
		fi
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
		--ip)
			CONTAINER_IP="$2"
			shift 2
			;;
		--keep-volume)
			KEEP_VOLUME=true
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

main