#!/usr/bin/env bash
# Static checks for the isolated RQ3 soundness worktree (plan §9).
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly MANIFEST="${RQ3SND_MANIFEST:-$REPO_ROOT/rq3snd_protected_surface.json}"
readonly IMAGE_STATE="${RQ3LH_IMAGE_STATE:-$REPO_ROOT/rq3lh_image_state.json}"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

[[ -f "$MANIFEST" ]] || die "missing soundness manifest: $MANIFEST"
[[ -f "$IMAGE_STATE" ]] || die "missing shared frozen image state: $IMAGE_STATE"
[[ -f "$REPO_ROOT/source/scripts/testing/phases.json" ]] || die "missing canonical phases.json"
[[ -f "$REPO_ROOT/source/scripts/testing/rq3snd_p1_01_launch_run.sh" ]] || die "missing launcher"
[[ -f "$REPO_ROOT/source/scripts/testing/rq3snd_p0_02_analyzer_selftest.py" ]] || die "missing analyzer selftest"
[[ -f "$REPO_ROOT/source/scripts/testing/analysis/rq3/readiness_robustness.py" ]] || die "missing analyzer"

# Default-off knob literals (an absent key must stay inert on the off-path).
grep -q 'READINESS_EVENT_DROP_MODE", "off"' "$REPO_ROOT/source/sdn_controller/control_events.py" \
    || die "drop knob is not default off"
grep -q 'EDGE_READY_PREMATURE_S", "0"' "$REPO_ROOT/source/docker/edge_server/source/app.py" \
    || die "premature knob is not default 0"
grep -q 'EDGE_READY_SEMANTIC_LIE_S", "0"' "$REPO_ROOT/source/docker/edge_server/source/app.py" \
    || die "semantic knob is not default 0"
grep -q 'READINESS_WAKE_VERIFY_MODE", "off"' "$REPO_ROOT/source/sdn_controller/scaling_config.py" \
    || die "wake_verify knob is not default off"
grep -q 'EDGE_APP_READY_EVENT", "0"' "$REPO_ROOT/source/docker/edge_server/source/edge_server_process_state.py" \
    || die "app_ready event is not default off"
grep -q 'EDGE_READY_PREMATURE_S' "$REPO_ROOT/source/sdn_controller/elasticity/compute_node_manager.py" \
    || die "compute_node_manager.py lacks EDGE_READY_PREMATURE_S pass-through"
grep -q 'EDGE_READY_SEMANTIC_LIE_S' "$REPO_ROOT/source/sdn_controller/elasticity/compute_node_manager.py" \
    || die "compute_node_manager.py lacks EDGE_READY_SEMANTIC_LIE_S pass-through"
for env in rq3snd_premature_lead2 rq3snd_premature_lead5 rq3snd_premature_lead10 rq3snd_semantic rq3snd_wake_verify rq3rob_event_only rq3rob_loss_all rq3sat_direct rq3sat_discovery; do
    [[ -f "$REPO_ROOT/source/scripts/testing/controller_env_overrides/$env.env" ]] \
        || die "missing env delta: $env.env"
done

# Quota is FIXED at 0.12 (Family-4 lock).
grep -q 'readonly EDGE_CPUS="0.12"' "$REPO_ROOT/source/scripts/testing/rq3snd_p1_01_launch_run.sh" \
    || die "launcher does not fix EDGE_CPUS=0.12"

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
missing = manifest.get("missing") or []
if missing:
    raise SystemExit(
        "manifest records missing protected paths: " + ", ".join(sorted(missing))
    )
for section in ("arm_env_sha256", "protected_sha256"):
    for relative, expected in manifest.get(section, {}).items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"{section} hash differs from manifest: {relative}")
print("RQ3 soundness static manifest checks: PASS")
PY

python3 - "$REPO_ROOT/source/scripts/testing/phases.json" <<'PY' || die "phases.json is not the frozen Family-4 profile (compute_plateau 600 s / rate 2.0 / service_pressure 1.0)"
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
print("RQ3 soundness phase-profile assertion: PASS")
PY

python3 - "$REPO_ROOT" <<'PY' || die "env-merge proof failed (off-path must stay byte-identical)"
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
overrides = root / "source/scripts/testing/controller_env_overrides"


def load(path):
    order = []
    lines = {}
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key = text.split("=", 1)[0].strip()
            if key not in lines:
                order.append(key)
            lines[key] = line
    return order, lines


def merge(paths):
    order = []
    lines = {}
    for path in paths:
        path_order, path_lines = load(path)
        for key in path_order:
            if key not in lines:
                order.append(key)
            lines[key] = path_lines[key]
    return order, lines


def payload(paths):
    order, lines = merge(paths)
    return "\n".join(lines[key] for key in order) + "\n"


def raw_payload(path):
    # Comment/blank-free env lines, exactly as the merge loader consumes them.
    lines = []
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            lines.append(line)
    return "\n".join(lines) + "\n"


def value(lines, key):
    return lines[key].split("=", 1)[1].strip()


direct = overrides / "rq3sat_direct.env"
discovery = overrides / "rq3sat_discovery.env"
event_only = overrides / "rq3rob_event_only.env"
wake_verify = overrides / "rq3snd_wake_verify.env"

# None-cell composition table exactly as the launcher resolves it
# (arm + no fault delta). Only the listed arm-delta keys may change;
# fault knobs must resolve to their inert default (or stay absent).
none_cells = {
    "event_only": ([direct, event_only], {"READINESS_EVENT_FALLBACK_S"}),
    "hybrid": ([direct], set()),
    "reconcile": ([discovery], set()),
    "wake_verify": ([direct, wake_verify],
                    {"READINESS_WAKE_VERIFY_MODE", "READINESS_EVENT_FALLBACK_S"}),
}
for arm, (paths, allowed_changes) in none_cells.items():
    if raw_payload(paths[0]) != payload([paths[0]]):
        raise SystemExit(f"base env is not merge-stable: {paths[0].name}")
    if arm in ("hybrid", "reconcile") and payload(paths) != raw_payload(paths[0]):
        raise SystemExit(f"{arm} none-cell merge is not byte-identical to the base env file")
    order, merged = merge(paths)
    base_order, base = merge([paths[0]])
    changed = {key for key in set(merged) | set(base)
               if merged.get(key) != base.get(key)}
    if not changed <= allowed_changes:
        raise SystemExit(
            f"{arm} none-cell merge changes unexpected keys: "
            f"{sorted(changed - allowed_changes)}"
        )
    for key, inert in (("EDGE_READY_PREMATURE_S", "0"),
                       ("EDGE_READY_SEMANTIC_LIE_S", "0"),
                       ("READINESS_EVENT_DROP_MODE", "off")):
        if key in merged and value(merged, key) != inert:
            raise SystemExit(f"{arm} none-cell merge carries {key} not default-{inert}")
    if "READINESS_WAKE_VERIFY_MODE" in merged:
        if arm != "wake_verify" or value(merged, "READINESS_WAKE_VERIFY_MODE") != "probe":
            raise SystemExit(f"{arm} none-cell merge carries an unexpected wake_verify mode")
if value(merge([direct, event_only])[1], "READINESS_EVENT_FALLBACK_S") != "130.0":
    raise SystemExit("event_only delta does not override READINESS_EVENT_FALLBACK_S to 130.0")
if value(merge([direct, wake_verify])[1], "READINESS_WAKE_VERIFY_MODE") != "probe":
    raise SystemExit("wake_verify delta does not set READINESS_WAKE_VERIFY_MODE=probe")
if float(value(merge([direct, wake_verify])[1], "READINESS_EVENT_FALLBACK_S")) != 20.0:
    raise SystemExit("wake_verify delta does not keep READINESS_EVENT_FALLBACK_S at 20.0")

# Fault deltas resolve to the pre-registered values (§3/§7.3) when merged
# over the direct base (fault env names live in the launcher's fault table).
fault_cases = (
    ("premature2", overrides / "rq3snd_premature_lead2.env", "EDGE_READY_PREMATURE_S", "2"),
    ("premature5", overrides / "rq3snd_premature_lead5.env", "EDGE_READY_PREMATURE_S", "5"),
    ("premature10", overrides / "rq3snd_premature_lead10.env", "EDGE_READY_PREMATURE_S", "10"),
    ("semantic", overrides / "rq3snd_semantic.env", "EDGE_READY_SEMANTIC_LIE_S", "1200"),
    ("loss_all", overrides / "rq3rob_loss_all.env", "READINESS_EVENT_DROP_MODE", "all"),
)
for name, delta, key, expected in fault_cases:
    _, merged = merge([direct, delta])
    if key not in merged or value(merged, key) != expected:
        raise SystemExit(f"fault delta mismatch for {name}: {key} != {expected}")
print("RQ3 soundness env-merge proof: PASS")
PY

bash -n "$SCRIPT_DIR/rq3snd_p1_01_launch_run.sh"

# Launcher negative tests (plan §9): each case must die during argument validation, BEFORE
# any action — a valid invocation would execute a real run and create a metrics folder.
readonly LAUNCHER="$REPO_ROOT/source/scripts/testing/rq3snd_p1_01_launch_run.sh"
readonly METRICS_DIR="$REPO_ROOT/source/scripts/testing/metrics"

metrics_dir_count() {
    find "$METRICS_DIR" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | wc -l || true
}

launcher_negative() {
    local name="$1"
    shift
    local before after status
    before="$(metrics_dir_count)"
    set +e
    bash "$LAUNCHER" "$@" >/dev/null 2>&1
    status=$?
    set -e
    after="$(metrics_dir_count)"
    [[ $status -ne 0 ]] || die "launcher negative (${name}): unexpectedly succeeded"
    [[ "$before" == "$after" ]] \
        || die "launcher negative (${name}): metrics/ changed ${before} -> ${after}"
    echo "launcher negative (${name}): rejected exit=${status}; metrics/ unchanged"
}

launcher_negative "unknown arm"         bogus      none        rq3snd_none_event_only_1      1
launcher_negative "unknown fault"       event_only bogus       rq3snd_none_event_only_1      1
launcher_negative "malformed label"     event_only none        not_a_label                   1
launcher_negative "label/args mismatch" event_only premature10 rq3snd_semantic_reconcile_92  1

readonly COMPILE_FILES=(
    "$SCRIPT_DIR/rq3snd_p0_01_prepare_tag.py"
    "$SCRIPT_DIR/rq3snd_p0_02_analyzer_selftest.py"
    "$SCRIPT_DIR/analysis/rq3/readiness_robustness.py"
    "$REPO_ROOT/source/docker/edge_server/source/app.py"
    "$REPO_ROOT/source/docker/edge_server/source/edge_server_process_state.py"
    "$REPO_ROOT/source/sdn_controller/readiness_gate.py"
    "$REPO_ROOT/source/sdn_controller/control_events.py"
    "$REPO_ROOT/source/sdn_controller/scaling_config.py"
    "$REPO_ROOT/source/sdn_controller/main_n1.py"
    "$REPO_ROOT/source/sdn_controller/main_n2.py"
    "$REPO_ROOT/source/sdn_controller/elasticity/compute_node_manager.py"
)
python3 -m py_compile "${COMPILE_FILES[@]}"

if python3 -c "import pyflakes" >/dev/null 2>&1; then
    PYFLAKES_CMD=(python3 -m pyflakes)
elif command -v pyflakes >/dev/null 2>&1; then
    PYFLAKES_CMD=(pyflakes)
else
    PYFLAKES_CMD=()
fi
if [[ ${#PYFLAKES_CMD[@]} -gt 0 ]]; then
    "${PYFLAKES_CMD[@]}" "${COMPILE_FILES[@]}" || die "pyflakes findings on the compile surface"
    echo "pyflakes: PASS"
else
    echo "SKIP (pyflakes not installed)"
fi

echo "RQ3 soundness preflight preparation checks: PASS"
