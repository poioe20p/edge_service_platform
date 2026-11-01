#!/usr/bin/env bash
# Guarded launcher for one RQ3 low-headroom preparation run.
# This file is intentionally not invoked by implementation or local validation.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly MANIFEST="${RQ3LH_MANIFEST:-$REPO_ROOT/.rq3lh_frozen_manifest.json}"
readonly IMAGE_STATE="${RQ3LH_IMAGE_STATE:-$REPO_ROOT/rq3lh_image_state.json}"
TEMP_ENV=""

die() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${TEMP_ENV:-}" && -f "$TEMP_ENV" ]]; then
        rm -f "$TEMP_ENV"
    fi
}
trap cleanup EXIT

[[ $# -eq 4 ]] || die "usage: $0 <direct|discovery|event_absent> <label> <seed> <edge_cpus>"
MODE="$1"
LABEL="$2"
SEED="$3"
EDGE_CPUS="$4"
[[ "$MODE" == "direct" || "$MODE" == "discovery" || "$MODE" == "event_absent" ]] || die "invalid mode"
[[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || die "invalid label"
[[ "$SEED" =~ ^[0-9]+$ ]] || die "invalid seed"
[[ "$EDGE_CPUS" == "0.10" || "$EDGE_CPUS" == "0.12" || "$EDGE_CPUS" == "0.15" ]] || die "quota must be 0.10, 0.12, or 0.15"

[[ -f "$MANIFEST" ]] || die "missing frozen manifest"
[[ -f "$IMAGE_STATE" ]] || die "missing image state record"
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
readonly ABSENT_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3lh_direct_event_absent.env"

case "$MODE" in
    direct)
        ENV_FILE="$DIRECT_ENV"
        ;;
    discovery)
        ENV_FILE="$DISCOVERY_ENV"
        ;;
    event_absent)
        TEMP_ENV="$(mktemp "${TMPDIR:-/tmp}/rq3lh_env_XXXXXX.env")"
        python3 - "$DIRECT_ENV" "$ABSENT_DELTA" "$TEMP_ENV" <<'PY'
import sys

out = {}
order = []
for name in sys.argv[1:3]:
    for raw in open(name, encoding="utf-8"):
        line = raw.rstrip("\r\n")
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key = text.split("=", 1)[0].strip()
        if key not in out:
            order.append(key)
        out[key] = line
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    handle.write("# generated RQ3 low-headroom event-source-absence merge\n")
    for key in order:
        handle.write(out[key] + "\n")
PY
        ENV_FILE="$TEMP_ENV"
        ;;
    *)
        die "unsupported mode: $MODE"
        ;;
esac

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
    STORAGE_CPUS=0.08 EDGE_CPUS="$EDGE_CPUS" WAN_RTT_MS=185 RANDOM_SEED="$SEED" \
    EDGE_MONGO_READ_PREFERENCE=secondaryPreferred EDGE_MONGO_MAX_POOL_SIZE=6 \
    VIP_DATA_PER_CONNECTION_FLOWS=1 EDGE_FLOW_ISOLATION=1 \
    SKIP_CLIENTS=1 SKIP_SEED=1 SKIP_SNAPSHOT=1
status=$?
set -e

python3 - "$REPO_ROOT" "$LABEL" "$EDGE_CPUS" <<'PY'
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
    (run_dir / "quota_snapshot.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
sys.exit(status)
PY
