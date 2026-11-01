#!/usr/bin/env bash
# Pre-launch per-run label check for the frozen-tree launchers (plan §5.1/§10).
# C1/C2/C4 launch from rq3tim_frozen @ 852a8a3 (no in-tree guard) and rq3snd_frozen2 @
# 541fb22 (has one); the runner runs this IMMEDIATELY BEFORE every launch from a frozen tree.
#
# `--tree` is repeatable and accumulates (every tree scanned, in the order given, never
# last-wins); with no `--tree` it scans this script's own tree's metrics/.
#
# Exit codes: 0 = no `*_<label>` run folder in any scanned tree (launch may proceed);
#             3 = duplicate label (STOP — never launch); 2 = usage/fail-closed error.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

usage() {
    echo "usage: $0 [--tree <tree-root>]... <label>" >&2
    exit 2
}

TREES=()
ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tree)
            [[ $# -ge 2 && -n "${2:-}" ]] || usage
            TREES+=("$2")
            shift 2
            ;;
        --tree=*)
            [[ -n "${1#--tree=}" ]] || usage
            TREES+=("${1#--tree=}")
            shift
            ;;
        -h|--help)
            echo "usage: $0 [--tree <tree-root>]... <label>"
            exit 0
            ;;
        -*)
            usage
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done
[[ ${#ARGS[@]} -eq 1 && -n "${ARGS[0]:-}" ]] || usage

readonly LABEL="${ARGS[0]}"
[[ "$LABEL" != *"/"* && "$LABEL" != *".."* ]] || {
    echo "ERROR: invalid label '$LABEL'" >&2
    exit 2
}

if [[ ${#TREES[@]} -eq 0 ]]; then
    TREES=("$REPO_ROOT")
fi

UNIQUE_TREES=()
for tree in "${TREES[@]}"; do
    duplicate=0
    for prior in "${UNIQUE_TREES[@]}"; do
        if [[ "$prior" == "$tree" ]]; then
            duplicate=1
            break
        fi
    done
    [[ $duplicate -eq 1 ]] || UNIQUE_TREES+=("$tree")
done

for tree in "${UNIQUE_TREES[@]}"; do
    [[ -d "$tree" ]] || {
        echo "ERROR: tree root not found: $tree" >&2
        exit 2
    }
    [[ -d "$tree/source/scripts/testing/metrics" ]] || {
        echo "ERROR: metrics dir not found: $tree/source/scripts/testing/metrics (wrong --tree?)" >&2
        exit 2
    }
done

hits=0
for tree in "${UNIQUE_TREES[@]}"; do
    metrics_dir="$tree/source/scripts/testing/metrics"
    echo "scanned: $metrics_dir"
    if ! existing_run="$(find "$metrics_dir" -mindepth 1 -maxdepth 1 -type d \
            -name "*_${LABEL}" -print 2>/dev/null | sort)"; then
        echo "ERROR: metrics scan failed for tree '$tree' ($metrics_dir) — cannot establish label uniqueness (fail closed)" >&2
        exit 2
    fi
    if [[ -n "$existing_run" ]]; then
        hits=1
        while IFS= read -r hit; do
            echo "DUPLICATE LABEL: '$LABEL' already has a run folder: $hit" >&2
        done <<< "$existing_run"
    fi
done

if [[ $hits -ne 0 ]]; then
    echo "STOP: never launch; escalate to campaign_log.md (quarantine/replace)." >&2
    exit 3
fi

echo "label check PASS: no existing run folder for '$LABEL' in any scanned tree (launch may proceed)"
exit 0
