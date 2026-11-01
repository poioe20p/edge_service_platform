#!/bin/bash
# ============================================================================
# Runtime helper: re-apply WAN emulation on the inter-LAN router without
# rebuilding the testbed.
#
# Usage:
#   wan_set.sh <RTT_ms> [jitter_ms] [loss_pct] [rate_kbit]
#
# Examples:
#   wan_set.sh 0                # clear emulation (raw veth latency)
#   wan_set.sh 30               # metro profile
#   wan_set.sh 80 10 0.1        # regional with jitter + light loss
#   wan_set.sh 200 30 0.5       # inter-continental
#
# Defaults: jitter=0, loss=0, rate=0 (uncapped).
# Wraps source/scripts/network/inject_wan_latency.sh.
# ============================================================================
set -euo pipefail

if [[ $# -lt 1 ]]; then
    sed -n '4,16p' "$0"
    exit 1
fi

export WAN_RTT_MS="${1:-0}"
export WAN_JITTER_MS="${2:-0}"
export WAN_LOSS_PCT="${3:-0}"
export WAN_RATE_KBIT="${4:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../network/inject_wan_latency.sh"
