#!/bin/bash
# ============================================================================
# rq3v2_p6_01_preflight.sh — RQ3 v2 Phase-6 pre-flight gates (cloud-vm-rq3).
#
# Automates the host-runnable gates of docs/operation/testing/experiment/v2/rq3/
# rq3_preflight.md: VM baseline (Stage 0), source/code gates (Stage 1), env
# regime validation (Stage 3), and the calibration-run measurability/arming
# analysis (Stages 5/6, given run folders). Image builds (Stage 2) and network
# provisioning (Stage 4) are operator-run per the doc — this script does NOT
# detect image/OVS/static-edge state; those are doc-stage gates.
#
# Exit 0 = all checked gates pass; 1 = at least one gate failed. Writes
# $REPORT (default rq3_preflight_report.txt).
#
# Usage:
#   rq3v2_p6_01_preflight.sh [--repo DIR] [--env-dir DIR] [--report FILE]
#       [--calib-direct RUN_DIR] [--calib-discovery RUN_DIR]
#       [--only baseline|code|env|calib]
# ============================================================================

set -euo pipefail

readonly SCRIPT_NAME="$(basename "$0")"
readonly DEFAULT_REPO="$HOME/efficient-storage-in-edge-scenarios"
readonly DEFAULT_ENV_DIR="$HOME/rq3_env"

REPO="${RQ3_REPO:-$DEFAULT_REPO}"
ENV_DIR="${RQ3_ENV_DIR:-$DEFAULT_ENV_DIR}"
REPORT="${RQ3_REPORT:-rq3_preflight_report.txt}"
CALIB_SUMMARY="${RQ3_CALIB_SUMMARY:-rq3_calib_summary.csv}"
CALIB_DIRECT=""
CALIB_DISCOVERY=""
ONLY=""

PASS=0
FAIL=0
WARN=0
FAILED_GATES=()
SKIPPED_CALIB=0

# Temp files to remove on exit.
_TMP_FILES=()

cleanup() {
    for f in "${_TMP_FILES[@]:-}"; do
        [[ -n "$f" && -f "$f" ]] && rm -f "$f"
    done
}

trap cleanup EXIT

report() {
    printf '%s\n' "$*" >> "$REPORT"
    printf '%s\n' "$*"
}

# check <desc> <cmd...>: run a gate command; exit 0 => PASS, else FAIL.
check() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        PASS=$((PASS + 1))
        report "[PASS] $desc"
    else
        FAIL=$((FAIL + 1))
        FAILED_GATES+=("$desc")
        report "[FAIL] $desc"
    fi
}

# assert_env_key <file> <key> <expected>: exact key=value assertion.
assert_env_key() {
    local file="$1" key="$2" expected="$3"
    local actual
    actual="$(grep -E "^${key}=" "$file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    if [[ "$actual" == "$expected" ]]; then
        PASS=$((PASS + 1))
        report "[PASS] env $key=$expected ($(basename "$file"))"
    else
        FAIL=$((FAIL + 1))
        FAILED_GATES+=("env $key=$(basename "$file")")
        report "[FAIL] env $key expected '$expected', got '${actual:-<missing>}' ($(basename "$file"))"
    fi
}

# assert_matrix <file...>: the full pre-registered knob set on every arm file.
assert_matrix() {
    local f
    for f in "$@"; do
        assert_env_key "$f" "EDGE_FLOW_ISOLATION" "1"
        assert_env_key "$f" "VIP_FLOW_ISOLATION" "1"
        # Per-connection VIP_SERVER flows (RQ3): required so the async
        # request_complete delete never collides with the next request's flow
        # (Check C coverage ~1.0 under the calibrated spike rate).
        assert_env_key "$f" "VIP_SERVER_PER_CONNECTION_FLOWS" "1"
        assert_env_key "$f" "EDGE_READY_PORT" "5000"
        assert_env_key "$f" "BACKEND_SELECTION_POLICY" "topology_host"
        assert_env_key "$f" "VIP_WARM_SERVER_SECONDS" "0"
        assert_env_key "$f" "SCALEUP_POLICY" "dual"
        # Base env disables compute scale-up (MAX_DYNAMIC_COMPUTE=0); RQ3 v2
        # arms must re-enable it or the readiness gate never sees a pending
        # compute backend (calibration finding).
        assert_env_key "$f" "MAX_DYNAMIC_COMPUTE" "6"
        assert_env_key "$f" "TELEMETRY_SOURCE" "event_preserving"
        assert_env_key "$f" "STORAGE_PERSISTENT_RESERVE_ENABLED" "0"
        assert_env_key "$f" "SS_ENABLED" "0"
        assert_env_key "$f" "CROSS_REGION_STORAGE_ENABLED" "0"
    done
}

stage_baseline() {
    report "== Stage 0 — VM baseline =="
    check "nproc >= 4" bash -c '[ "$(nproc)" -ge 4 ]'
    check "ubuntu 22.04" bash -c 'grep -q "22.04" /etc/os-release'
    check "docker daemon via sudo -n" bash -c "sudo -n docker ps >/dev/null 2>&1"
    check "fresh state (0 containers)" bash -c '[ "$(sudo -n docker ps -q 2>/dev/null | wc -l)" -eq 0 ]'
    check "repo present" test -d "$REPO"
    check "make present" bash -c 'command -v make >/dev/null'
    check "git present" bash -c 'command -v git >/dev/null'
    # OVS is containerized (source/docker/OVS/); the host only needs the
    # openvswitch kernel module (loadable) plus the ovs-container image.
    check "openvswitch kernel module" \
        bash -c 'sudo -n modprobe openvswitch 2>/dev/null; lsmod | grep -q openvswitch'
    check "ovs-container image present" \
        bash -c "sudo -n docker images --format '{{.Repository}}:{{.Tag}}' | grep -q 'ovs-container'"
    check "python3 aiohttp" bash -c "python3 -c 'import aiohttp'"
    check "python3 requests" bash -c "python3 -c 'import requests'"
    check "pip3 present" bash -c "pip3 --version >/dev/null 2>&1"
    report "  host: $(hostname 2>/dev/null || echo n/a)  nproc: $(nproc 2>/dev/null || echo n/a)"
    report "  repo HEAD: $(git -C "$REPO" log --oneline -1 2>/dev/null || echo n/a)"
}

stage_code() {
    report "== Stage 1 — source markers + code gates =="
    check "marker: EDGE_APP_READY_EVENT (edge_server)" \
        grep -q "EDGE_APP_READY_EVENT" \
        "$REPO/source/docker/edge_server/source/edge_server_process_state.py"
    check "marker: app_ready whitelist (aggregator)" \
        grep -q '"app_ready"' \
        "$REPO/source/docker/local_state_server/aggregator.py"
    check "marker: admit_on_event (readiness_gate)" \
        grep -q "def admit_on_event" \
        "$REPO/source/sdn_controller/readiness_gate.py"
    check "marker: discovery_15 relabel (analyzer)" \
        grep -q "discovery_15" \
        "$REPO/docs/research_questions/v2/rq3/rq3_admission_analysis.py"
    check "driver selftest (host)" \
        bash -c "cd '$REPO/source/scripts' && python3 testing/openloop_p1_01_driver_selftest.py"
    check "rq3 analyzer selftest" \
        bash -c "cd '$REPO/source/scripts' && python3 testing/rq3v2_p1_01_analyzer_selftest.py"
    check "rq3 app_ready selftest" \
        bash -c "cd '$REPO/source/scripts' && python3 testing/rq3v2_p2_01_app_ready_selftest.py"
}

stage_env() {
    report "== Stage 3 — env regime validation =="
    local base="$REPO/source/scripts/testing/controller_env_overrides"
    local docs_env="$REPO/docs/operation/testing/experiment/v2/rq3/env"
    check "rq3_env dir present" test -d "$ENV_DIR"
    if [[ ! -d "$ENV_DIR" ]]; then
        return 0
    fi
    local direct="$ENV_DIR/rq3_direct.env"
    local disc="$ENV_DIR/rq3_discovery.env"
    local disc15="$ENV_DIR/rq3_discovery_15.env"
    for f in "$direct" "$disc" "$disc15"; do
        check "env file exists: $(basename "$f")" test -f "$f"
    done

    # Full pre-registered knob matrix — all arms (VM mirror + docs copies).
    assert_matrix "$direct" "$disc" "$disc15"
    assert_env_key "$direct" "READINESS_PROPAGATION" "direct"
    assert_env_key "$direct" "EDGE_APP_READY_EVENT" "1"
    assert_env_key "$direct" "READINESS_EVENT_FALLBACK_S" "5.0"
    assert_env_key "$disc" "READINESS_PROPAGATION" "discovery"
    assert_env_key "$disc" "DISCOVERY_POLL_INTERVAL_S" "10.0"
    assert_env_key "$disc15" "READINESS_PROPAGATION" "discovery"
    assert_env_key "$disc15" "DISCOVERY_POLL_INTERVAL_S" "15.0"
    # EDGE_APP_READY_EVENT must NOT be enabled outside direct (a misconfigured
    # discovery run would otherwise be admitted on the event, bypassing the
    # cadence — the treatment). Checked for BOTH discovery regimes.
    local app_ready_f
    for app_ready_f in "$disc" "$disc15"; do
        local app_ready
        app_ready="$(grep -E '^EDGE_APP_READY_EVENT=' "$app_ready_f" 2>/dev/null | head -1 | cut -d= -f2- || true)"
        if [[ -z "$app_ready" || "$app_ready" == "0" ]]; then
            PASS=$((PASS + 1)); report "[PASS] $(basename "$app_ready_f"): EDGE_APP_READY_EVENT absent/0"
        else
            FAIL=$((FAIL + 1)); FAILED_GATES+=("$(basename "$app_ready_f") EDGE_APP_READY_EVENT")
            report "[FAIL] $(basename "$app_ready_f"): EDGE_APP_READY_EVENT must be 0/absent (got '$app_ready')"
        fi
    done

    # Sync (FAIL-hard): canonical controller_env_overrides == VM mirror.
    check "sync: direct canonical == ~/rq3_env" \
        bash -c "diff -q '$base/rq3_direct.env' '$direct' >/dev/null"
    check "sync: discovery canonical == ~/rq3_env" \
        bash -c "diff -q '$base/rq3_discovery.env' '$disc' >/dev/null"
    check "sync: discovery_15 canonical == ~/rq3_env" \
        bash -c "diff -q '$base/rq3_discovery_15.env' '$disc15' >/dev/null"

    # Docs env copies: exist + key knobs (provenance header differs by design).
    check "docs env copy dir present" test -d "$docs_env"
    if [[ -d "$docs_env" ]]; then
        local dc docs_direct docs_disc docs_disc15
        docs_direct="$docs_env/rq3_direct.env"
        docs_disc="$docs_env/rq3_discovery.env"
        docs_disc15="$docs_env/rq3_discovery_15.env"
        for dc in "$docs_direct" "$docs_disc" "$docs_disc15"; do
            check "docs env copy: $(basename "$dc")" test -f "$dc"
        done
        # Same knob matrix as the canonical files (per-knob; the provenance
        # header differs by design).
        assert_matrix "$docs_direct" "$docs_disc" "$docs_disc15"
        assert_env_key "$docs_direct" "READINESS_PROPAGATION" "direct"
        assert_env_key "$docs_direct" "EDGE_APP_READY_EVENT" "1"
        assert_env_key "$docs_disc" "READINESS_PROPAGATION" "discovery"
        assert_env_key "$docs_disc" "DISCOVERY_POLL_INTERVAL_S" "10.0"
        assert_env_key "$docs_disc15" "READINESS_PROPAGATION" "discovery"
        assert_env_key "$docs_disc15" "DISCOVERY_POLL_INTERVAL_S" "15.0"
    fi

    # Port consistency: EDGE_READY_PORT == edge bind_port default (BIND_PORT, 5000).
    local bind_line
    bind_line="$(grep -E 'BIND_PORT' "$REPO/source/docker/edge_server/source/edge_server_config.py" 2>/dev/null | head -1 || true)"
    if [[ "$bind_line" == *'"BIND_PORT", "5000"'* ]]; then
        PASS=$((PASS + 1)); report "[PASS] edge bind_port default == 5000"
    else
        FAIL=$((FAIL + 1)); FAILED_GATES+=("edge bind_port default")
        report "[FAIL] edge bind_port default != 5000 (line: ${bind_line:-<missing>})"
    fi
}

stage_calib() {
    report "== Stages 5/6 — calibration analysis (given run folders) =="
    if [[ -z "$CALIB_DIRECT" && -z "$CALIB_DISCOVERY" ]]; then
        report "[SKIP] no --calib-direct/--calib-discovery provided — run after the G2 calibration runs"
        SKIPPED_CALIB=1
        return 0
    fi
    # G1/G2/G3 require BOTH arms (the doc gates are per-LAN in both arms).
    if [[ -z "$CALIB_DIRECT" || -z "$CALIB_DISCOVERY" ]]; then
        FAIL=$((FAIL + 1)); FAILED_GATES+=("calibration both arms required")
        report "[FAIL] both --calib-direct and --calib-discovery are required (doc G1/G2/G3 cover both arms)"
    fi

    # Arm cross-check: the run folder's arm must match the flag.
    local arm_check_ok=1
    if [[ -n "$CALIB_DIRECT" ]]; then
        local a
        a="$(grep -E '^READINESS_PROPAGATION=' "$CALIB_DIRECT/controller_env_snapshot.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
        if [[ "$a" != "direct" ]]; then
            arm_check_ok=0
            report "[FAIL] --calib-direct folder arm is '${a:-<missing env snapshot>}' (expected direct)"
        fi
    fi
    if [[ -n "$CALIB_DISCOVERY" ]]; then
        local a2
        a2="$(grep -E '^READINESS_PROPAGATION=' "$CALIB_DISCOVERY/controller_env_snapshot.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
        if [[ "$a2" != "discovery" ]]; then
            arm_check_ok=0
            report "[FAIL] --calib-discovery folder arm is '${a2:-<missing env snapshot>}' (expected discovery)"
        fi
    fi
    if [[ "$arm_check_ok" -eq 0 ]]; then
        FAIL=$((FAIL + 1)); FAILED_GATES+=("calibration arm cross-check")
    fi

    # G4: flow-validation per calibration folder (0 pass / 1 hard fail / 2 degraded).
    local dir
    for dir in "$CALIB_DIRECT" "$CALIB_DISCOVERY"; do
        [[ -n "$dir" ]] || continue
        # `if` (not a bare call) so a non-zero flow-validation exit does not
        # abort under set -e; the code is captured for the gate verdict.
        local rc=0
        if python3 "$REPO/docs/research_questions/v2/rq3/rq3_flow_validation.py" "$dir" >/dev/null 2>&1; then
            rc=0
        else
            rc=$?
        fi
        if [[ $rc -eq 1 ]]; then
            FAIL=$((FAIL + 1)); FAILED_GATES+=("flow-validation $(basename "$dir")")
            report "[FAIL] flow-validation hard violation: $dir"
        elif [[ $rc -eq 2 ]]; then
            report "[DEGRADED] flow-isolation coverage < 0.9: $dir (recorded)"
        else
            PASS=$((PASS + 1)); report "[PASS] flow-validation: $dir"
        fi
    done

    local dirs=()
    [[ -n "$CALIB_DIRECT" ]] && dirs+=("$CALIB_DIRECT")
    [[ -n "$CALIB_DISCOVERY" ]] && dirs+=("$CALIB_DISCOVERY")
    local csv="$CALIB_SUMMARY"
    check "analyzer runs on calibration folders" \
        bash -c "cd '$REPO' && python3 docs/research_questions/v2/rq3/rq3_admission_analysis.py ${dirs[*]} --csv '$csv' >/dev/null"
    # Fail if the analyzer produced no data rows (wrong folder / non-RQ3 arm).
    local nrows
    nrows="$(tail -n +2 "$csv" 2>/dev/null | grep -c . || true)"
    if [[ "${nrows:-0}" -lt 1 ]]; then
        FAIL=$((FAIL + 1)); FAILED_GATES+=("calibration analyzer rows")
        report "[FAIL] analyzer produced no data rows for the calibration folders (header-only CSV)"
        return 0
    fi
    local verdict
    verdict="$(python3 - "$csv" <<'PY' || echo PYTHON_ERROR
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
by = {}
for r in rows:
    by.setdefault(r.get("arm") or "", []).append(r)
out = []
for arm in sorted(by):
    for r in by[arm]:
        void = (r.get("void") or "").strip().lower() == "true"
        b1 = int(r.get("backends_lan1") or 0)
        b2 = int(r.get("backends_lan2") or 0)
        g1 = int(r.get("gap_requests_lan1") or 0)
        g2 = int(r.get("gap_requests_lan2") or 0)
        frac = r.get("event_fraction") or ""
        flags = []
        if void: flags.append("void")
        if b1 < 1 or b2 < 1: flags.append("minadmissions-FAIL")
        if g1 < 20 or g2 < 20: flags.append("measurability-gap<20/LAN")
        if arm == "direct":
            try:
                if float(frac) < 0.8: flags.append("event-frac<0.80")
            except ValueError:
                flags.append("event-frac-missing")
        out.append(f"{arm} b1={b1} b2={b2} gap_lan1={g1} gap_lan2={g2} event_frac={frac} " + (";".join(flags) if flags else "OK"))
print("\n".join(out))
PY
)"
    report "  calib analysis: $verdict"
    if [[ "$verdict" == *"PYTHON_ERROR"* ]] \
        || grep -q "FAIL" <<<"$verdict" \
        || grep -q "<0.80" <<<"$verdict" \
        || grep -q "event-frac-missing" <<<"$verdict" \
        || grep -q "measurability-gap<20" <<<"$verdict"; then
        FAIL=$((FAIL + 1))
        FAILED_GATES+=("calibration arming gates")
        report "[FAIL] calibration arming gates (see above)"
    else
        PASS=$((PASS + 1))
        report "[PASS] calibration arming gates"
    fi
}

usage() {
    echo "Usage: $SCRIPT_NAME [--repo DIR] [--env-dir DIR] [--report FILE]"
    echo "       [--calib-direct RUN_DIR] [--calib-discovery RUN_DIR] [--only baseline|code|env|calib]"
    exit 0
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --repo)          REPO="$2"; shift 2 ;;
            --env-dir)       ENV_DIR="$2"; shift 2 ;;
            --report)        REPORT="$2"; shift 2 ;;
            --calib-direct)  CALIB_DIRECT="$2"; shift 2 ;;
            --calib-discovery) CALIB_DISCOVERY="$2"; shift 2 ;;
            --calib-summary) CALIB_SUMMARY="$2"; shift 2 ;;
            --only)          ONLY="$2"; shift 2 ;;
            -h|--help)       usage ;;
            *) echo "Error: unknown option $1" >&2; exit 1 ;;
        esac
    done

    : > "$REPORT"
    report "RQ3 v2 pre-flight report — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    report "repo=$REPO  env_dir=$ENV_DIR"

    case "$ONLY" in
        ""|baseline|code|env|calib) ;;
        *) echo "Error: --only must be baseline|code|env|calib" >&2; exit 1 ;;
    esac

    if [[ -z "$ONLY" || "$ONLY" == "baseline" ]]; then stage_baseline; fi
    if [[ -z "$ONLY" || "$ONLY" == "code" ]]; then stage_code; fi
    if [[ -z "$ONLY" || "$ONLY" == "env" ]]; then stage_env; fi
    if [[ -z "$ONLY" || "$ONLY" == "calib" ]]; then stage_calib; fi

    report ""
    report "== Summary =="
    report "  PASS=$PASS  FAIL=$FAIL  WARN=$WARN"
    if [[ ${#FAILED_GATES[@]} -gt 0 ]]; then
        report "  FAILED:"
        for g in "${FAILED_GATES[@]}"; do report "    - $g"; done
    else
        report "  all checked gates PASS"
    fi
    report "  full report: $REPORT"
    if [[ ${#FAILED_GATES[@]} -gt 0 ]]; then
        return 1
    fi
    if [[ "$SKIPPED_CALIB" -eq 1 ]]; then
        report "  INCOMPLETE: calibration analysis skipped (no --calib-* folders) — run it before GO"
        return 2
    fi
    return 0
}

main "$@"
