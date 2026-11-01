#!/bin/bash
# WAN emulation netem on the inter-LAN router: half of WAN_RTT_MS per LAN-side
# egress (eth1/eth2) so RTT(lan1<->lan2) == WAN_RTT_MS; same-LAN traffic is untouched.
set -euo pipefail

PID_ROUTER=${PID_ROUTER:-$(docker inspect -f '{{.State.Pid}}' nat-router 2>/dev/null || true)}
if [[ -z "${PID_ROUTER}" ]]; then
    echo "[wan] nat-router container not running — skipping netem setup" >&2
    exit 0
fi

RTT=${WAN_RTT_MS:-0}
JIT=${WAN_JITTER_MS:-0}
LOSS=${WAN_LOSS_PCT:-0}
RATE=${WAN_RATE_KBIT:-0}
ONE_WAY=$(( RTT / 2 ))

apply_one() {
    local iface=$1
    # Always clear first so re-runs are idempotent.
    sudo nsenter -t "${PID_ROUTER}" -n tc qdisc del dev "${iface}" root 2>/dev/null || true

    if [[ "${RTT}" -eq 0 && "${LOSS}" -eq 0 && "${RATE}" -eq 0 ]]; then
        echo "[wan] ${iface}: cleared (no emulation)"
        return
    fi

    local args=()
    if [[ "${RTT}" -gt 0 ]]; then
        if [[ "${JIT}" -gt 0 ]]; then
            args+=(delay "${ONE_WAY}ms" "${JIT}ms" distribution normal)
        else
            args+=(delay "${ONE_WAY}ms")
        fi
    fi
    [[ "${LOSS}" -gt 0 ]] && args+=(loss "${LOSS}%")
    [[ "${RATE}" -gt 0 ]] && args+=(rate "${RATE}kbit")

    sudo nsenter -t "${PID_ROUTER}" -n tc qdisc add dev "${iface}" root netem "${args[@]}"
    echo "[wan] ${iface}: ${args[*]}"
}

apply_one eth1
apply_one eth2
