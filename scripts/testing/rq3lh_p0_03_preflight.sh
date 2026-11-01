#!/usr/bin/env bash
# Static checks for the detached RQ3 low-headroom worktree.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly MANIFEST="${RQ3LH_MANIFEST:-$REPO_ROOT/.rq3lh_frozen_manifest.json}"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

[[ -f "$MANIFEST" ]] || die "missing frozen manifest: $MANIFEST"
[[ -f "$REPO_ROOT/source/scripts/testing/phases.json" ]] || die "missing canonical phases.json"
[[ -f "$REPO_ROOT/source/scripts/testing/rq3lh_p1_01_launch_run.sh" ]] || die "missing launcher"
[[ -f "$REPO_ROOT/source/scripts/testing/analysis/rq3/readiness_low_headroom.py" ]] || die "missing analyzer"

python3 - "$MANIFEST" "$REPO_ROOT" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
root = pathlib.Path(sys.argv[2]).resolve()
if manifest.get("dry_run"):
    raise SystemExit("dry-run manifest cannot be used for a VM preflight")
if pathlib.Path(manifest["worktree_root"]).resolve() != root:
    raise SystemExit("manifest worktree_root does not match launcher root")
phase = root / "source/scripts/testing/phases.json"
data = json.loads(phase.read_text(encoding="utf-8"))
names = [item.get("name") for item in data.get("phases", data)]
expected = ["baseline", "compute_plateau", "recovery_gap", "demand_drop", "idle_tail"]
if names != expected:
    raise SystemExit(f"unexpected canonical phase names: {names}")
digest = hashlib.sha256(phase.read_bytes()).hexdigest()
if manifest.get("phase_sha256") != digest:
    raise SystemExit("canonical phases hash differs from frozen manifest")
if manifest.get("tag_commit") != "55c22fbf3a5ab907ce90601fb31dea82ad705e2c":
    raise SystemExit("unexpected frozen tag commit")
if manifest.get("controller_commit") != "d26709935c72795bcf6d744c5b3b839aeb77f559":
    raise SystemExit("unexpected controller commit")
for relative, expected in manifest.get("arm_env_sha256", {}).items():
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"arm env hash differs from manifest: {relative}")
print("RQ3 low-headroom static manifest checks: PASS")
PY

bash -n "$SCRIPT_DIR/rq3lh_p1_01_launch_run.sh"
python3 -m py_compile \
    "$SCRIPT_DIR/rq3lh_p0_01_prepare_frozen.py" \
    "$SCRIPT_DIR/rq3lh_p0_02_analyzer_selftest.py" \
    "$SCRIPT_DIR/analysis/rq3/readiness_low_headroom.py"

echo "RQ3 low-headroom preflight preparation checks: PASS"
