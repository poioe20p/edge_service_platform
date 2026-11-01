#!/usr/bin/env bash
# Guarded launcher for one RQ3 robustness run (frozen worktree, no active run).
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly MANIFEST="${RQ3ROB_MANIFEST:-$REPO_ROOT/.rq3rob_frozen_manifest.json}"
readonly IMAGE_STATE="${RQ3LH_IMAGE_STATE:-$REPO_ROOT/rq3lh_image_state.json}"
TEMP_ENV=""
TEMP_PLAN=""

die() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${TEMP_ENV:-}" && -f "$TEMP_ENV" ]]; then rm -f "$TEMP_ENV"; fi
    if [[ -n "${TEMP_PLAN:-}" && -f "$TEMP_PLAN" ]]; then rm -f "$TEMP_PLAN"; fi
}
trap cleanup EXIT

[[ $# -ge 4 && $# -le 5 ]] || die "usage: $0 <arm> <fault> <label> <seed> [restart_offset_s]"
ARM="$1"
FAULT="$2"
LABEL="$3"
SEED="$4"
RESTART_OFFSET="${5:-45}"
[[ "$ARM" == "event_only" || "$ARM" == "hybrid" || "$ARM" == "reconcile" ]] || die "invalid arm"
[[ "$FAULT" == "none" || "$FAULT" == "loss_all" || "$FAULT" == "loss_alt" || "$FAULT" == "restart" ]] || die "invalid fault"
[[ "$LABEL" =~ ^rq3rob_[a-z_]+_[a-z_]+_[0-6]$ ]] || die "invalid label (expect rq3rob_<cell>_<arm>_<block>)"
[[ "$SEED" =~ ^[0-9]+$ ]] || die "invalid seed"
[[ "$RESTART_OFFSET" =~ ^[0-9]+$ ]] || die "invalid restart offset"

[[ -f "$MANIFEST" ]] || die "missing robustness manifest"
[[ -f "$IMAGE_STATE" ]] || die "missing shared image state record"
python3 - "$MANIFEST" "$IMAGE_STATE" "$REPO_ROOT" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
image_state = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
root = pathlib.Path(sys.argv[3]).resolve()
if pathlib.Path(manifest.get("worktree_root", "")).resolve() != root:
    raise SystemExit("manifest root mismatch")
if image_state.get("acquired") is not True:
    raise SystemExit("image guard is not acquired")
PY

readonly PHASES="testing/phases.json"
readonly DIRECT_ENV="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3sat_direct.env"
readonly DISCOVERY_ENV="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3sat_discovery.env"
readonly EVENT_ONLY_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3rob_event_only.env"
readonly LOSS_ALL_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3rob_loss_all.env"
readonly LOSS_ALT_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3rob_loss_alt.env"

BASE_ENV="$DIRECT_ENV"
ARM_DELTA=""
case "$ARM" in
    event_only) ARM_DELTA="$EVENT_ONLY_DELTA" ;;
    hybrid) ;;
    reconcile) BASE_ENV="$DISCOVERY_ENV" ;;
esac
FAULT_DELTA=""
case "$FAULT" in
    none) ;;
    loss_all) FAULT_DELTA="$LOSS_ALL_DELTA" ;;
    loss_alt) FAULT_DELTA="$LOSS_ALT_DELTA" ;;
esac

TEMP_ENV="$(mktemp "${TMPDIR:-/tmp}/rq3rob_env_XXXXXX.env")"
python3 - "$BASE_ENV" "$ARM_DELTA" "$FAULT_DELTA" "$TEMP_ENV" <<'PY'
import sys

out = {}
order = []
for name in sys.argv[1:4]:
    if not name:
        continue
    for raw in open(name, encoding="utf-8"):
        line = raw.rstrip("\r\n")
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key = text.split("=", 1)[0].strip()
        if key not in out:
            order.append(key)
        out[key] = line
with open(sys.argv[4], "w", encoding="utf-8") as handle:
    handle.write("# generated RQ3 robustness controller-env merge\n")
    for key in order:
        handle.write(out[key] + "\n")
PY
ENV_FILE="$TEMP_ENV"

FAULT_PLAN_ARG=""
if [[ "$FAULT" == "restart" ]]; then
    TEMP_PLAN="$(mktemp "${TMPDIR:-/tmp}/rq3rob_plan_XXXXXX.json")"
    python3 - "$RESTART_OFFSET" "$TEMP_PLAN" <<'PY'
import json
import sys

plan = {
    "actions": [{
        "name": "controller_restart_lan1",
        "phase": "compute_plateau",
        "after_s": float(sys.argv[1]),
        "selector": {"domain": "n1", "mode": "restart"},
        "action": {"type": "docker_restart", "timeout_s": 15.0},
    }]
}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(plan, handle, indent=2)
PY
    FAULT_PLAN_ARG="FAULT_PLAN=$TEMP_PLAN"
fi

cd "$REPO_ROOT"
ulimit -n 65535
set +e
sudo -n make -C source/scripts \
    setup_network create_clients setup_test_data run_experiment \
    OSKEN_ENV_OVERRIDE_FILE="$ENV_FILE" \
    RUN_LABEL="$LABEL" \
    PHASES_CONFIG="$PHASES" \
    CLIENTS=24 CONTENT_ITEMS=3000 USERS=100 DATA_SEED=42 \
    TRAFFIC_DRIVER_MODE=open_loop CURL_MAX_TIME=300 INFLIGHT_WINDOW=1024 DRAIN_S=30 \
    STORAGE_CPUS=0.08 EDGE_CPUS=0.15 WAN_RTT_MS=185 RANDOM_SEED="$SEED" \
    EDGE_MONGO_READ_PREFERENCE=secondaryPreferred EDGE_MONGO_MAX_POOL_SIZE=6 \
    VIP_DATA_PER_CONNECTION_FLOWS=1 EDGE_FLOW_ISOLATION=1 \
    $FAULT_PLAN_ARG \
    SKIP_CLIENTS=1 SKIP_SEED=1 SKIP_SNAPSHOT=1
status=$?
set -e

# The run folder is root-owned (created by sudo make); run the post-run
# quota snapshot writer under sudo so it can write into it (plan gate:
# "quota_snapshot matches").
sudo -n python3 - "$REPO_ROOT" "$LABEL" "0.15" "$status" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
label = sys.argv[2]
quota = sys.argv[3]
metrics = root / "source/scripts/testing/metrics"
candidates = [path for path in metrics.iterdir()
              if path.is_dir() and path.name.endswith("_" + label)]
if candidates:
    run_dir = max(candidates, key=lambda path: path.stat().st_mtime)
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
