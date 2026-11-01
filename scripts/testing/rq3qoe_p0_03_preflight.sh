#!/usr/bin/env bash
# Static checks for the isolated RQ3 QoE worktree.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly MANIFEST="${RQ3QOE_MANIFEST:-$REPO_ROOT/.rq3qoe_frozen_manifest.json}"
readonly IMAGE_STATE="${RQ3LH_IMAGE_STATE:-$REPO_ROOT/rq3lh_image_state.json}"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

[[ -f "$MANIFEST" ]] || die "missing qoe manifest: $MANIFEST"
[[ -f "$IMAGE_STATE" ]] || die "missing shared frozen image state: $IMAGE_STATE"
[[ -f "$REPO_ROOT/source/scripts/testing/phases.json" ]] || die "missing canonical phases.json"
[[ -f "$REPO_ROOT/source/scripts/testing/rq3qoe_p1_01_launch_run.sh" ]] || die "missing launcher"
[[ -f "$REPO_ROOT/source/scripts/testing/analysis/rq3/readiness_robustness.py" ]] || die "missing analyzer"

grep -q 'READINESS_EVENT_DROP_MODE", "off"' "$REPO_ROOT/source/sdn_controller/control_events.py" \
    || die "drop knob is not default off"
for env in rq3rob_event_only rq3rob_loss_all rq3rob_loss_alt rq3sat_direct rq3sat_discovery; do
    [[ -f "$REPO_ROOT/source/scripts/testing/controller_env_overrides/$env.env" ]] \
        || die "missing env delta: $env.env"
done

python3 - "$MANIFEST" "$IMAGE_STATE" "$REPO_ROOT" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
image_state = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
root = pathlib.Path(sys.argv[3]).resolve()
if pathlib.Path(manifest.get("worktree_root", "")).resolve() != root:
    raise SystemExit("manifest worktree_root does not match preflight root")
if manifest.get("frozen_base_commit") != "852a8a3":
    raise SystemExit("manifest frozen_base_commit is not 852a8a3")
if image_state.get("acquired") is not True:
    raise SystemExit("shared image guard is not acquired")
for relative, expected in manifest.get("arm_env_sha256", {}).items():
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"env hash differs from manifest: {relative}")
print("RQ3 QoE static manifest checks: PASS")
PY

bash -n "$SCRIPT_DIR/rq3qoe_p1_01_launch_run.sh"
python3 -m py_compile \
    "$SCRIPT_DIR/rq3qoe_p0_01_prepare_tag.py" \
    "$SCRIPT_DIR/rq3qoe_p0_02_analyzer_selftest.py" \
    "$SCRIPT_DIR/analysis/rq3/readiness_robustness.py"

echo "RQ3 QoE preflight preparation checks: PASS"
