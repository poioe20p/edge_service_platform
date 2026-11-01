#!/usr/bin/env bash
# RQ2 v5 W3 campaign driver.
# Contract: docs/operation/testing/experiment/v5/rq2/experiment_plan.md
#
# setup_network's cleanup.sh -v sweeps ALL containers at every launch, so strays never
# survive into a run; the recorded dyn_pre count is evidence only.
#
# HALT on launcher rc != 0, missing run folder, missing .run_completed, culls != 0.
# OOM lines are recorded, not fatal (v4 carry).
set -u

REPO="$HOME/efficient-storage-in-edge-scenarios"
BAT="$HOME/rq2v5_battery"
EVID="$BAT/evidence"
LOG="$BAT/driver.log"
HALT="$BAT/DRIVER_HALTED"
DONE="$BAT/CAMPAIGN_DONE"
STATUS="$BAT/rq2v5_run_status.csv"
METRICS="$REPO/source/scripts/testing/metrics"
PINS="$REPO/source/scripts/testing/controller_env_overrides/rq2v5_pins.env"
PHASES="$REPO/source/scripts/testing/phases.json"
REFDIR="$REPO/docs/operation/testing/experiment/v5/rq2"
REF_MX="$REFDIR/rq2v5_ref_phases_mx.json"
REF_DB="$REFDIR/rq2v5_ref_phases_db.json"
REF_RESTORE="$REFDIR/rq2v5_ref_phases_restore.json"

BATCH="${1:?usage: rq2v5_campaign_driver.sh <batch-file> [--dry-run]}"
DRY="${2:-}"

mkdir -p "$EVID"
sha() { sha256sum "$1" 2>/dev/null | cut -c1-20; }

rm -f "$HALT" "$DONE"
[ -f "$STATUS" ] || echo "ts_utc,label,mode,regime,seed,ec,sc,rc,run_completed,culls,oom_lines,phase_last,verdict,reason" > "$STATUS"

total=$(grep -vcE '^\s*(#|$)' "$BATCH")
n=0
echo "[driver] start batch=$BATCH runs=$total dry=${DRY:-no} $(date -u +%FT%TZ)" >> "$LOG"

while read -r mode label seed ec sc; do
  case "${mode:-}" in ''|'#'*) continue ;; esac
  n=$((n + 1))
  regime=""
  case "$label" in *_mx_*) regime=mx ;; *_db_*) regime=db ;; esac
  if [ -z "$regime" ]; then
    echo "[driver] HALT bad_label $label" >> "$LOG"
    echo "bad_label $label" > "$HALT"; exit 2
  fi

  ref="$REF_MX"; [ "$regime" = db ] && ref="$REF_DB"
  want=$(sha "$ref"); have=$(sha "$PHASES")
  if [ "$want" != "$have" ]; then
    echo "[driver] phases_swap regime=$regime $have -> $want ($(basename "$ref"))" >> "$LOG"
    cp "$ref" "$PHASES"
    have=$(sha "$PHASES")
    if [ "$want" != "$have" ]; then
      echo "[driver] HALT phases_swap_failed $label have=$have want=$want" >> "$LOG"
      echo "phases_swap_failed $label" > "$HALT"; exit 2
    fi
  fi

  dyn_pre=$(sudo -n docker ps --format '{{.Names}}' 2>/dev/null | grep -cE '^edge_storage_lan[0-9]+_dyn' || true)
  echo "[driver] launch $n/$total $label mode=$mode seed=$seed ec=$ec sc=$sc dyn_pre=$dyn_pre phase_sha=$have $(date -u +%FT%TZ)" >> "$LOG"
  if [ "$DRY" = "--dry-run" ]; then
    continue
  fi

  t0=$(date -u +%s)
  EDGE_CPUS="$ec" STORAGE_CPUS="$sc" HASH_WORK_FACTOR=25000 \
    bash "$REPO/source/scripts/testing/rq2v5_launch_run.sh" "$mode" "$label" "$seed" "$PINS" \
    > "$BAT/run_${label}.out" 2>&1 < /dev/null
  rc=$?

  folder=$(ls -d "$METRICS"/*_"$label" 2>/dev/null | tail -1)
  run_completed=NO
  [ -n "$folder" ] && [ -f "$folder/.run_completed" ] && run_completed=yes
  culls=0
  if [ -n "$folder" ]; then
    culls=$(cat "$folder"/controller_lan1.log "$folder"/controller_lan2.log 2>/dev/null | grep -c 'triggering removal' || true)
  fi
  oom=$(sudo -n journalctl --since "@$t0" --no-pager 2>/dev/null | grep -icE 'out of memory|oom-kill|killed process' || true)
  phase_last=""
  [ -n "$folder" ] && [ -f "$folder/current_phase.txt" ] && phase_last=$(tr -d '\n' < "$folder/current_phase.txt")

  reason=""
  [ "$rc" -ne 0 ] && reason="launcher_exit_$rc"
  [ -z "$folder" ] && reason="${reason:+$reason;}missing_folder"
  [ -n "$folder" ] && [ "$run_completed" != yes ] && reason="${reason:+$reason;}missing_run_completed"
  [ "${culls:-0}" -gt 0 ] && reason="${reason:+$reason;}culls_nonzero"
  verdict=pending_review; [ -n "$reason" ] && verdict=halt

  echo "$(date -u +%FT%TZ),$label,$mode,$regime,$seed,$ec,$sc,$rc,$run_completed,$culls,$oom,$phase_last,$verdict,${reason:-none}" >> "$STATUS"
  {
    echo "== $label $(date -u +%FT%TZ)"
    echo "rc=$rc folder=$folder run_completed=$run_completed culls=$culls oom_lines=$oom phase_last=$phase_last"
    echo "-- dyn after run --"
    sudo -n docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null | grep -E 'dyn' || true
  } > "$EVID/$label.txt"

  if [ -n "$reason" ]; then
    echo "[driver] HALT $label reason=$reason $(date -u +%FT%TZ)" >> "$LOG"
    echo "$label $reason" > "$HALT"
    exit 1
  fi
  echo "[driver] done $n/$total $label rc=0 oom=$oom $(date -u +%FT%TZ)" >> "$LOG"
  sleep 15
done < "$BATCH"

if [ "$DRY" = "--dry-run" ]; then
  echo "[driver] dry-run complete: $n runs parsed, phases/batch consistent"
  exit 0
fi

if [ -f "$REF_RESTORE" ]; then
  cp "$PHASES" "$EVID/final_phases_snapshot.json"
  cp "$REF_RESTORE" "$PHASES"
  echo "[driver] phases_restore sha=$(sha "$PHASES")" >> "$LOG"
fi
echo "[driver] CAMPAIGN COMPLETE runs=$n $(date -u +%FT%TZ)" >> "$LOG"
echo "$n runs" > "$DONE"
