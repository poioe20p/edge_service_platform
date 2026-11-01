#!/bin/bash


set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_NAME=$(basename "$0")

log()   { printf '[INFO] %s\n' "$*"; }
warn()  { printf '[WARN] %s\n' "$*" 1>&2; }
error() { printf '[ERROR] %s\n' "$*" 1>&2; }

cleanup_trap() {
	local ec=$?
	if [[ $ec -ne 0 ]]; then
		error "${SCRIPT_NAME} failed (exit $ec). Review messages above."
	fi
}

trap 'error "Command failed at line ${LINENO}: ${BASH_COMMAND}"' ERR
trap cleanup_trap EXIT

print_usage() {
	cat <<EOF
Usage: $SCRIPT_NAME [options]

Options:
	-n, --network    Clean only network artifacts
	-d, --docker     Clean only Docker resources (containers, dangling images, networks)
	-s, --stop-containers  Stop all running containers (no removal)
	    --images     Remove project images tagged :latest (in addition to selected mode)
	-v, --volumes            Remove ALL edge_storage_server volumes (static + dynamic)
	-r, --reset              Remove all containers and all edge_storage_server volumes
	    --image-filter NAME  Scope stop/remove operations to containers from image NAME
	-h, --help               Show this help
EOF
}

if [[ ${1-} == "-h" || ${1-} == "--help" ]]; then
		print_usage
		exit 0
fi

NAT_IFACE="${NAT_IFACE-}"
NAT_SUBNET=${NAT_SUBNET:-192.168.100.0/24}



need_sudo() {
	if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
		return 1
	fi
	return 0
}

SUDO=""
if need_sudo; then
	if command -v sudo >/dev/null 2>&1; then
		SUDO="sudo"
	else
		warn "Not running as root and 'sudo' is not available; privileged operations may fail."
	fi
fi

require_cmd() {
	command -v "$1" >/dev/null 2>&1 || { error "Required command not found: $1"; exit 127; }
}

ip_safe_del_link() {
	local link="$1"
	if $SUDO ip link show "$link" >/dev/null 2>&1; then
		$SUDO ip link del "$link" || warn "Failed to delete link: $link"
	else
		log "Link not present, skipping: $link"
	fi
}

iptables_safe_delete_nat() {
	local subnet="$1"; shift
	local iface="$1"
	if [[ -z "$iface" ]]; then
		log "NAT_IFACE not set; skipping NAT rule cleanup."
		return 0
	fi
	if $SUDO iptables -t nat -C POSTROUTING -s "$subnet" -o "$iface" -j MASQUERADE >/dev/null 2>&1; then
		$SUDO iptables -t nat -D POSTROUTING -s "$subnet" -o "$iface" -j MASQUERADE || warn "Failed to delete NAT rule"
	else
		log "NAT rule not present, skipping."
	fi
}

network_cleanup() {
	log "🧹 Cleaning up network artifacts"
	require_cmd ip
	# veth pairs to remove (both ends handled by single 'del')
	local veths=(veth1 veth2 veth3 veth4 veth5 veth6)
	for v in "${veths[@]}"; do
		ip_safe_del_link "$v"
	done

	if $SUDO ip link show veth5 >/dev/null 2>&1; then
		$SUDO ip addr flush dev veth5 || warn "Failed to flush addresses on veth5"
	fi

	if [[ -L /var/run/netns/ovs || -e /var/run/netns/ovs ]]; then
		$SUDO rm -f /var/run/netns/ovs || warn "Failed to remove /var/run/netns/ovs"
	else
		log "/var/run/netns/ovs not present, skipping."
	fi

	if command -v iptables >/dev/null 2>&1; then
		iptables_safe_delete_nat "$NAT_SUBNET" "$NAT_IFACE"
	else
		warn "iptables not available; skipping NAT cleanup."
	fi

	log "✅ Network cleanup complete."
}

docker_cmd() {
	if command -v docker >/dev/null 2>&1; then
		if [[ "${EUID:-$(id -u)}" -eq 0 ]] || id -nG 2>/dev/null | grep -q '\bdocker\b'; then
			echo docker
		elif command -v sudo >/dev/null 2>&1; then
			echo "sudo docker"
		else
			echo docker
		fi
	else
		echo ""
	fi
}

docker_cleanup() {
	local DOCKER
	DOCKER=$(docker_cmd)
	if [[ -z "$DOCKER" ]]; then
		warn "Docker not installed; skipping Docker cleanup."
		return 0
	fi

	log "🧹 Cleaning up Docker resources"

	local filter_args=()
	if [[ -n "$IMAGE_FILTER" ]]; then
		filter_args=(--filter "ancestor=${IMAGE_FILTER}")
		log "Filtering containers by image: ${IMAGE_FILTER}"
	fi

	local cids
	if cids=$(${DOCKER} ps -aq "${filter_args[@]}" 2>/dev/null); then
		if [[ -n "$cids" ]]; then
			${DOCKER} stop $cids >/dev/null 2>&1 || true
			${DOCKER} rm -f $cids >/dev/null 2>&1 || true
			log "Removed containers: $(echo "$cids" | wc -w)"
		else
			log "No containers to remove."
		fi
	else
		warn "Failed to list containers."
	fi

	local dangling
	if dangling=$(${DOCKER} images -f dangling=true -q 2>/dev/null); then
		if [[ -n "$dangling" ]]; then
			${DOCKER} rmi -f $dangling >/dev/null 2>&1 || true
			log "Removed dangling images: $(echo "$dangling" | wc -w)"
		else
			log "No dangling images to remove."
		fi
	fi

	${DOCKER} network prune -f >/dev/null 2>&1 || true
	${DOCKER} builder prune -f >/dev/null 2>&1 || true

	log "✅ Docker cleanup complete."
}

stop_containers() {
	local DOCKER
	DOCKER=$(docker_cmd)
	if [[ -z "$DOCKER" ]]; then
		warn "Docker not installed; skipping container stop."
		return 0
	fi

	log "⏸️ Stopping running containers"

	local filter_args=()
	if [[ -n "$IMAGE_FILTER" ]]; then
		filter_args=(--filter "ancestor=${IMAGE_FILTER}")
		log "Filtering containers by image: ${IMAGE_FILTER}"
	fi

	local running
	if running=$(${DOCKER} ps -q "${filter_args[@]}" 2>/dev/null); then
		if [[ -n "$running" ]]; then
			${DOCKER} stop $running >/dev/null 2>&1 || warn "Failed to stop one or more containers"
			log "Stopped containers: $(echo "$running" | wc -w)"
		else
			log "No running containers to stop."
		fi
	else
		warn "Failed to list running containers."
	fi
}

images_cleanup() {
	local DOCKER
	DOCKER=$(docker_cmd)
	if [[ -z "$DOCKER" ]]; then
		warn "Docker not installed; skipping image removal."
		return 0
	fi
	# Project image tags (must match build_images.sh)
	local images=(
		ovs-container
		ubuntu-nat-router
		edge_server
		edge_storage_server
		osken-controller
		local_state_server
	)
	local removed=0
	for img in "${images[@]}"; do
		if ${DOCKER} images -q "${img}:latest" >/dev/null 2>&1; then
			local id
			id=$(${DOCKER} images -q "${img}:latest" | head -n 1)
			if [[ -n "$id" ]]; then
				${DOCKER} rmi -f "${img}:latest" >/dev/null 2>&1 || true
				((++removed))
				log "Removed image: ${img}:latest"
			fi
		fi
	done
	if [[ $removed -eq 0 ]]; then
		log "No project :latest images found to remove."
	else
		log "✅ Removed ${removed} project image(s)."
	fi
}

list_transient_volumes() {
	local docker_cli="$1"
	# Dynamic storage nodes: edge_storage_lanX_dynY(_cs for cross-region).
	# Selective-sync containers: sel_sync_lanX_dynY.
	${docker_cli} volume ls --format '{{.Name}}' 2>/dev/null \
		| grep -E '^edge_storage_lan[0-9]+_dyn[0-9]+(_cs)?-data$|^sel_sync_lan[0-9]+_dyn[0-9]+-data$' || true
}

# Kept for backward compatibility with any external callers.
list_dynamic_storage_volumes() {
	list_transient_volumes "$1"
}

transient_volumes_cleanup() {
	log "🧹 Removing transient (dynamic) Docker volumes"
	local DOCKER
	DOCKER=$(docker_cmd)
	if [[ -z "$DOCKER" ]]; then
		warn "Docker not installed; skipping transient volume cleanup."
		return 0
	fi
	local extra_vols
	extra_vols=$(list_transient_volumes "$DOCKER")
	local removed=0
	for vol in $extra_vols; do
		if ${DOCKER} volume rm -f "$vol" >/dev/null 2>&1; then
			log "Removed transient volume: $vol"
			((++removed))
		else
			warn "Failed to remove transient volume: $vol"
		fi
	done
	if [[ $removed -gt 0 ]]; then
		log "✅ Removed ${removed} transient volume(s)."
	else
		log "No transient volumes to remove."
	fi
}

volumes_cleanup() {
	log "🧹 Removing edge_storage_server volumes"
	local DOCKER
	DOCKER=$(docker_cmd)
	if [[ -z "$DOCKER" ]]; then
		warn "Docker not installed; skipping volume removal."
		return 0
	fi
	local volumes=(edge_storage_server_n1-data edge_storage_server_n2-data)
	local removed=0
	for vol in "${volumes[@]}"; do
		if ${DOCKER} volume inspect "$vol" >/dev/null 2>&1; then
			if ${DOCKER} volume rm "$vol" >/dev/null 2>&1; then
				log "Removed volume: $vol"
				((++removed))
			else
				warn "Failed to remove volume: $vol"
			fi
		else
			log "Volume not present, skipping: $vol"
		fi
	done
	transient_volumes_cleanup
}

reset_cleanup() {
	log "♻️ Performing full reset (containers + edge_storage_server volumes)"
	local DOCKER
	DOCKER=$(docker_cmd)
	if [[ -z "$DOCKER" ]]; then
		warn "Docker not installed; skipping reset."
		return 0
	fi

	local containers
	if containers=$(${DOCKER} ps -aq 2>/dev/null); then
		if [[ -n "$containers" ]]; then
			${DOCKER} container rm -f $containers >/dev/null 2>&1 || warn "Failed to remove one or more containers"
		else
			log "No containers to remove."
		fi
	else
		warn "Failed to list containers for reset."
	fi

	${DOCKER} container rm -f edge_storage_server_n1 edge_storage_server_n2 >/dev/null 2>&1 || true

	local volumes=(edge_storage_server_n1-data edge_storage_server_n2-data)
	local removed=0
	for vol in "${volumes[@]}"; do
		if ${DOCKER} volume inspect "$vol" >/dev/null 2>&1; then
			if ${DOCKER} volume rm -f "$vol" >/dev/null 2>&1; then
				log "Removed volume: $vol"
				((++removed))
			else
				warn "Failed to remove volume: $vol"
			fi
		else
			log "Volume not present, skipping: $vol"
		fi
	done
	transient_volumes_cleanup

	if [[ $removed -gt 0 ]]; then
			log "✅ Reset removed ${removed} edge_storage_server volume(s)."
	fi
}

MODE="all"  # all | network | docker
DO_IMAGES=false
DO_VOLUMES=false
DO_RESET=false
DO_STOP_CONTAINERS=false
MODE_SELECTED=false
IMAGE_FILTER=""
while [[ $# -gt 0 ]]; do
	case "$1" in
		-n|--network)
			MODE="network"; MODE_SELECTED=true; shift ;;
		-d|--docker)
			MODE="docker"; MODE_SELECTED=true; shift ;;
		-s|--stop-containers)
			DO_STOP_CONTAINERS=true; shift ;;
		-i|--images)
			DO_IMAGES=true; shift ;;
		-v|--volumes)
			DO_VOLUMES=true; shift ;;
		-r|--reset)
			DO_RESET=true; shift ;;
		--image-filter)
			[[ -z "${2-}" ]] && { error "--image-filter requires an argument"; exit 2; }
			IMAGE_FILTER="$2"; shift 2 ;;
		-h|--help)
			print_usage; exit 0 ;;
		*)
			error "Unknown option: $1"; print_usage; exit 2 ;;
	esac
done

if [[ "$DO_STOP_CONTAINERS" == true && "$MODE_SELECTED" == false && "$MODE" == "all" ]]; then
	MODE="none"
fi

case "$MODE" in
	none)
		;;
	all)
		network_cleanup
		docker_cleanup
		transient_volumes_cleanup
		;;
	network)
		network_cleanup
		;;
	docker)
		docker_cleanup
		transient_volumes_cleanup
		;;
esac

if [[ "$DO_STOP_CONTAINERS" == true ]]; then
	stop_containers
fi

if [[ "$DO_IMAGES" == true ]]; then
    images_cleanup
fi

if [[ "$DO_VOLUMES" == true ]]; then
	volumes_cleanup
fi

if [[ "$DO_RESET" == true ]]; then
	reset_cleanup
fi

log "🎯 Cleanup finished ($MODE)."