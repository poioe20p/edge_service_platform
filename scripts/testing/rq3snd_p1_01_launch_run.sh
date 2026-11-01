#!/usr/bin/env bash
# Guarded launcher for one RQ3 soundness run (frozen worktree, no active run). No rate axis:
# the Family-4 profile is ASSERTED, faults are env-injected, and the quota is FIXED at 0.12.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly MANIFEST="${RQ3SND_MANIFEST:-$REPO_ROOT/rq3snd_protected_surface.json}"
readonly IMAGE_STATE="${RQ3LH_IMAGE_STATE:-$REPO_ROOT/rq3lh_image_state.json}"
readonly EDGE_CPUS="0.12"
TEMP_ENV=""

die() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${TEMP_ENV:-}" && -f "$TEMP_ENV" ]]; then rm -f "$TEMP_ENV"; fi
    if [[ -n "${ATTESTATION:-}" && -f "$ATTESTATION" ]]; then rm -f "$ATTESTATION"; fi
}
trap cleanup EXIT

[[ $# -eq 4 ]] || die "usage: $0 <arm> <fault> <label> <seed>"
ARM="$1"
FAULT="$2"
LABEL="$3"
SEED="$4"
[[ "$ARM" == "event_only" || "$ARM" == "hybrid" || "$ARM" == "reconcile" || "$ARM" == "wake_verify" ]] \
    || die "invalid arm"
[[ "$FAULT" == "none" || "$FAULT" == "premature2" || "$FAULT" == "premature5" || "$FAULT" == "premature10" || "$FAULT" == "semantic" || "$FAULT" == "loss_all" ]] \
    || die "invalid fault"
# reruns: same ordinal + trailing 'r' (e.g. _90r; max 1 per R-run, plan §5)
[[ "$LABEL" =~ ^rq3snd_(none|premature2|premature5|premature10|semantic|loss_all)_(event_only|hybrid|reconcile|wake_verify)_[0-9]+r?$ ]] \
    || die "invalid label (expect rq3snd_<cell>_<arm>_<ordinal|block>[r])"
[[ "$SEED" =~ ^[0-9]+$ ]] || die "invalid seed"
# Label↔args cross-check: the analyzer derives cell/arm from the folder name, so a label
# that disagrees with the CLI args would silently poison analysis.
[[ "$LABEL" == "rq3snd_${FAULT}_${ARM}_"* ]] \
    || die "label/args mismatch: label must start with rq3snd_${FAULT}_${ARM}_"

# Globally unique labels (run_matrix.md): a label that already has a run folder under metrics/ is refused.
existing_run="$(find "$REPO_ROOT/source/scripts/testing/metrics" -mindepth 1 -maxdepth 1 \
    -type d -name "*_${LABEL}" -print -quit 2>/dev/null || true)"
[[ -z "$existing_run" ]] || die "duplicate label already has a run folder: $existing_run"

# Frozen Family-4 phase profile is asserted, not swept: a wrong-file edit must STOP, never run silently.
python3 - "$REPO_ROOT/source/scripts/testing/phases.json" <<'PY' || die "frozen phases profile mismatch (expect compute_plateau 600 s / rate_per_client 2.0 / service_pressure 1.0)"
import json
import sys

phases = json.load(open(sys.argv[1], encoding="utf-8"))["phases"]
plateau = next((phase for phase in phases if phase.get("name") == "compute_plateau"), None)
if plateau is None:
    raise SystemExit(1)
if float(plateau.get("duration_s") or 0) != 600.0:
    raise SystemExit(2)
if float(plateau.get("rate_per_client") or 0) != 2.0:
    raise SystemExit(3)
if float((plateau.get("mix") or {}).get("service_pressure") or 0) != 1.0:
    raise SystemExit(4)
PY

[[ -f "$MANIFEST" ]] || die "missing soundness manifest"
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
readonly PREMATURE2_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3snd_premature_lead2.env"
readonly PREMATURE5_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3snd_premature_lead5.env"
readonly PREMATURE10_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3snd_premature_lead10.env"
readonly SEMANTIC_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3snd_semantic.env"
readonly WAKE_VERIFY_DELTA="$REPO_ROOT/source/scripts/testing/controller_env_overrides/rq3snd_wake_verify.env"

BASE_ENV="$DIRECT_ENV"
ARM_DELTA=""
case "$ARM" in
    event_only) ARM_DELTA="$EVENT_ONLY_DELTA" ;;
    hybrid) ;;
    reconcile) BASE_ENV="$DISCOVERY_ENV" ;;
    wake_verify) ARM_DELTA="$WAKE_VERIFY_DELTA" ;;
esac
FAULT_DELTA=""
case "$FAULT" in
    none) ;;
    premature2) FAULT_DELTA="$PREMATURE2_DELTA" ;;
    premature5) FAULT_DELTA="$PREMATURE5_DELTA" ;;
    premature10) FAULT_DELTA="$PREMATURE10_DELTA" ;;
    semantic) FAULT_DELTA="$SEMANTIC_DELTA" ;;
    loss_all) FAULT_DELTA="$LOSS_ALL_DELTA" ;;
esac

TEMP_ENV="$(mktemp "${TMPDIR:-/tmp}/rq3snd_env_XXXXXX.env")"
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
    handle.write("# generated RQ3 soundness controller-env merge (rq3rob/rq3sat envs reused byte-identical)\n")
    for key in order:
        handle.write(out[key] + "\n")
PY
ENV_FILE="$TEMP_ENV"

# Fault attestation goes to a temp file, then into the run folder: a key absent from the
# merge records its code default explicitly.
ATTESTATION="$(mktemp "${TMPDIR:-/tmp}/rq3snd_attestation_XXXXXX.txt")"
python3 - "$ENV_FILE" "$ATTESTATION" "$LABEL" "$ARM" "$FAULT" "$SEED" <<'PY'
import sys

env_path, out_path, label, arm, fault, seed = sys.argv[1:7]
defaults = {
    "EDGE_READY_PREMATURE_S": "0",
    "EDGE_READY_SEMANTIC_LIE_S": "0",
    "READINESS_WAKE_VERIFY_MODE": "off",
    "READINESS_EVENT_FALLBACK_S": "5.0",
    "READINESS_EVENT_DROP_MODE": "off",
    "EDGE_APP_READY_EVENT": "0",
}
values = {}
for raw in open(env_path, encoding="utf-8"):
    text = raw.strip()
    if not text or text.startswith("#") or "=" not in text:
        continue
    key, _, value = text.partition("=")
    values[key.strip()] = value.strip()
lines = [
    "# RQ3 soundness fault attestation — resolved from the merged controller env.",
    "# A \"# default\" marker means the key was absent and the code default is recorded.",
    f"label={label}",
    f"arm={arm}",
    f"fault={fault}",
    f"seed={seed}",
]
for key in (
    "EDGE_READY_PREMATURE_S",
    "EDGE_READY_SEMANTIC_LIE_S",
    "READINESS_WAKE_VERIFY_MODE",
    "READINESS_EVENT_FALLBACK_S",
    "READINESS_EVENT_DROP_MODE",
    "EDGE_APP_READY_EVENT",
):
    if key in values:
        lines.append(f"{key}={values[key]}")
    else:
        lines.append(f"{key}={defaults[key]}  # default")
with open(out_path, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
PY

cd "$REPO_ROOT"
ulimit -n 65535
# Family-4 invocation surface (rq3tim make layer); the P0 full-invocation diff against the
# Family-4 launcher is the binding check — do not alter.
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

# The run folder is root-owned (created by sudo make), so the post-run artifact writer must
# run under sudo to write quota_snapshot + the fault attestation into it.
sudo -n python3 - "$REPO_ROOT" "$LABEL" "$EDGE_CPUS" "$status" "$ATTESTATION" <<'PY'
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
    target_attestation = run_dir / "rq3snd_fault_attestation.txt"
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
