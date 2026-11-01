#!/usr/bin/env bash
# Static checks for the RQ3 admission-modes completion campaign (plan §12.2).
#
# Checks run against the EXECUTION trees that actually launch the runs, never the operator
# tree; with no --exec-tree the execution-tree checks and env-merge proof report SKIP.
#
# Exit codes: 0 = every check passed; 1 = the first failing check (reason on stderr);
#             2 = usage error. There is no partial pass.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
readonly LAUNCHER="$REPO_ROOT/source/scripts/testing/rq3adm_p1_01_launch_run.sh"
readonly LABEL_CHECK="$REPO_ROOT/source/scripts/testing/rq3adm_p0_04_label_check.sh"
readonly OVERRIDES="$REPO_ROOT/source/scripts/testing/controller_env_overrides"
readonly PHASES="$REPO_ROOT/source/scripts/testing/phases.json"
readonly CANONICAL_ENV="$OVERRIDES/current_state_integrated.env"
readonly METRICS_DIR="$REPO_ROOT/source/scripts/testing/metrics"
readonly PLAN_ANALYSIS="$REPO_ROOT/docs/operation/testing/experiment/v3/rq3_admission_modes/analysis"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

SKIPPED_CHECKS=()
skip_check() {
    SKIPPED_CHECKS+=("$1")
    echo "SKIP ($2)"
}

EXEC_TREES=()
SUPPORT_TREE="${RQ3ADM_SUPPORT_TREE:-$REPO_ROOT}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --exec-tree)
            [[ $# -ge 2 && -n "$2" ]] || die "--exec-tree requires a path"
            EXEC_TREES+=("$2")
            shift 2
            ;;
        --exec-tree=*)
            [[ -n "${1#--exec-tree=}" ]] || die "--exec-tree requires a path"
            EXEC_TREES+=("${1#--exec-tree=}")
            shift
            ;;
        --support-tree)
            [[ $# -ge 2 && -n "$2" ]] || die "--support-tree requires a path"
            SUPPORT_TREE="$2"
            shift 2
            ;;
        --support-tree=*)
            [[ -n "${1#--support-tree=}" ]] || die "--support-tree requires a path"
            SUPPORT_TREE="${1#--support-tree=}"
            shift
            ;;
        -h|--help)
            echo 'usage: rq3adm_p0_02_preflight.sh [--exec-tree <execution tree>]...'
            echo '                                 [--support-tree <C3 execution tree>]'
            echo '       (or RQ3ADM_EXEC_TREES="<tree> <tree>", RQ3ADM_SUPPORT_TREE=<tree>)'
            echo '       exec-tree check classification: frozen-manifest presence first,'
            echo '       worktree basename as fallback; the support tree is the C3 group'
            echo '       (no drop-knob requirement); unclassified trees are generic.'
            exit 0
            ;;
        *)
            die "unknown argument: $1 (usage: $0 [--exec-tree <path>]... [--support-tree <path>])"
            ;;
    esac
done
if [[ ${#EXEC_TREES[@]} -eq 0 && -n "${RQ3ADM_EXEC_TREES:-}" ]]; then
    read -r -a EXEC_TREES <<< "$RQ3ADM_EXEC_TREES"
fi

[[ -f "$REPO_ROOT/source/scripts/testing/rq3adm_p0_01_platform_identity.py" ]] \
    || die "missing platform-identity script"
[[ -f "$LAUNCHER" ]] || die "missing launcher"
[[ -f "$LABEL_CHECK" ]] || die "missing frozen-tree label check"
[[ -f "$REPO_ROOT/source/scripts/testing/rq3adm_p0_03_analyzer_selftest.py" ]] \
    || die "missing analyzer selftest"
[[ -f "$REPO_ROOT/source/scripts/testing/analysis/rq3/readiness_robustness.py" ]] \
    || die "missing analyzer"
[[ -f "$PHASES" ]] || die "missing canonical phases.json"
[[ -f "$CANONICAL_ENV" ]] || die "missing canonical env override baseline"
[[ -f "$REPO_ROOT/tools/rq3_stratified_analysis.py" ]] || die "missing stratified tool"

# Cross-tree byte-identity is asserted only for the matrix arm/fault deltas both matrix
# groups share; the supporting-set files legitimately differ per tree (validated vs C3).
EXEC_MATRIX_ENVS=(rq3sat_direct rq3sat_discovery rq3rob_event_only rq3rob_loss_all)

support_output="$(python3 - "$REPO_ROOT" "$SUPPORT_TREE" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
tree = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "source/scripts/testing"))
import rq3adm_p0_01_platform_identity as p0

if not tree.is_dir():
    raise SystemExit(f"supporting/C3 execution tree not found: {tree}")
capture = p0.capture_support_surface(tree)
items: list = []
p0.compare_support_group(items, capture)
blocking = [item for item in items
            if item["required"] and item["status"] == "DRIFT"]
for item in blocking:
    print(f"  {item['item']}: {item['status']} — {item['note']}", file=sys.stderr)
if blocking:
    raise SystemExit("supporting/C3 execution-surface mismatch (STOP)")
absent = sorted(item["item"].rsplit(":", 1)[-1] for item in items
                if item["status"] == "UNDEFINED" and item["required"])
checked = [item["item"] for item in items if item["status"] == "MATCH"]
print(f"supporting/C3 execution tree {tree.name}: v2 env files + per-set "
      f"profile hashes + family profile -> PASS ({len(checked)} checks; drop "
      "knob not required for this group)")
if absent:
    print("SUPPORT-SET-ABSENT " + " ".join(absent))
PY
)" || die "supporting/C3 execution-tree checks failed"
while IFS= read -r support_line; do
    [[ -n "$support_line" ]] || continue
    case "$support_line" in
        SUPPORT-SET-ABSENT\ *)
            for absent_set in ${support_line#SUPPORT-SET-ABSENT }; do
                skip_check "supporting set ${absent_set} snapshot absent (VM-only, not checkable here)" \
                    "supporting set ${absent_set}: archived profile snapshot absent in the support tree — not checkable here (the supporting launcher and the P0 gate STOP if it is missing on the VM)"
            done
            ;;
        *) echo "$support_line" ;;
    esac
done <<< "$support_output"

if [[ ${#EXEC_TREES[@]} -eq 0 ]]; then
    skip_check "exec-tree checks — no --exec-tree given" \
        "execution-tree checks (drop knob / matrix env files / phases.json): no --exec-tree or RQ3ADM_EXEC_TREES given — pass the frozen worktrees on the VM, e.g. --exec-tree ~/rq3tim_frozen --exec-tree ~/rq3snd_frozen2; these checks never silently PASS on the operator tree"
else
    home_phases_sha="$(sha256sum "$PHASES" | cut -d' ' -f1)"
    for tree in "${EXEC_TREES[@]}"; do
        [[ -d "$tree" ]] || die "execution tree not found: $tree"
        if [[ "$(cd "$tree" && pwd -P)" == "$(cd "$SUPPORT_TREE" && pwd -P)" ]]; then
            echo "execution tree ${tree} [supporting]: covered by the supporting/C3 checks above (drop knob not required for this group)"
            continue
        fi
        exec_group="generic"
        if [[ -f "$tree/.rq3tim_frozen_manifest.json" ]]; then
            exec_group="timing"
        elif [[ -f "$tree/rq3snd_protected_surface.json" ]]; then
            exec_group="soundness"
        else
            case "$(basename "$tree")" in
                *rq3tim*) exec_group="timing" ;;
                *rq3snd*) exec_group="soundness" ;;
            esac
        fi
        grep -q 'READINESS_EVENT_DROP_MODE", "off"' \
            "$tree/source/sdn_controller/control_events.py" 2>/dev/null \
            || die "execution tree ${tree} [${exec_group}]: control_events.py lacks the default-off READINESS_EVENT_DROP_MODE drop knob (the lost condition would be silently inert)"
        for env in "${EXEC_MATRIX_ENVS[@]}"; do
            [[ -f "$tree/source/scripts/testing/controller_env_overrides/$env.env" ]] \
                || die "execution tree ${tree} [${exec_group}]: missing matrix env file: $env.env"
        done
        grep -q '^READINESS_EVENT_FALLBACK_S=20.0$' \
            "$tree/source/scripts/testing/controller_env_overrides/rq3sat_direct.env" \
            || die "execution tree ${tree} [${exec_group}]: rq3sat_direct.env is not the 20 s hybrid base"
        grep -q '^READINESS_PROPAGATION=discovery$' \
            "$tree/source/scripts/testing/controller_env_overrides/rq3sat_discovery.env" \
            || die "execution tree ${tree} [${exec_group}]: rq3sat_discovery.env is not the discovery base"
        grep -q '^DISCOVERY_POLL_INTERVAL_S=10.0$' \
            "$tree/source/scripts/testing/controller_env_overrides/rq3sat_discovery.env" \
            || die "execution tree ${tree} [${exec_group}]: rq3sat_discovery.env is not the 10 s poll base"
        grep -q '^READINESS_EVENT_FALLBACK_S=130.0$' \
            "$tree/source/scripts/testing/controller_env_overrides/rq3rob_event_only.env" \
            || die "execution tree ${tree} [${exec_group}]: rq3rob_event_only.env is not the inert-130 s delta"
        grep -q '^READINESS_EVENT_DROP_MODE=all$' \
            "$tree/source/scripts/testing/controller_env_overrides/rq3rob_loss_all.env" \
            || die "execution tree ${tree} [${exec_group}]: rq3rob_loss_all.env is not the drop-all fault delta"
        premature_env="$tree/source/scripts/testing/controller_env_overrides/rq3snd_premature_lead10.env"
        if [[ -f "$premature_env" ]]; then
            grep -q '^EDGE_READY_PREMATURE_S=10$' "$premature_env" \
                || die "execution tree ${tree} [${exec_group}]: rq3snd_premature_lead10.env is not the 10 s premature fault delta"
        elif [[ "$exec_group" == "soundness" ]]; then
            die "execution tree ${tree} [soundness]: missing rq3snd_premature_lead10.env"
        fi
        [[ "$(sha256sum "$tree/source/scripts/testing/phases.json" | cut -d' ' -f1)" \
           == "$home_phases_sha" ]] \
            || die "execution tree ${tree} [${exec_group}]: phases.json differs from the canonical family profile (operator sha ${home_phases_sha})"
        echo "execution tree ${tree} [${exec_group}]: drop knob + matrix env files + phases.json -> PASS"
    done
    if [[ ${#EXEC_TREES[@]} -gt 1 ]]; then
        for env in "${EXEC_MATRIX_ENVS[@]}"; do
            first_sha=""
            for tree in "${EXEC_TREES[@]}"; do
                [[ -f "$tree/source/scripts/testing/controller_env_overrides/$env.env" ]] || continue
                sha="$(sha256sum "$tree/source/scripts/testing/controller_env_overrides/$env.env" | cut -d' ' -f1)"
                if [[ -z "$first_sha" ]]; then
                    first_sha="$sha"
                elif [[ "$sha" != "$first_sha" ]]; then
                    die "matrix env file $env.env differs across the given execution trees (${tree}) — fail closed"
                fi
            done
        done
        echo "execution trees: matrix arm/fault env files byte-identical across ${#EXEC_TREES[@]} trees"
    fi
fi

# Canonical-file hashes are recorded (the campaign creates no variant file); phases.json
# must hold the family profile — the supporting-set restore is a runtime in-place edit.
mkdir -p "$PLAN_ANALYSIS"
python3 - "$REPO_ROOT" "$PHASES" "$CANONICAL_ENV" "$PLAN_ANALYSIS/canonical_hashes.json" <<'PY' \
    || die "canonical-file hash/assertion failed"
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
phases_path = Path(sys.argv[2])
env_path = Path(sys.argv[3])
out = Path(sys.argv[4])


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


phases = json.loads(phases_path.read_text(encoding="utf-8"))["phases"]
plateau = next((phase for phase in phases if phase.get("name") == "compute_plateau"), None)
if plateau is None:
    raise SystemExit("canonical phases.json has no compute_plateau phase")
if float(plateau.get("duration_s") or 0) != 600.0 \
        or float(plateau.get("rate_per_client") or 0) != 2.0 \
        or float((plateau.get("mix") or {}).get("service_pressure") or 0) != 1.0:
    raise SystemExit("canonical phases.json is not the family profile "
                     "(compute_plateau 600 s @2.0 pure service_pressure)")
record = {
    "phases_json": {
        "path": "source/scripts/testing/phases.json",
        "sha256": sha256(phases_path),
        "profile": "family: baseline 60 s @1.0 -> compute_plateau 600 s @2.0 "
                   "(pure service_pressure) -> recovery_gap -> demand_drop -> idle_tail",
    },
    "current_state_integrated_env": {
        "path": "source/scripts/testing/controller_env_overrides/current_state_integrated.env",
        "sha256": sha256(env_path),
        "note": "canonical override baseline; not modified by this campaign",
    },
}
out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("canonical-file hashes recorded:", json.dumps(
    {key: value["sha256"][:12] for key, value in record.items()}))
PY

if [[ ${#EXEC_TREES[@]} -eq 0 ]]; then
    skip_check "env-merge proof — no --exec-tree given" \
        "env-merge byte-identity proof: no --exec-tree/RQ3ADM_EXEC_TREES given — the proof reads the campaign env files and the launcher texts from the execution trees only and is never run against operator-tree copies; pass --exec-tree <frozen worktree> on the VM"
else
ENV_SOURCE=""
ENV_SOURCE_LABEL=""
for tree in "${EXEC_TREES[@]}"; do
    candidate="$tree/source/scripts/testing/controller_env_overrides"
    candidate_missing=0
    for env in rq3sat_direct rq3sat_discovery rq3rob_event_only rq3rob_loss_all \
               rq3snd_premature_lead10 rq3_direct rq3_discovery rq3_discovery_15; do
        [[ -f "$candidate/$env.env" ]] || candidate_missing=1
    done
    if [[ "$candidate_missing" -eq 0 ]]; then
        ENV_SOURCE="$candidate"
        ENV_SOURCE_LABEL="execution tree ${tree}"
        break
    fi
done
[[ -n "$ENV_SOURCE" ]] \
    || die "env-merge proof cannot run: no execution tree carries the full campaign env set (passed: ${EXEC_TREES[*]})"
TIM_LAUNCHER=""
SND_LAUNCHER=""
for tree in "${EXEC_TREES[@]}"; do
    tree_group="generic"
    if [[ -f "$tree/.rq3tim_frozen_manifest.json" ]]; then
        tree_group="timing"
    elif [[ -f "$tree/rq3snd_protected_surface.json" ]]; then
        tree_group="soundness"
    else
        case "$(basename "$tree")" in
            *rq3tim*) tree_group="timing" ;;
            *rq3snd*) tree_group="soundness" ;;
        esac
    fi
    if [[ "$tree_group" == "timing" && -z "$TIM_LAUNCHER" ]]; then
        TIM_LAUNCHER="$tree/source/scripts/testing/rq3tim_p1_01_launch_run.sh"
    elif [[ "$tree_group" == "soundness" && -z "$SND_LAUNCHER" ]]; then
        SND_LAUNCHER="$tree/source/scripts/testing/rq3snd_p1_01_launch_run.sh"
    fi
done
[[ -n "$TIM_LAUNCHER" ]] \
    || die "env-merge proof cannot run: no timing execution tree given (its launcher text is required)"
[[ -n "$SND_LAUNCHER" ]] \
    || die "env-merge proof cannot run: no soundness execution tree given (its launcher text is required)"
[[ -f "$TIM_LAUNCHER" ]] \
    || die "env-merge proof cannot run: ${TIM_LAUNCHER} missing in the timing execution tree"
[[ -f "$SND_LAUNCHER" ]] \
    || die "env-merge proof cannot run: ${SND_LAUNCHER} missing in the soundness execution tree"
[[ -f "$LAUNCHER" ]] \
    || die "env-merge proof cannot run: ${LAUNCHER} missing in the support tree"
python3 - "$ENV_SOURCE" \
    "$TIM_LAUNCHER" \
    "$SND_LAUNCHER" \
    "$LAUNCHER" <<'PY' \
    || die "env-merge proof failed (off-path must stay byte-identical; the composition model is derived from the launchers that execute and fails closed on divergence)"
import pathlib
import re
import sys

overrides = pathlib.Path(sys.argv[1])
launcher_texts = {
    "tim": pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"),
    "snd": pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"),
    "adm": pathlib.Path(sys.argv[4]).read_text(encoding="utf-8"),
}


def env_table(text):
    table = {}
    for match in re.finditer(
            r'readonly\s+([A-Z0-9_]+)="[^"]*/([A-Za-z0-9_]+\.env)"', text):
        table[match.group(1)] = match.group(2)
    return table


def require(text, pattern, label):
    if re.search(pattern, text) is None:
        raise SystemExit(f"launcher composition anchor missing ({label}): {pattern!r}")


# ---------------------------------------------------------------------------
# Derive the arm/fault composition from the launcher text (never trust the
# hardcoded model alone): every variable -> file mapping and every case branch
# the proof relies on is cross-asserted; any divergence fails closed.
# ---------------------------------------------------------------------------
tim_env = env_table(launcher_texts["tim"])
snd_env = env_table(launcher_texts["snd"])
adm_env = env_table(launcher_texts["adm"])

expected_tables = (
    ("tim", tim_env, {"DIRECT_ENV": "rq3sat_direct.env",
                      "DISCOVERY_ENV": "rq3sat_discovery.env",
                      "EVENT_ONLY_DELTA": "rq3rob_event_only.env",
                      "LOSS_ALL_DELTA": "rq3rob_loss_all.env"}),
    ("snd", snd_env, {"PREMATURE10_DELTA": "rq3snd_premature_lead10.env",
                      "DIRECT_ENV": "rq3sat_direct.env",
                      "DISCOVERY_ENV": "rq3sat_discovery.env"}),
    ("adm", adm_env, {"DIRECT_ENV": "rq3_direct.env",
                      "DISCOVERY_ENV": "rq3_discovery.env",
                      "DISCOVERY15_ENV": "rq3_discovery_15.env"}),
)
for launcher, table, expected in expected_tables:
    for var, name in expected.items():
        if table.get(var) != name:
            raise SystemExit(
                f"{launcher} launcher {var} maps to {table.get(var)!r}; the "
                f"composition model expects {name!r} — fail closed")

require(launcher_texts["tim"], r'BASE_ENV="\$DIRECT_ENV"', "tim base=direct")
require(launcher_texts["tim"],
        r'event_only\)\s*ARM_DELTA="\$EVENT_ONLY_DELTA"', "tim event_only delta")
require(launcher_texts["tim"],
        r'reconcile\)\s*BASE_ENV="\$DISCOVERY_ENV"', "tim reconcile base")
require(launcher_texts["tim"],
        r'loss_all\)\s*FAULT_DELTA="\$LOSS_ALL_DELTA"', "tim loss_all delta")
require(launcher_texts["snd"],
        r'premature10\)\s*FAULT_DELTA="\$PREMATURE10_DELTA"', "snd premature10 delta")
require(launcher_texts["snd"],
        r'reconcile\)\s*BASE_ENV="\$DISCOVERY_ENV"', "snd reconcile base")
require(launcher_texts["adm"],
        r'\*":direct"\)\s*ARM_ENV="\$DIRECT_ENV"', "adm direct env")
require(launcher_texts["adm"],
        r'disc15:discovery\)\s*ARM_ENV="\$DISCOVERY15_ENV"', "adm disc15 env")
require(launcher_texts["adm"],
        r'\*":discovery"\)\s*ARM_ENV="\$DISCOVERY_ENV"', "adm discovery env")

direct = overrides / tim_env["DIRECT_ENV"]
discovery = overrides / tim_env["DISCOVERY_ENV"]
event_only = overrides / tim_env["EVENT_ONLY_DELTA"]
loss_all = overrides / tim_env["LOSS_ALL_DELTA"]
premature10 = overrides / snd_env["PREMATURE10_DELTA"]


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


# None-cell composition table exactly as the launchers resolve it.
none_cells = {
    "event_only": ([direct, event_only], {"READINESS_EVENT_FALLBACK_S"}),
    "hybrid": ([direct], set()),
    "reconcile": ([discovery], set()),
}
for arm, (paths, allowed_changes) in none_cells.items():
    if raw_payload(paths[0]) != payload([paths[0]]):
        raise SystemExit(f"base env is not merge-stable: {paths[0].name}")
    if arm == "hybrid" and payload(paths) != raw_payload(paths[0]):
        raise SystemExit("hybrid none-cell merge is not byte-identical to the base env file")
    order, merged = merge(paths)
    base_order, base = merge([paths[0]])
    changed = {key for key in set(merged) | set(base)
               if merged.get(key) != base.get(key)}
    if not changed <= allowed_changes:
        raise SystemExit(
            f"{arm} none-cell merge changes unexpected keys: "
            f"{sorted(changed - allowed_changes)}")
    for key, inert in (("EDGE_READY_PREMATURE_S", "0"),
                       ("EDGE_READY_SEMANTIC_LIE_S", "0"),
                       ("READINESS_EVENT_DROP_MODE", "off")):
        if key in merged and value(merged, key) != inert:
            raise SystemExit(f"{arm} none-cell merge carries {key} not default-{inert}")
if value(merge([direct, event_only])[1], "READINESS_EVENT_FALLBACK_S") != "130.0":
    raise SystemExit("event_only delta does not override READINESS_EVENT_FALLBACK_S to 130.0")
if float(value(merge([direct])[1], "READINESS_EVENT_FALLBACK_S")) != 20.0:
    raise SystemExit("hybrid base does not keep READINESS_EVENT_FALLBACK_S at 20.0")
if value(merge([discovery])[1], "READINESS_PROPAGATION") != "discovery" \
        or float(value(merge([discovery])[1], "DISCOVERY_POLL_INTERVAL_S")) != 10.0:
    raise SystemExit("reconcile base is not the discovery 10 s regime")

# Fault deltas over the arm base resolve to the pre-registered values.
for name, base_paths, delta, key, expected in (
        ("lost/event_only", (direct, event_only), loss_all, "READINESS_EVENT_DROP_MODE", "all"),
        ("lost/hybrid", (direct,), loss_all, "READINESS_EVENT_DROP_MODE", "all"),
        ("lost/reconcile", (discovery,), loss_all, "READINESS_EVENT_DROP_MODE", "all"),
        ("premature10/event_only", (direct, event_only), premature10,
         "EDGE_READY_PREMATURE_S", "10"),
        ("premature10/hybrid", (direct,), premature10, "EDGE_READY_PREMATURE_S", "10"),
        ("premature10/reconcile", (discovery,), premature10,
         "EDGE_READY_PREMATURE_S", "10")):
    _, merged = merge([*base_paths, delta])
    if key not in merged or value(merged, key) != expected:
        raise SystemExit(f"fault delta mismatch for {name}: {key} != {expected}")

# Supporting sets: single canonical env file, no delta; the file names are
# cross-asserted against the supporting launcher's readonly table.
supporting = {
    adm_env["DIRECT_ENV"]: {"READINESS_PROPAGATION": "direct",
                            "EDGE_APP_READY_EVENT": "1"},
    adm_env["DISCOVERY_ENV"]: {"READINESS_PROPAGATION": "discovery",
                               "EDGE_APP_READY_EVENT": "0"},
    adm_env["DISCOVERY15_ENV"]: {"READINESS_PROPAGATION": "discovery",
                                 "DISCOVERY_POLL_INTERVAL_S": "15.0",
                                 "EDGE_APP_READY_EVENT": "0"},
}
if set(supporting) != {"rq3_direct.env", "rq3_discovery.env",
                       "rq3_discovery_15.env"}:
    raise SystemExit(
        f"supporting launcher env table diverges from the expected set: "
        f"{sorted(supporting)} — fail closed")
for name, expectations in supporting.items():
    path = overrides / name
    if raw_payload(path) != payload([path]):
        raise SystemExit(f"supporting env is not merge-stable: {name}")
    _, merged = merge([path])
    for key, expected in expectations.items():
        resolved = value(merged, key) if key in merged \
            else {"EDGE_APP_READY_EVENT": "0"}.get(key)
        if resolved != expected:
            raise SystemExit(f"supporting env {name}: {key} resolves to {resolved} != {expected}")
print("RQ3 admission-modes env-merge proof: PASS")
PY
    echo "RQ3 admission-modes env-merge proof: PASS (env source: ${ENV_SOURCE_LABEL}; launchers: timing/soundness execution trees + the supporting launcher)"
fi

bash -n "$LAUNCHER"
bash -n "$LABEL_CHECK"

# Launcher negatives: each case must be refused BEFORE any action, since a valid invocation
# would restore a set profile and execute a real run (no metrics folder may be created).
metrics_dir_count() {
    find "$METRICS_DIR" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | wc -l || true
}

file_hash() {
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

launcher_negative() {
    local name="$1"
    shift
    local before after status phases_before phases_after
    before="$(metrics_dir_count)"
    phases_before="$(file_hash "$PHASES")"
    set +e
    bash "$LAUNCHER" "$@" >/dev/null 2>&1
    status=$?
    set -e
    after="$(metrics_dir_count)"
    phases_after="$(file_hash "$PHASES")"
    [[ $status -ne 0 ]] || die "launcher negative (${name}): unexpectedly succeeded"
    [[ "$before" == "$after" ]] \
        || die "launcher negative (${name}): metrics/ changed ${before} -> ${after}"
    [[ "$phases_before" == "$phases_after" ]] \
        || die "launcher negative (${name}): canonical phases.json was modified"
    echo "launcher negative (${name}): rejected exit=${status}; metrics/ and phases.json unchanged"
}

launcher_negative "wrong set"       bogus direct    rq3_camp_direct_99   1
launcher_negative "invalid arm"     camp  bogus     rq3_camp_direct_99   1
launcher_negative "malformed label" camp  direct    not_a_label          1
launcher_negative "label/arm mismatch" camp direct  rq3_camp_disc_99     1
launcher_negative "disc15 non-discovery" disc15 direct rq3_disc15_99       1
# Duplicate-label case uses rq3_disc15_1 (seed 2007's set); skipped when those folders are absent.
if find "$METRICS_DIR" -mindepth 1 -maxdepth 1 -type d \
        -name "*_rq3_disc15_1" -print -quit 2>/dev/null | grep -q .; then
    launcher_negative "duplicate label" disc15 discovery rq3_disc15_1    2007
else
    echo "SKIP launcher negative (duplicate label): recorded rq3_disc15_1 folder not present on this tree"
fi

set +e
bash "$LABEL_CHECK" "zzz_fresh_probe_label_$$" >/dev/null 2>&1
fresh_status=$?
set -e
[[ $fresh_status -eq 0 ]] || die "label check refused a fresh label (exit ${fresh_status})"
echo "label check negative (fresh label): exit 0 (launch may proceed)"
if find "$METRICS_DIR" -mindepth 1 -maxdepth 1 -type d \
        -name "*_rq3_disc15_1" -print -quit 2>/dev/null | grep -q .; then
    set +e
    bash "$LABEL_CHECK" rq3_disc15_1 >/dev/null 2>&1
    dup_status=$?
    set -e
    [[ $dup_status -eq 3 ]] \
        || die "label check did not refuse the recorded duplicate (exit ${dup_status} != 3)"
    echo "label check negative (duplicate label): exit 3 (STOP)"
else
    echo "SKIP label check duplicate negative: recorded rq3_disc15_1 folder not present"
fi

bad_snapshot="$(mktemp "${TMPDIR:-/tmp}/rq3adm_bad_snapshot_XXXXXX.json")"
trap 'rm -f "$bad_snapshot"' EXIT
python3 - "$bad_snapshot" <<'PY'
import json
import sys

phases = {"phases": [
    {"name": "baseline", "duration_s": 60, "rate_per_client": 1.0, "mix": {}},
    {"name": "compute_spike", "duration_s": 180, "rate_per_client": 99.0,
     "mix": {"service_pressure": 0.9, "feed_ranking": 0.05, "content_lookup": 0.05}},
    {"name": "cleanup_gap", "duration_s": 180, "rate_per_client": 0.5, "mix": {}},
]}
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(phases))
PY
before="$(metrics_dir_count)"
phases_before="$(file_hash "$PHASES")"
set +e
RQ3ADM_SNAPSHOT_FILE="$bad_snapshot" bash "$LAUNCHER" camp direct rq3_camp_direct_99 1 \
    >/dev/null 2>&1
status=$?
set -e
after="$(metrics_dir_count)"
phases_after="$(file_hash "$PHASES")"
[[ $status -ne 0 ]] || die "launcher negative (wrong profile hash): unexpectedly succeeded"
[[ "$before" == "$after" ]] || die "launcher negative (wrong profile hash): metrics/ changed"
[[ "$phases_before" == "$phases_after" ]] \
    || die "launcher negative (wrong profile hash): canonical phases.json was modified"
echo "launcher negative (wrong profile hash): rejected exit=${status}; snapshot validated before restore"

# The launcher forwards OSKEN_ENV_OVERRIDE_FILE to BOTH setup_network and run_experiment, and
# keeps SKIP_CLIENTS=0 SKIP_SEED=0 SKIP_SNAPSHOT=0 on the run line (plan §5.2).
grep -q 'OSKEN_ENV_OVERRIDE_FILE="\$ARM_ENV"' "$LAUNCHER" \
    || die "launcher does not pass OSKEN_ENV_OVERRIDE_FILE"
grep -c 'OSKEN_ENV_OVERRIDE_FILE="\$ARM_ENV"' "$LAUNCHER" | grep -q '^2$' \
    || die "OSKEN_ENV_OVERRIDE_FILE must be passed to both setup_network and run_experiment"
grep -q 'SKIP_CLIENTS=0 SKIP_SEED=0 SKIP_SNAPSHOT=0' "$LAUNCHER" \
    || die "launcher does not run the supporting sets with SKIP_CLIENTS=0 SKIP_SEED=0 SKIP_SNAPSHOT=0"
grep -q 'restore-family' "$LAUNCHER" || die "launcher lacks the restore-family mode"
grep -q 'canonical_hashes.json' "$LAUNCHER" \
    || die "restore-family does not compare against the preflight canonical hash record"

# Runtime import check: py_compile cannot see a broken import path or aggregation API.
python3 "$REPO_ROOT/tools/rq3_stratified_analysis.py" --help >/dev/null \
    || die "stratified tool --help failed (module load)"
python3 - "$REPO_ROOT" <<'PY' || die "stratified tool analyzer import / rule axis failed"
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "tools"))
import rq3_stratified_analysis as tool

module = tool._import_analyzer()
if not hasattr(module, "completion_backend_records"):
    raise SystemExit("analyzer import lacks completion_backend_records")

# Embedded, machine-independent composed-identity fixtures (2026-10-11 (h)):
# the rule axis is asserted from synthetic snapshot texts, so the check runs
# identically on any host with no dependence on recorded run folders.
FIXTURES = (
    ("A:none", "READINESS_PROPAGATION=direct\n"
               "READINESS_EVENT_FALLBACK_S=130.0\nMAX_DYNAMIC_COMPUTE=12\n"),
    ("A:loss_all", "READINESS_PROPAGATION=direct\n"
                   "READINESS_EVENT_FALLBACK_S=130.0\n"
                   "MAX_DYNAMIC_COMPUTE=12\nREADINESS_EVENT_DROP_MODE=all\n"),
    ("A:premature10", "READINESS_PROPAGATION=direct\n"
                      "READINESS_EVENT_FALLBACK_S=130.0\n"
                      "MAX_DYNAMIC_COMPUTE=12\nEDGE_READY_PREMATURE_S=10\n"),
    ("B:none", "READINESS_PROPAGATION=direct\n"
               "READINESS_EVENT_FALLBACK_S=20.0\nMAX_DYNAMIC_COMPUTE=12\n"),
    ("B:premature10", "READINESS_PROPAGATION=direct\n"
                      "READINESS_EVENT_FALLBACK_S=20.0\n"
                      "MAX_DYNAMIC_COMPUTE=12\nEDGE_READY_PREMATURE_S=10\n"),
    ("C:none", "READINESS_PROPAGATION=discovery\n"
               "DISCOVERY_POLL_INTERVAL_S=10.0\nMAX_DYNAMIC_COMPUTE=12\n"),
    ("C:loss_all", "READINESS_PROPAGATION=discovery\n"
                   "DISCOVERY_POLL_INTERVAL_S=10.0\n"
                   "MAX_DYNAMIC_COMPUTE=12\nREADINESS_EVENT_DROP_MODE=all\n"),
    ("wake_verify:none", "READINESS_PROPAGATION=direct\n"
                         "READINESS_EVENT_FALLBACK_S=20.0\n"
                         "MAX_DYNAMIC_COMPUTE=12\n"
                         "READINESS_WAKE_VERIFY_MODE=probe\n"),
    ("direct:none", "READINESS_PROPAGATION=direct\n"
                    "READINESS_EVENT_FALLBACK_S=5.0\nMAX_DYNAMIC_COMPUTE=6\n"),
    ("discovery:none", "READINESS_PROPAGATION=discovery\n"
                       "DISCOVERY_POLL_INTERVAL_S=10.0\nMAX_DYNAMIC_COMPUTE=6\n"),
    ("discovery_15:none", "READINESS_PROPAGATION=discovery\n"
                          "DISCOVERY_POLL_INTERVAL_S=15.0\n"
                          "MAX_DYNAMIC_COMPUTE=6\n"),
)
fixture_rules = {}
for expected, text in FIXTURES:
    got = tool._derive_rule(tool._parse_env_text(text))
    if got != expected:
        raise SystemExit(f"rule-axis fixture: {got} != {expected}")
    fixture_rules[expected] = got
# Matrix C must never pool with the v2 discovery set: their composed readiness
# keys match apart from the regime marker (the marker separates them).
if fixture_rules["C:none"] == fixture_rules["discovery:none"]:
    raise SystemExit("matrix C pools with the v2 discovery set")

# Optional extra (never required): when recorded v2-set snapshots happen to be
# present, validate their mapping. Only a snapshot INSIDE the v2-set globs may
# flag leakage — campaign snapshots elsewhere in metrics/ (e.g. the legitimate
# rq3sat_camp_* folders, which derive to B:none/C:none) are never flagged.
metrics = root / "source/scripts/testing/metrics"
V2_SET_GLOBS = (
    ("*_rq3_direct_*", "direct:none"),
    ("*_rq3_disc_*", "discovery:none"),
    ("*_rq3_disc15_*", "discovery_15:none"),
    ("*_rq3_probe_cell12_direct_*", "direct:none"),
    ("*_rq3_probe_cell12_disc_*", "discovery:none"),
)
recorded_v2 = {}
if metrics.is_dir():
    for pattern, expected in V2_SET_GLOBS:
        for folder in sorted(metrics.glob(pattern)):
            snapshot = folder / "controller_env_snapshot.env"
            if not snapshot.is_file():
                continue
            recorded_v2[folder.name] = (
                tool._derive_rule(tool._parse_env(str(snapshot))), expected)
mismatches = sorted(
    f"{name} -> {got} (expected {expected})"
    for name, (got, expected) in recorded_v2.items() if got != expected)
if mismatches:
    raise SystemExit("recorded v2-set snapshot(s) derive to an unexpected "
                     f"rule (v2-set leakage or drift): {mismatches}")
if recorded_v2:
    print(f"recorded v2-set snapshot fixtures (optional extra): "
          f"{len(recorded_v2)} validated")
else:
    print("(recorded v2-set snapshots not present — optional extra skipped; "
          "the embedded rule-axis fixtures above are authoritative)")
print("stratified tool runtime import + rule axis (A/B/C + v2 sets): PASS")
PY
echo "stratified tool runtime import: PASS"

set +e
python3 "$REPO_ROOT/tools/rq3_stratified_analysis.py" \
    "$SCRIPT_DIR/metrics" "$SCRIPT_DIR/metrics" >/dev/null 2>&1
dup_status=$?
set -e
[[ "$dup_status" -eq 3 ]] \
    || die "stratified tool did not refuse a duplicate run folder (exit ${dup_status})"
echo "stratified tool duplicate run folder: refused (exit 3)"

readonly COMPILE_FILES=(
    "$SCRIPT_DIR/rq3adm_p0_01_platform_identity.py"
    "$SCRIPT_DIR/rq3adm_p0_03_analyzer_selftest.py"
    "$SCRIPT_DIR/analysis/rq3/readiness_robustness.py"
    "$REPO_ROOT/tools/rq3_stratified_analysis.py"
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

python3 "$SCRIPT_DIR/rq3adm_p0_01_platform_identity.py" --selftest >/dev/null
echo "platform-identity selftest (rq3adm_p0_01): PASS"

python3 "$SCRIPT_DIR/rq3adm_p0_03_analyzer_selftest.py" >/dev/null
echo "completion analyzer selftest (rq3adm): PASS"
python3 "$SCRIPT_DIR/rq3tim_p0_02_analyzer_selftest.py" >/dev/null
echo "timing analyzer selftest (rq3tim): PASS"
python3 "$SCRIPT_DIR/rq3snd_p0_02_analyzer_selftest.py" >/dev/null
echo "soundness analyzer selftest (rq3snd): PASS"

if [[ ${#SKIPPED_CHECKS[@]} -gt 0 ]]; then
    skipped_joined=""
    for skipped_entry in "${SKIPPED_CHECKS[@]}"; do
        skipped_joined+="${skipped_joined:+; }${skipped_entry}"
    done
    echo "RQ3 admission-modes preflight preparation checks: PASS (skipped: ${skipped_joined})"
else
    echo "RQ3 admission-modes preflight preparation checks: PASS"
fi
