#!/usr/bin/env bash
# Guarded launcher for one RQ3 admission-modes supporting run (§12.3 / §5.2).
# usage: rq3adm_p1_01_launch_run.sh <set> <arm> <label> <seed>  |  ... restore-family
#          sets: camp | cell12 | disc15      arms: direct | discovery
#
# Per set: that set's archived phases_snapshot.json is restored into the canonical
# phases.json IN PLACE — validated BEFORE the copy, so any mismatch is a STOP.
#
# restore-family restores the Family-4 profile from a hash-verified copy (never a
# tree-local `git checkout`), asserting the hash recorded at preflight.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly METRICS_DIR="$REPO_ROOT/source/scripts/testing/metrics"
readonly PHASES_PATH="$REPO_ROOT/source/scripts/testing/phases.json"
readonly PLAN_ANALYSIS="$REPO_ROOT/docs/operation/testing/experiment/v3/rq3_admission_modes/analysis"
# Recorded canonical family-profile hash (the preflight's analysis/canonical_hashes.json
# reference; byte-identical in the frozen trees). Used when that record is absent.
readonly FAMILY_PROFILE_SHA256="57bccc12bebb98dfeb920c417d343e734763cd9a747f917b8177ee04977c7aa2"
TEMP_FILES=()

die() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    local path
    for path in "${TEMP_FILES[@]:-}"; do
        if [[ -n "$path" && -f "$path" ]]; then
            rm -f "$path"
        fi
    done
    return 0
}
trap cleanup EXIT

set_params() {
    case "$1" in
        camp)
            SET_RATE="3.0"; SET_WINDOW=1024; SET_EDGE_CPUS="0.30"
            SET_LABEL_RE='^rq3_camp_(direct|disc)_[0-9]+$'
            SET_SNAPSHOT_GLOB="*_rq3_direct_* *_rq3_disc_*"
            ;;
        cell12)
            SET_RATE="12.0"; SET_WINDOW=4096; SET_EDGE_CPUS="0.30"
            SET_LABEL_RE='^rq3_probe_cell12_(direct|disc)_[0-9]+$'
            SET_SNAPSHOT_GLOB="*_rq3_probe_cell12_direct_* *_rq3_probe_cell12_disc_*"
            ;;
        disc15)
            SET_RATE="3.0"; SET_WINDOW=1024; SET_EDGE_CPUS="0.30"
            SET_LABEL_RE='^rq3_disc15_[0-9]+$'
            SET_SNAPSHOT_GLOB="*_rq3_disc15_*"
            ;;
        *)
            die "invalid set '$1' (camp | cell12 | disc15)"
            ;;
    esac
}

assert_set_profile() {
    python3 - "$1" "$SET_RATE" "$2" <<'PY' || die "profile mismatch on $2 (STOP; plan §5.2)"
import json
import sys

path, rate_expected, context = sys.argv[1], sys.argv[2], sys.argv[3]
phases = json.load(open(path, encoding="utf-8"))["phases"]
by_name = {}
for phase in phases:
    by_name.setdefault(phase.get("name"), phase)


def fail(message):
    raise SystemExit(f"{context}: {message}")


baseline = by_name.get("baseline")
if baseline is None:
    fail("missing baseline phase")
if float(baseline.get("duration_s") or 0) != 60.0:
    fail(f"baseline duration {baseline.get('duration_s')} != 60 s")
if float(baseline.get("rate_per_client") or 0) != 1.0:
    fail(f"baseline rate {baseline.get('rate_per_client')} != 1.0")

spike = by_name.get("compute_spike")
if spike is None:
    fail("missing compute_spike phase")
if float(spike.get("duration_s") or 0) != 180.0:
    fail(f"compute_spike duration {spike.get('duration_s')} != 180 s")
if float(spike.get("rate_per_client") or 0) != float(rate_expected):
    fail(f"compute_spike rate {spike.get('rate_per_client')} != {rate_expected}")
mix = spike.get("mix") or {}
expected_mix = {"service_pressure": 0.9, "feed_ranking": 0.05, "content_lookup": 0.05}
for key, value in expected_mix.items():
    if abs(float(mix.get(key) or 0.0) - value) > 1e-9:
        fail(f"compute_spike mix {key}={mix.get(key)} != {value}")

gap = by_name.get("cleanup_gap")
if gap is None:
    fail("missing cleanup_gap phase")
if float(gap.get("duration_s") or 0) != 180.0:
    fail(f"cleanup_gap duration {gap.get('duration_s')} != 180 s")
if float(gap.get("rate_per_client") or 0) != 0.5:
    fail(f"cleanup_gap rate {gap.get('rate_per_client')} != 0.5")
print(f"profile assertion PASS ({context})")
PY
}

assert_family_profile() {
    python3 - "$1" <<'PY' || die "family profile restore assertion failed (STOP; plan §5.2)"
import json
import sys

phases = json.load(open(sys.argv[1], encoding="utf-8"))["phases"]
plateau = next((phase for phase in phases if phase.get("name") == "compute_plateau"), None)
if plateau is None:
    raise SystemExit("missing compute_plateau")
if float(plateau.get("duration_s") or 0) != 600.0:
    raise SystemExit(f"compute_plateau duration {plateau.get('duration_s')} != 600 s")
if float(plateau.get("rate_per_client") or 0) != 2.0:
    raise SystemExit(f"compute_plateau rate {plateau.get('rate_per_client')} != 2.0")
if float((plateau.get("mix") or {}).get("service_pressure") or 0) != 1.0:
    raise SystemExit("compute_plateau mix is not pure service_pressure")
print("family profile restore assertion: PASS")
PY
}

sha256_of() {
    python3 - "$1" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

restore_snapshot() {
    local source=""
    if [[ -n "${RQ3ADM_SNAPSHOT_FILE:-}" ]]; then
        [[ -f "$RQ3ADM_SNAPSHOT_FILE" ]] || die "RQ3ADM_SNAPSHOT_FILE not found: $RQ3ADM_SNAPSHOT_FILE"
        source="$RQ3ADM_SNAPSHOT_FILE"
    else
        local glob candidate
        for glob in $SET_SNAPSHOT_GLOB; do
            candidate="$(find "$METRICS_DIR" -mindepth 1 -maxdepth 1 -type d -name "$glob" \
                -print 2>/dev/null | sort | head -n 1 || true)"
            if [[ -n "$candidate" && -f "$candidate/phases_snapshot.json" ]]; then
                source="$candidate/phases_snapshot.json"
                break
            fi
        done
    fi
    [[ -n "$source" ]] || die "no archived phases_snapshot.json found for set '$SET_NAME' (searched metrics/ for: $SET_SNAPSHOT_GLOB)"
    assert_set_profile "$source" "archived snapshot $source"
    local snapshot_hash
    snapshot_hash="$(sha256_of "$source")"
    cp "$source" "$PHASES_PATH"
    local installed_hash
    installed_hash="$(sha256_of "$PHASES_PATH")"
    [[ "$installed_hash" == "$snapshot_hash" ]] \
        || die "installed phases.json is not byte-identical to the snapshot (STOP)"
    assert_set_profile "$PHASES_PATH" "installed canonical phases.json"
    SNAPSHOT_SOURCE="$source"
    SNAPSHOT_SHA256="$snapshot_hash"
}

restore_family() {
    mkdir -p "$PLAN_ANALYSIS"
    # Tree-agnostic: HEAD carries the family profile only in the frozen execution trees, so a
    # tree-local `git checkout` is never used — restore from a hash-verified copy and assert it.
    local canonical_hash="" reference
    if [[ -f "$PLAN_ANALYSIS/canonical_hashes.json" ]]; then
        canonical_hash="$(python3 - "$PLAN_ANALYSIS/canonical_hashes.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print((data.get("phases_json") or {}).get("sha256", ""))
PY
)"
    fi
    if [[ -n "$canonical_hash" ]]; then
        reference="analysis/canonical_hashes.json"
    else
        canonical_hash="$FAMILY_PROFILE_SHA256"
        reference="built-in recorded canonical hash (preflight record absent)"
    fi

    local candidate candidate_hash candidates=() family_trees=()
    if [[ -n "${RQ3ADM_FAMILY_PROFILE_FILE:-}" ]]; then
        candidates+=("$RQ3ADM_FAMILY_PROFILE_FILE")
    fi
    if [[ -n "${RQ3ADM_FAMILY_TREES:-}" ]]; then
        read -r -a family_trees <<< "$RQ3ADM_FAMILY_TREES"
    else
        family_trees=("$HOME/rq3tim_frozen" "$HOME/rq3snd_frozen2")
    fi
    local tree
    for tree in "${family_trees[@]}"; do
        candidates+=("$tree/source/scripts/testing/phases.json")
    done
    local source=""
    for candidate in "${candidates[@]}"; do
        [[ -f "$candidate" ]] || continue
        candidate_hash="$(sha256_of "$candidate")"
        if [[ "$candidate_hash" == "$canonical_hash" ]]; then
            source="$candidate"
            break
        fi
    done
    [[ -n "$source" ]] \
        || die "no verified family profile source found (expected sha256 $canonical_hash; tried: ${candidates[*]:-none}) — set RQ3ADM_FAMILY_PROFILE_FILE or RQ3ADM_FAMILY_TREES to the frozen trees"
    assert_family_profile "$source"
    cp "$source" "$PHASES_PATH"
    local installed_hash
    installed_hash="$(sha256_of "$PHASES_PATH")"
    [[ "$installed_hash" == "$canonical_hash" ]] \
        || die "installed phases.json hash differs from the canonical reference (STOP): $installed_hash != $canonical_hash"
    assert_family_profile "$PHASES_PATH"
    python3 - "$REPO_ROOT" "$installed_hash" "$canonical_hash" "$source" "$reference" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
record = {
    "event": "family_profile_restore",
    "phases_path": "source/scripts/testing/phases.json",
    "installed_sha256": sys.argv[2],
    "canonical_hash": sys.argv[3] or None,
    "canonical_hash_matches": bool(sys.argv[3]) and sys.argv[3] == sys.argv[2],
    "restored_from": f"verified family phases.json copy: {sys.argv[4]}",
    "source_path": sys.argv[4],
    "source_sha256": sys.argv[2],
    "reference": sys.argv[5],
    "profile": {"compute_plateau": {"duration_s": 600, "rate_per_client": 2.0,
                                    "mix": {"service_pressure": 1.0}}},
    "assertion": "PASS",
    "recorded_at": datetime.now(timezone.utc).isoformat(),
}
target = root / "docs/operation/testing/experiment/v3/rq3_admission_modes/analysis/family_profile_restore.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"restore_record": str(target),
                  "source": sys.argv[4], "sha256": sys.argv[2]}, indent=2))
PY
    echo "family profile restored and asserted (recorded under analysis/family_profile_restore.json)"
    exit 0
}

if [[ $# -eq 1 && "$1" == "restore-family" ]]; then
    restore_family
fi

[[ $# -eq 4 ]] || die "usage: $0 <set> <arm> <label> <seed> | restore-family"
SET_NAME="$1"
ARM="$2"
LABEL="$3"
SEED="$4"
set_params "$SET_NAME"
[[ "$ARM" == "direct" || "$ARM" == "discovery" ]] || die "invalid arm '$ARM' (direct | discovery)"
[[ "$SEED" =~ ^[0-9]+$ ]] || die "invalid seed '$SEED'"
[[ "$LABEL" =~ $SET_LABEL_RE ]] || die "invalid label '$LABEL' for set '$SET_NAME'"

# Label/arm cross-check: the analyzer derives set/arm from the folder name, so
# a label that disagrees with the CLI args would silently poison analysis.
case "$SET_NAME" in
    camp)
        [[ "$LABEL" == "rq3_camp_direct_"* && "$ARM" == "direct" ]] \
            || [[ "$LABEL" == "rq3_camp_disc_"* && "$ARM" == "discovery" ]] \
            || die "label/arm mismatch: label must be rq3_camp_{direct|disc}_<n> matching the arm"
        ;;
    cell12)
        [[ "$LABEL" == "rq3_probe_cell12_direct_"* && "$ARM" == "direct" ]] \
            || [[ "$LABEL" == "rq3_probe_cell12_disc_"* && "$ARM" == "discovery" ]] \
            || die "label/arm mismatch: label must be rq3_probe_cell12_{direct|disc}_<n>"
        ;;
    disc15)
        [[ "$ARM" == "discovery" ]] \
            || die "the disc15 set is the discovery_15 sensitivity arm (discovery only)"
        ;;
esac

# Globally unique labels (run_matrix.md): a label that already has a run folder under metrics/ is refused.
existing_run="$(find "$METRICS_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name "*_${LABEL}" -print -quit 2>/dev/null || true)"
[[ -z "$existing_run" ]] || die "duplicate label already has a run folder: $existing_run"

[[ -f "$PHASES_PATH" ]] || die "missing canonical phases.json"
[[ -d "$METRICS_DIR" ]] || die "missing metrics directory: $METRICS_DIR"

readonly SET_RATE SET_WINDOW SET_EDGE_CPUS SET_LABEL_RE SET_SNAPSHOT_GLOB
SNAPSHOT_SOURCE=""
SNAPSHOT_SHA256=""
restore_snapshot

readonly DIRECT_ENV="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3_direct.env"
readonly DISCOVERY_ENV="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3_discovery.env"
readonly DISCOVERY15_ENV="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3_discovery_15.env"
case "$SET_NAME:$ARM" in
    *":direct") ARM_ENV="$DIRECT_ENV" ;;
    disc15:discovery) ARM_ENV="$DISCOVERY15_ENV" ;;
    *":discovery") ARM_ENV="$DISCOVERY_ENV" ;;
esac
[[ -f "$ARM_ENV" ]] || die "missing canonical arm env: $ARM_ENV"

# Per-run profile/env attestation (mirrors rq3snd_fault_attestation.txt).
ATTESTATION="$(mktemp "${TMPDIR:-/tmp}/rq3adm_attestation_XXXXXX.txt")"
TEMP_FILES+=("$ATTESTATION")
{
    echo "# RQ3 admission-modes supporting-run attestation"
    echo "label=$LABEL"
    echo "set=$SET_NAME"
    echo "arm=$ARM"
    echo "seed=$SEED"
    echo "phases_snapshot_source=$SNAPSHOT_SOURCE"
    echo "phases_snapshot_sha256=$SNAPSHOT_SHA256"
    echo "phases_installed_sha256=$(sha256_of "$PHASES_PATH")"
    echo "profile=baseline 60s@1.0 compute_spike 180s@$SET_RATE cleanup_gap 180s@0.5"
    echo "env_file=$ARM_ENV"
    echo "env_sha256=$(sha256_of "$ARM_ENV")"
    echo "inflight_window=$SET_WINDOW clients=3 edge_cpus=$SET_EDGE_CPUS mongo_pool=1"
} > "$ATTESTATION"

cd "$REPO_ROOT"
ulimit -n 65535
# Recorded v2 per-run reset cycle (rq3_preflight.md §8/§10): teardown_clients -> setup_network
# -> run; the env override goes to BOTH setup_network and run_experiment.
set +e
sudo -n make -C source/scripts teardown_clients
teardown_status=$?
sudo -n make -C source/scripts setup_network \
    OSKEN_ENV_OVERRIDE_FILE="$ARM_ENV" \
    EDGE_FLOW_ISOLATION=1 EDGE_MONGO_MAX_POOL_SIZE=1
setup_status=$?
sudo -n make -C source/scripts create_clients setup_test_data run_experiment \
    OSKEN_ENV_OVERRIDE_FILE="$ARM_ENV" \
    RUN_LABEL="$LABEL" \
    PHASES_CONFIG="testing/phases.json" \
    CLIENTS=3 CONTENT_ITEMS=3000 USERS=100 DATA_SEED=42 \
    TRAFFIC_DRIVER_MODE=open_loop CURL_MAX_TIME=300 INFLIGHT_WINDOW="$SET_WINDOW" DRAIN_S=30 \
    STORAGE_CPUS=0.08 EDGE_CPUS="$SET_EDGE_CPUS" WAN_RTT_MS=185 RANDOM_SEED="$SEED" \
    EDGE_MONGO_READ_PREFERENCE=secondaryPreferred EDGE_MONGO_MAX_POOL_SIZE=1 \
    VIP_DATA_PER_CONNECTION_FLOWS=1 EDGE_FLOW_ISOLATION=1 \
    SKIP_CLIENTS=0 SKIP_SEED=0 SKIP_SNAPSHOT=0
status=$?
set -e
if [[ $teardown_status -ne 0 || $setup_status -ne 0 ]]; then
    echo "WARN: reset-cycle make step failed (teardown=$teardown_status setup=$setup_status)" >&2
fi

# The run folder is root-owned (created by sudo make), so the post-run artifact writer
# must run under sudo (plan gate: quota_snapshot 0.30 + the attestation, §10).
sudo -n python3 - "$REPO_ROOT" "$LABEL" "$SET_EDGE_CPUS" "$status" "$ATTESTATION" <<'PY'
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
label = sys.argv[2]
quota = sys.argv[3]
attestation = Path(sys.argv[5])
metrics = root / "source/scripts/testing/metrics"
candidates = [path for path in metrics.iterdir()
              if path.is_dir() and path.name.endswith("_" + label)]
if candidates:
    run_dir = max(candidates, key=lambda path: path.stat().st_mtime)
    target_attestation = run_dir / "rq3adm_profile_attestation.txt"
    if attestation.is_file() and not target_attestation.exists():
        shutil.copyfile(attestation, target_attestation)
    names = subprocess.check_output(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], text=True
    ).splitlines()
    compute = [name for name in names if re.match(r"^edge_server(?:_n[12]|_lan[12]_dyn\d+)$", name)]
    expected = round(float(quota) * 1_000_000_000)
    inspected = []
    for name in compute:
        try:
            data = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
            value = data.get("HostConfig", {}).get("NanoCpus")
            inspected.append({"container": name, "nano_cpus": value, "matches": value == expected})
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
            inspected.append({"container": name, "nano_cpus": None, "matches": False})
    snapshot = {
        "requested_edge_cpus": quota,
        "expected_nano_cpus": expected,
        "containers": inspected,
        "all_compute_containers_match": bool(inspected) and all(item["matches"] for item in inspected),
    }
    target = run_dir / "quota_snapshot.json"
    if not target.exists():
        # Never overwrite a mid-run snapshot: by run end dynamic nodes are
        # torn down and only statics would be recorded.
        target.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
sys.exit(int(sys.argv[4]))
PY
