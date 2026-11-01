#!/bin/bash
# ============================================================================
# rq3rel_p0_preflight.sh — research_q3 release-mechanism pre-flight gate.
#
# Runs on the cloud VM (cd's to the repo root, so any CWD works).
#
# Implements the pre-launch gate + P1 launch of the merged-design staged
# preflight (docs/operation/testing/experiment/research_q3_DO_NOT_CITE/preflight_campaign.md):
# Stage 0 (local static checks) and Stage 1 (VM sync/harness) are
# operator-driven per that doc; this script is the Stage 2.0 pre-launch
# gate (canonical phases hold=480, canonical env pins off) and launches the
# Stage 2 P1 run. Run it DETACHED (`nohup ... &`) and monitor in parallel
# with tools/watch_run.py + the checkpoint sampler per that doc.
#
# Stage 1 — harness quick check:
#   1. canonical phases file present (source/scripts/testing/phases.json)
#   2. phases.json "hold" duration_s = 480 (the runner must have applied the
#      600→480 edit to the canonical file before P1)
#   3. canonical controller override pins RELEASE_MECHANISM=off
# Stage 2 — one full-length STABILIZED early preflight run (P1):
#   research_q3_launch_run.sh arm_stabilized.env research_q3_s_pre_e1 42, then
#   an rs_status probe on the newest run dir, then a manual checklist plus
#   script-checkable PASS/FAIL hints (release-log event rows, both tiers).
#
# Exit 0 = all checked gates pass; 1 = a gate failed.
# ============================================================================

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

echo "== rq3rel_p0_preflight: repo root = $(pwd) =="

# ---------------------------------------------------------------------------
# Stage 2.0 — pre-launch gate (canonical phases + canonical env)
# ---------------------------------------------------------------------------
echo
echo "== Stage 2.0 — pre-launch gate =="
if [[ ! -f source/scripts/testing/phases.json ]]; then
    echo "[FAIL] source/scripts/testing/phases.json missing — canonical phases file required" >&2
    exit 1
fi
echo "[PASS] canonical phases.json present"

if ! python3 -c "import json; d=json.load(open('source/scripts/testing/phases.json')); h=next((p for p in d['phases'] if p.get('name') == 'hold'), None); assert h is not None and h['duration_s'] == 480, 'hold phase duration_s is not 480'"; then
    echo "[FAIL] phases.json 'hold' duration_s is not 480 — apply the 600→480" \
         "edit to source/scripts/testing/phases.json before launching P1" >&2
    exit 1
fi
echo "[PASS] phases.json hold duration_s = 480 (600→480 edit applied)"

if ! grep -qE '^RELEASE_MECHANISM=off($|[[:space:]])' \
     source/scripts/testing/controller_env_overrides/current_state_integrated.env; then
    echo "[FAIL] current_state_integrated.env does not pin RELEASE_MECHANISM=off" \
         "(the incumbent arm must have the release mechanism explicitly off)" >&2
    exit 1
fi
echo "[PASS] canonical env pins RELEASE_MECHANISM=off"

# ---------------------------------------------------------------------------
# Stage 2 — P1 STABILIZED early preflight run (mechanism-firing gate)
# ---------------------------------------------------------------------------
echo
echo "== Stage 2 — P1 STABILIZED early preflight run (mechanism-firing gate, hold=480) =="
echo "  launch: bash source/scripts/testing/research_q3_launch_run.sh arm_stabilized.env research_q3_s_pre_e1 42"
bash source/scripts/testing/research_q3_launch_run.sh arm_stabilized.env research_q3_s_pre_e1 42
echo "[DONE] P1 preflight run completed"

echo
echo "== Stage 2b — post-run replica-set probe (P1 run dir) =="
RUN_DIR="$(ls -dt source/scripts/testing/metrics/*research_q3_s_pre_e1*/ 2>/dev/null | head -n1 || true)"
if [[ -z "$RUN_DIR" ]]; then
    echo "[FAIL] no run dir matching *research_q3_s_pre_e1* under source/scripts/testing/metrics/" >&2
    exit 1
fi
RUN_DIR="${RUN_DIR%/}"
echo "  newest run dir: $RUN_DIR"
# run_experiment already auto-probes the run folder (rs_status_*.json), and
# the run folder is root-owned while this script runs as the operator user —
# only re-probe when no rs_status file exists yet, and never abort on failure.
if ls "$RUN_DIR"/rs_status_*.json >/dev/null 2>&1; then
    echo "  [skip] rs_status_*.json already present (run_experiment auto-probe) — not re-probing"
else
    python3 source/scripts/testing/rq3rel_p4_rs_probe.py --run-dir "$RUN_DIR" --label s_pre_e1 \
        || echo "  [warn] rs_probe failed (non-fatal) — check run folder manually"
fi

echo
echo "== Manual verification checklist (operator verifies) =="
echo "  [ ] harness active      : controller logs show flow-isolation markers"
echo "  [ ] storm spawn         : both tiers (compute + storage) spawn in storm"
echo "  [ ] release events      : compute quarantine/recall rows AND storage retention"
echo "                            quarantine_begin/recall rows (tier=storage) in release_log_lan{1,2}.csv"
echo "  [ ] recall substitution : decision-log rows action=recall, reason=recalled_substitution"
echo "                            (no DataAlert at that window)"
echo "  [ ] evictions           : 0 non-ok evictions in rs_evict_logs_lan{1,2}/"
echo "  [ ] D1 attribution      : D1 clean outside attribution windows"

echo
echo "== Script-checkable hints (release log event rows, both tiers) =="
_qb_total=0
_rc_total=0
_sqb_total=0
_src_total=0
_en_total=0
for _f in "$RUN_DIR"/release_log_lan1.csv "$RUN_DIR"/release_log_lan2.csv; do
    if [[ ! -f "$_f" ]]; then
        echo "  [hint] $(basename "$_f") missing — quarantine never armed?"
        continue
    fi
    # release_log is CSV; the tier column precedes the event column, so a
    # tier-scoped row is matched with the tier first. Recall counts use
    # `,recall,1,` so audit/abort rows (success=0) never satisfy the gate.
    _qb="$(grep -cE ',compute,.*,quarantine_begin,' "$_f" 2>/dev/null || true)"
    _rc="$(grep -cE ',compute,.*,recall,1,' "$_f" 2>/dev/null || true)"
    _sqb="$(grep -cE ',storage,.*,quarantine_begin,' "$_f" 2>/dev/null || true)"
    _src="$(grep -cE ',storage,.*,recall,1,' "$_f" 2>/dev/null || true)"
    _en="$(grep -c ',end,' "$_f" 2>/dev/null || true)"
    echo "  [hint] $(basename "$_f"): compute qb=$_qb rc=$_rc storage qb=$_sqb rc=$_src end=$_en"
    _qb_total=$((_qb_total + ${_qb:-0}))
    _rc_total=$((_rc_total + ${_rc:-0}))
    _sqb_total=$((_sqb_total + ${_sqb:-0}))
    _src_total=$((_src_total + ${_src:-0}))
    _en_total=$((_en_total + ${_en:-0}))
done
if [[ "$_qb_total" -gt 0 ]] && [[ "$_rc_total" -gt 0 ]] \
   && [[ "$_sqb_total" -gt 0 ]] && [[ "$_src_total" -gt 0 ]]; then
    echo "  [PASS] compute quarantine_begin=$_qb_total recall(success=1)=$_rc_total;" \
         "storage quarantine_begin=$_sqb_total recall(success=1)=$_src_total"
else
    echo "  [FAIL] run must show quarantine_begin AND success=1 recall rows on BOTH tiers" \
         "(compute qb=$_qb_total rc=$_rc_total; storage qb=$_sqb_total rc=$_src_total — a tier never armed or never genuinely recalled)" >&2
    exit 1
fi
if [[ "$_en_total" -gt 0 ]]; then
    echo "  [PASS-hint] end rows: $_en_total"
else
    echo "  [FAIL-hint] no end rows — release never completed (informational)"
fi

echo
echo "[DONE] rq3rel_p0_preflight complete"
