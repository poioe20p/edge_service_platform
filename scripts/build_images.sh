#!/bin/bash

if [ -z "${BASH_VERSION:-}" ]; then
	exec bash "$0" "$@"
fi


set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_NAME=$(basename "$0")

log()   { printf '[INFO] %s\n' "$*"; }
warn()  { printf '[WARN] %s\n' "$*" 1>&2; }
error() { printf '[ERROR] %s\n' "$*" 1>&2; }

cleanup() {
	local exit_code=$?
	if [[ $exit_code -ne 0 ]]; then
		error "A failure occurred (exit $exit_code). See logs above."
	fi
}

trap 'error "Command failed at line ${LINENO}: ${BASH_COMMAND}"' ERR
trap cleanup EXIT

print_usage() {
		cat <<EOF
Usage:
	./build_images.sh                # Build all images
	./build_images.sh OVS            # Build only the OVS image (by directory name)
	./build_images.sh ovs-container  # Build only the OVS image (by tag)
	./build_images.sh OVS edge_server edge_storage_server  # Build selected images

Options:
	-r, --reset NAME   Remove the running/stopped container associated with NAME before rebuilding.
	-h, --help         Show this help message and exit.

Notes:
- The script auto-detects the project root based on its own location.
- It will try to use "docker" directly if you have permission; otherwise it will use "sudo docker" when available.
EOF
}

RESET_TARGETS=()
POSITIONAL_ARGS=()

while [[ $# -gt 0 ]]; do
	case "$1" in
		-h|--help)
			print_usage
			exit 0
			;;
		-r|--reset)
			if [[ $# -lt 2 ]]; then
				error "Option $1 requires a container or image name argument."
				exit 1
			fi
			RESET_TARGETS+=("$2")
			shift 2
			;;
		--)
			shift
			POSITIONAL_ARGS+=("$@")
			break
			;;
		-*)
			error "Unknown option: $1"
			print_usage
			exit 1
			;;
		*)
			POSITIONAL_ARGS+=("$1")
			shift
			;;
	esac
done

set -- "${POSITIONAL_ARGS[@]}"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
DOCKER_DIR="${REPO_ROOT}/docker"
STORAGE_ENV_FILE="${REPO_ROOT}/.env-mongo"



[[ -d "$DOCKER_DIR" ]] || {
	error "Docker directory not found at: $DOCKER_DIR"
	error "Ensure the repository structure is intact."
	exit 1
}

require_command() {
	command -v "$1" >/dev/null 2>&1 || { error "Required command not found: $1"; exit 127; }
}

require_command docker

docker_cmd() {
	if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
		echo docker
		return
	fi
	if command -v id >/dev/null 2>&1 && id -nG 2>/dev/null | grep -qw docker; then
		echo docker
		return
	fi
	if command -v sudo >/dev/null 2>&1; then
		echo "sudo docker"
		return
	fi
	warn "You may not have permission to run Docker without sudo."
	echo docker
}

DOCKER=$(docker_cmd)

log "Building images..."
log "Repository root: $REPO_ROOT"
log "Docker build context base: $DOCKER_DIR"
log "Docker command: $DOCKER"

declare -A CONTAINER_NAME_MAP=(
	["ovs"]="ovs"
	["ovs-container"]="ovs"
	["os-ken"]="osken"
	["osken-controller"]="osken"
	["edge_server"]="edge_server_n1"
	["edge_server"]="edge_server_n2"
	["edge_storage_server"]="edge_storage_server_n1"
	["edge_storage_server"]="edge_storage_server_n2"
	["ubuntu-nat-router"]="nat-router"
	["local_state_server"]="aggregator_n1"
	["local_state_server"]="aggregator_n2"
)

resolve_container_name() {
	local dir_l=${1,,}
	local tag_l=${2,,}
	if [[ -n ${CONTAINER_NAME_MAP[$dir_l]:-} ]]; then
		printf '%s' "${CONTAINER_NAME_MAP[$dir_l]}"
		return
	fi
	if [[ -n ${CONTAINER_NAME_MAP[$tag_l]:-} ]]; then
		printf '%s' "${CONTAINER_NAME_MAP[$tag_l]}"
		return
	fi
	printf '%s' ""
}

matches_reset_target() {
	local target_l=${1,,}
	local dir_l=${2,,}
	local tag_l=${3,,}
	local container_l=${4,,}
	if [[ $target_l == "$dir_l" || $target_l == "$tag_l" ]]; then
		return 0
	fi
	if [[ -n $container_l && $target_l == "$container_l" ]]; then
		return 0
	fi
	return 1
}

reset_container() {
	local target="$1"
	local container_name="$2"
	if [[ -z $container_name ]]; then
		container_name=${target,,}
	fi
	if ${DOCKER} ps -a --format '{{.Names}}' | grep -Fxq "$container_name"; then
		log "Reset requested for '$target': removing container '$container_name'..."
		${DOCKER} rm -f "$container_name" >/dev/null
		log "Container '$container_name' removed."
	else
		log "Reset requested for '$target', but no container named '$container_name' was found."
	fi
}

declare -A RESET_APPLIED=()

IMAGES=(
	"OVS:ovs-container"
	"ubuntu-nat-router:ubuntu-nat-router"
	"edge_server:edge_server"
	"edge_storage_server:edge_storage_server"
	"edge_selective_storage:edge_selective_storage"
	"os-ken:osken-controller"
	"local_state_server:local_state_server"
)

build_image() {
	local dir="$1"
	local tag="$2"
	local ctx="$DOCKER_DIR/$dir"
	local dockerfile="$ctx/Dockerfile"

	[[ -d "$ctx" ]] || { error "Build directory not found: $ctx"; return 2; }
	[[ -f "$dockerfile" ]] || { error "Dockerfile not found: $dockerfile"; return 2; }

	log "Building image '$tag' from '$dir'..."
	${DOCKER} build -t "$tag" -f "$dockerfile" "$ctx"
	log "Image built: $tag"
}

should_build() {
	local dir="$1"; shift
	local tag="$1"; shift

	[[ $# -eq 0 ]] && return 0

	local sel
	for sel in "$@"; do
		if [[ "$sel" == "$dir" || "$sel" == "$tag" ]]; then
			return 0
		fi
	done
	return 1
}

SELECTION=("$@")

build_count=0
for entry in "${IMAGES[@]}"; do
	IFS=":" read -r dir tag <<<"$entry"
	if should_build "$dir" "$tag" "${SELECTION[@]}"; then
		container_name=$(resolve_container_name "$dir" "$tag")
		if ((${#RESET_TARGETS[@]})); then
			for idx in "${!RESET_TARGETS[@]}"; do
				target=${RESET_TARGETS[$idx]}
				if [[ -n ${RESET_APPLIED[$idx]:-} ]]; then
					continue
				fi
				if matches_reset_target "$target" "$dir" "$tag" "$container_name"; then
					reset_container "$target" "$container_name"
					RESET_APPLIED[$idx]=1
				fi
			done
		fi
		build_image "$dir" "$tag"
		# Use pre-increment to avoid set -e exiting when previous value is 0
		((++build_count))
	else
		log "Skipping $dir ($tag)"
	fi
done

if ((${#RESET_TARGETS[@]})); then
	for idx in "${!RESET_TARGETS[@]}"; do
		if [[ -z ${RESET_APPLIED[$idx]:-} ]]; then
			warn "Reset requested for '${RESET_TARGETS[$idx]}', but no matching image was built."
		fi
	done
fi

if [[ $build_count -eq 0 ]]; then
	warn "No images selected to build. Check your arguments."
	exit 0
fi

log "All requested images built ($build_count). Listing local images:"
${DOCKER} images
