#!/bin/bash

# trace_request.sh — end-to-end request trace for the edge platform
# usage: sudo bash trace_request.sh --ns <namespace> -- <curl command...>
# See: docs/operation/testing/trace_request_plan.md

set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"


if [[ -t 1 ]]; then
    readonly BOLD='\033[1m'
    readonly DIM='\033[2m'
    readonly CYAN='\033[36m'
    readonly GREEN='\033[32m'
    readonly YELLOW='\033[33m'
    readonly RED='\033[31m'
    readonly RESET='\033[0m'
else
    readonly BOLD='' DIM='' CYAN='' GREEN='' YELLOW='' RED='' RESET=''
fi


usage() {
    cat <<EOF
Usage: sudo $SCRIPT_NAME --ns <namespace> -- <curl command...>

Options:
  --ns    Network namespace of the client (e.g. lan1_client_1)
  --      Separator; everything after is the curl command

Example:
  sudo $SCRIPT_NAME --ns lan1_client_1 \\
    -- curl -s "http://10.0.0.253:5000/content/lan1::content::001?requester=lan1::user::001"
EOF
    exit 1
}


collect_logs() {
    local container="$1"
    docker logs "$container" --since "$BEFORE_TS" --until "$AFTER_TS" 2>&1
}


filter_or_fallback() {
    local pattern="$1"
    local fallback="$2"
    local input
    input=$(cat)

    local matched
    matched=$(echo "$input" | grep -E "$pattern" 2>/dev/null || true)

    if [[ -n "$matched" ]]; then
        echo "$matched"
    else
        echo "$fallback"
    fi
}


NS=""
CURL_CMD=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ns)
            [[ -z "${2:-}" ]] && { echo "Error: --ns requires a value"; usage; }
            NS="$2"; shift 2
            ;;
        --)
            shift; CURL_CMD=("$@"); break
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Error: unknown option '$1'"
            usage
            ;;
    esac
done

[[ -z "$NS" ]]              && { echo "Error: --ns is required"; usage; }
[[ ${#CURL_CMD[@]} -eq 0 ]] && { echo "Error: curl command required after --"; usage; }


CLIENT_IP=$(ip -n "$NS" -4 addr show \
    | grep -oP '(?<=inet\s)10\.\d+\.\d+\.\d+' | head -1 || true)
[[ -z "$CLIENT_IP" ]] && { echo "Error: no 10.x.x.x IP found in namespace '$NS'"; exit 1; }

CLIENT_MAC=$(ip -n "$NS" link show \
    | grep -oP '(?<=link/ether\s)[0-9a-f:]+' | head -1 || true)
[[ -z "$CLIENT_MAC" ]] && { echo "Error: no MAC address found in namespace '$NS'"; exit 1; }

if [[ "$CLIENT_IP" == 10.0.0.* ]]; then
    LAN="lan1"
    CONTROLLER="osken"
    EDGE_SERVER="edge_server_n1"
    PEER_CONTROLLER="osken_2"
elif [[ "$CLIENT_IP" == 10.0.1.* ]]; then
    LAN="lan2"
    CONTROLLER="osken_2"
    EDGE_SERVER="edge_server_n2"
    PEER_CONTROLLER="osken"
else
    echo "Error: unexpected IP range '$CLIENT_IP' — expected 10.0.0.x or 10.0.1.x"
    exit 1
fi


# Record start timestamp (1 second before for docker log margin)
BEFORE_TS=$(date -u -d '-1 second' '+%Y-%m-%dT%H:%M:%S')

RESPONSE=$(ip netns exec "$NS" "${CURL_CMD[@]}" \
    -w $'\n%{http_code}' 2>/dev/null) || true

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

# Record end timestamp (5 seconds buffer for async telemetry flush)
AFTER_TS=$(date -u -d '+5 seconds' '+%Y-%m-%dT%H:%M:%S')


CTRL_LOGS=$(collect_logs "$CONTROLLER")
EDGE_LOGS=$(collect_logs "$EDGE_SERVER")

VIP_SERVER_LINES=$(echo "$CTRL_LOGS" | filter_or_fallback \
    "select_server:|vip_server: client=${CLIENT_IP}|dnat/snat installed:.*10\.0\.0\.253" \
    "(no VIP_SERVER routing — DNAT flow may already be installed)")

EDGE_LINES=$(echo "$EDGE_LOGS" | filter_or_fallback \
    "${CLIENT_IP}|Created MongoClient|Sending telemetry event|error" \
    "(no edge server logs in time window)")

EDGE_IP=$(echo "$CTRL_LOGS" \
    | grep -F "vip_server: client=${CLIENT_IP}" \
    | grep -oP '(?<=real=)\S+' | head -1 || true)

VIP_DATA_LINES=$(echo "$CTRL_LOGS" | filter_or_fallback \
    "select_storage\(|warm-selected=|vip_data\(|awaiting ARP from backend|dnat/snat installed:.*10\.0\.[01]\.200" \
    "(no VIP_DATA routing — DNAT flow may already be installed)")

CROSS_LAN_LINES=""
if echo "$CTRL_LOGS" | grep -qE "cross-network" 2>/dev/null; then
    PEER_LOGS=$(collect_logs "$PEER_CONTROLLER")
    CROSS_LAN_LINES=$(echo "$PEER_LOGS" \
    | grep -E "vip_data\(|select_storage\(|warm-selected=|awaiting ARP from backend|dnat/snat" || true)
fi


if [[ "$HTTP_CODE" == 2* ]]; then
    SC="$GREEN"
elif [[ "$HTTP_CODE" == 4* ]]; then
    SC="$YELLOW"
else
    SC="$RED"
fi

URL_PATH=$(printf '%s ' "${CURL_CMD[@]}" \
    | grep -oP 'http://[^ ]+' | head -1 \
    | sed 's|http://[^/]*||' || true)
[[ -z "$URL_PATH" ]] && URL_PATH="(unknown path)"

echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  Request Trace: ${CYAN}${NS}${RESET}${BOLD} → ${URL_PATH}${RESET}"
echo -e "${BOLD}  Client: ${RESET}IP=${CLIENT_IP}  MAC=${CLIENT_MAC}  LAN=${LAN}"
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
echo ""

echo -e "${BOLD}── 1. VIP_SERVER Routing (${CONTROLLER}) ──────────────────────${RESET}"
echo "$VIP_SERVER_LINES" | sed 's/^/  /'
echo ""

echo -e "${BOLD}── 2. Edge Server (${EDGE_SERVER}) ────────────────────────────${RESET}"
echo "$EDGE_LINES" | sed 's/^/  /'
echo ""

echo -e "${BOLD}── 3. VIP_DATA Routing (${CONTROLLER}) ────────────────────────${RESET}"
echo "$VIP_DATA_LINES" | sed 's/^/  /'
echo ""

if [[ -n "$CROSS_LAN_LINES" ]]; then
    echo -e "${BOLD}── 4. Cross-LAN Routing (${PEER_CONTROLLER}) ──────────────────${RESET}"
    echo "$CROSS_LAN_LINES" | sed 's/^/  /'
    echo ""
fi

echo -e "${BOLD}── Response ───────────────────────────────────────────────────${RESET}"
echo -e "  HTTP ${SC}${HTTP_CODE}${RESET}"
if [[ ${#BODY} -gt 200 ]]; then
    echo "  ${BODY:0:200}…"
else
    echo "  $BODY"
fi
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
