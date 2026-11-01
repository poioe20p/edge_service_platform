#!/usr/bin/env python3
"""RQ3 robustness and QoE-consequence campaign analysis.

Read-only artifact analysis. Subcommands:

  classify            per-backend outcome rows for one or more runs
  stage1              Stage 1 loss-cell hypotheses (H1/H2/H3)
  restart-observation post-restart pending-backend outcomes per arm
  qoe-screen          Family 3 (rq3_qoe) C1/C2/C3 discovery gates
  qoe-campaign        Family 3 (rq3_qoe) E-stage H-Q1..H-Q4 verdicts
  timing-screen       Family rq3_timing P1/P2/lock preflight gates
  timing-campaign     Family rq3_timing E-stage H-T1..H-T3 verdicts
  soundness-damage    Family 5 (rq3_soundness) damage tables (U / D_s / D-bar)
  soundness-preflight Family 5 (rq3_soundness) P1 gates R1-R3b and lock JSON
  soundness-crossover Family 5 (rq3_soundness) lead-N fits, N*, H-S1..H-S4
  completion-intact   Family 6 (rq3_admission_modes) E-I1..E-I4 (7-block intact)
  completion-lost     Family 6 (rq3_admission_modes) E-L1..E-L4 (7-block lost)
  completion-premature Family 6 (rq3_admission_modes) E-P1..E-P3 (7 seed-matched
                      premature 10 s-lead pairs; E-P4 retired 2026-10-11 (b))
  completion-support  Family 6 (rq3_admission_modes) E-S1..E-S3 (added
                      supporting-set replicates, per set)

Family 3 measures the visible/bounded QoE consequence of readiness-event
loss under a uniform EDGE_CPUS quota ladder; see
docs/operation/testing/experiment/v3/rq3_qoe_DO_NOT_CITE/experiment_plan.md.
Family 5 measures the soundness half of the reframed RQ3 (usable-capacity
realization under readiness-channel faults F1/F2/F3); see
docs/operation/testing/experiment/v3/rq3_soundness/experiment_plan.md.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from .readiness_low_headroom import (
    PHASE,
    as_float,
    capacity_summary,
    cpu_values,
    is_completed,
    is_success,
    load_phases,
    load_window_log,
    parse_env,
    parse_ready_epoch,
    percentile,
    phase_bounds,
    read_csv,
    request_rows,
    run_seed,
    timestamp,
    write_csv,
    write_json,
)

LABEL_RE = re.compile(r"rq3rob_(none|loss_all|loss_alt|restart)_(event_only|hybrid|reconcile)_(\d+)$")
QOE_LABEL_RE = re.compile(r"rq3qoe_(none|loss_all|loss_alt)_(event_only|hybrid|reconcile)_(\d+)$")
DYNAMIC_RE = re.compile(r"^edge_server_lan[12]_dyn\d+$")
RUNNING_STATES = {"running", "created", "restarting"}

# Family 3 (rq3_qoe) pre-registered contract constants. Floors follow the
# frozen P4 workload: plateau ~21.6k rows/LAN, baseline ~120 rows/LAN (2
# active clients/LAN at client_fraction 0.1). The baseline floors below are the
# amended artifact-sanity minimums (pooled >=100, per-LAN >=50) so the
# baseline bad_rate <=1% read is meaningful; the earlier draft's 1000/LAN
# baseline figure is unreachable and was amended 2026-09-07 (see
# docs/operation/testing/experiment/v3/rq3_qoe_DO_NOT_CITE/experiment_plan.md §3).
QOE_LADDER = ("0.13", "0.12", "0.11", "0.10", "0.09")
PLATEAU_MIN_POOLED = 5000
PLATEAU_MIN_PER_LAN = 1000
BASELINE_MIN_POOLED = 100
BASELINE_MIN_PER_LAN = 50
CONTRAST_PP = 0.05
CONTROL_BAD_MAX = 0.01
BOUNDED_BAD_MAX = 0.15

# Slow-share axis (amendment 2026-09-08, user-approved; see
# docs/operation/testing/experiment/v3/rq3_qoe_DO_NOT_CITE/experiment_plan.md §3/§4/§5).
# QoE worsening = "higher latency and/or timeouts": slow_rate counts plateau
# service rows with status==timeout (unconditional; driver cap 300 s > T) or
# latency_s > T. Controls slow ceiling 2 pp; event_only slow boundedness
# ceiling 25 %; slow contrast bar reuses CONTRAST_PP (5 pp).
QOE_SLOW_S = 1.0
SLOW_CONTROL_MAX = 0.02
# The lost-message assessability gate tolerates 0.5 pp above the 2 pp control
# ceiling. Block 7 (seed 5207) has a no-fault control at 2.02 %, and 0.02 pp
# sits inside the noise of the tail estimate. The ceiling above governs every
# other family.
SLOW_CONTROL_MAX_LOST = 0.025
SLOW_BOUNDED_EVENT_MAX = 0.25


def container_events(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = run_dir / "container_events.csv"
    if not path.exists():
        return rows
    for row in csv.DictReader(path.open(newline="", encoding="utf-8", errors="replace")):
        parsed = timestamp(row.get("timestamp_iso"))
        if parsed is None:
            continue
        item = dict(row)
        item["_ts"] = parsed
        rows.append(item)
    return rows


def restart_markers(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = run_dir / "experiment_fault_events.csv"
    if not path.exists():
        return rows
    for row in csv.DictReader(path.open(newline="", encoding="utf-8", errors="replace")):
        if row.get("action_type") == "docker_restart" and row.get("status") == "executed":
            parsed = timestamp(row.get("timestamp"))
            if parsed is not None:
                item = dict(row)
                item["_ts"] = parsed
                rows.append(item)
    return rows


def admission_rows(run_dir: Path) -> list[dict[str, Any]]:
    """All admission-log rows (admitted and abandoned) with true readiness."""
    rows: list[dict[str, Any]] = []
    for lan in (1, 2):
        path = run_dir / f"admission_log_lan{lan}.csv"
        if not path.exists():
            continue
        for row in csv.DictReader(path.open(newline="", encoding="utf-8", errors="replace")):
            item = dict(row)
            item["lan"] = lan
            item["_admitted_ts"] = timestamp(row.get("admitted_ts"))
            item["_abandoned_ts"] = timestamp(row.get("app_ready_ts")) if row.get("result") == "abandoned" else None
            item["_true_ready"] = parse_ready_epoch(
                run_dir / "service_logs" / f"{row.get('container', '')}.log"
            )
            rows.append(item)
    return rows


def classify(run_dir: Path) -> dict[str, Any]:
    """Per-run classification of every dynamic compute backend."""
    events = container_events(run_dir)
    adm = admission_rows(run_dir)
    admitted_by_container = {
        row.get("container"): row for row in adm if row.get("result") == "admitted"
    }
    abandoned_by_container = {
        row.get("container"): row for row in adm if row.get("result") == "abandoned"
    }
    spawn: dict[str, float] = {}
    final_state: dict[str, str] = {}
    last_event_ts: dict[str, float] = {}
    for event in events:
        container = event.get("container", "")
        if not DYNAMIC_RE.match(container):
            continue
        if container not in spawn:
            spawn[container] = event["_ts"]
        final_state[container] = event.get("state", final_state.get(container, ""))
        last_event_ts[container] = event["_ts"]
    removed: dict[str, float] = {
        container: last_event_ts[container]
        for container in spawn
        if final_state.get(container, "") not in RUNNING_STATES
    }
    first_abandon = min(
        (row["_abandoned_ts"] for row in abandoned_by_container.values()
         if row["_abandoned_ts"] is not None),
        default=None,
    )
    backends: list[dict[str, Any]] = []
    for container, spawn_ts in sorted(spawn.items(), key=lambda pair: pair[1]):
        admitted = admitted_by_container.get(container)
        abandoned = abandoned_by_container.get(container)
        outcome = ""
        admit_source = ""
        admitted_ts = None
        if admitted is not None:
            outcome = "admitted"
            admit_source = admitted.get("admit_source", "")
            admitted_ts = admitted["_admitted_ts"]
        elif abandoned is not None:
            outcome = "abandoned"
            admit_source = "abandoned"
            admitted_ts = None
        elif container in removed:
            outcome = "removed_without_admission"
        else:
            outcome = "orphaned_dark"
        pre_churn = first_abandon is None or spawn_ts < first_abandon
        backends.append({
            "container": container,
            "lan": int(re.search(r"lan(\d)", container).group(1)),
            "spawn_ts": spawn_ts,
            "outcome": outcome,
            "admit_source": admit_source,
            "true_ready_ts": (admitted or abandoned or {}).get("_true_ready"),
            "admitted_ts": admitted_ts,
            "abandoned_ts": (abandoned or {}).get("_abandoned_ts"),
            "removed_ts": removed.get(container),
            "pre_churn": pre_churn,
            "dark_s": (admitted_ts - (admitted or {}).get("_true_ready")
                       if admitted_ts is not None and (admitted or {}).get("_true_ready") is not None
                       else None),
            "spawn_admit_s": (admitted_ts - spawn_ts
                              if admitted_ts is not None else None),
        })
    per_lan = {lan: {
        "pre_churn_spawned": sum(b["pre_churn"] for b in backends if b["lan"] == lan),
        "admitted": sum(b["pre_churn"] and b["outcome"] == "admitted"
                        for b in backends if b["lan"] == lan),
    } for lan in (1, 2)}
    pre_churn_spawned = sum(per_lan[lan]["pre_churn_spawned"] for lan in (1, 2))
    admitted_pre_churn = sum(per_lan[lan]["admitted"] for lan in (1, 2))
    return {
        "run": run_dir.name,
        "backends": backends,
        "first_abandon_ts": first_abandon,
        "spawned": len(backends),
        "pre_churn_spawned": pre_churn_spawned,
        "admitted": sum(item["outcome"] == "admitted" for item in backends),
        "abandoned": sum(item["outcome"] == "abandoned" for item in backends),
        "orphaned_dark": sum(item["outcome"] == "orphaned_dark" for item in backends),
        "removed_without_admission": sum(
            item["outcome"] == "removed_without_admission" for item in backends),
        "preservation": (admitted_pre_churn / pre_churn_spawned
                         if pre_churn_spawned else None),
        "per_lan_preservation": {
            lan: (per_lan[lan]["admitted"] / per_lan[lan]["pre_churn_spawned"]
                  if per_lan[lan]["pre_churn_spawned"] else None)
            for lan in (1, 2)
        },
    }


def label_parts(run_name: str) -> tuple[str, str, str] | None:
    match = LABEL_RE.search(run_name)
    return (match.group(1), match.group(2), match.group(3)) if match else None


def exact_mwu(x: list[float], y: list[float]) -> float:
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        return float("nan")
    allv = sorted(x + y)
    ranks = {value: index + 1 for index, value in enumerate(allv)}
    u = sum(ranks[value] for value in x) - n1 * (n1 + 1) / 2
    mean = n1 * n2 / 2
    total = extreme = 0
    for selection in itertools.combinations(range(n1 + n2), n1):
        u_perm = sum(sorted((ranks[allv[i]] for i in selection))) - n1 * (n1 + 1) / 2
        total += 1
        if abs(u_perm - mean) >= abs(u - mean):
            extreme += 1
    return extreme / total


def cliffs(x: list[float], y: list[float]) -> float:
    gt = sum(1 for i in x for j in y if i > j)
    lt = sum(1 for i in x for j in y if i < j)
    return (gt - lt) / (len(x) * len(y))


def classify_command(args: argparse.Namespace) -> int:
    rows: list[dict[str, Any]] = []
    for path in args.run_dir:
        result = classify(Path(path))
        for backend in result["backends"]:
            rows.append({"run": result["run"], **backend})
        print(json.dumps({key: result[key] for key in
                          ("run", "spawned", "admitted", "abandoned",
                           "orphaned_dark", "removed_without_admission",
                           "preservation")}, indent=2))
    write_csv(Path(args.out), rows)
    return 0


def stage1_command(args: argparse.Namespace) -> int:
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for path in args.run_dir:
        parts = label_parts(Path(path).name)
        if parts is None:
            raise ValueError(f"cannot parse run label: {Path(path).name}")
        cell, arm, _ = parts
        cells.setdefault((cell, arm), []).append(classify(Path(path)))
    report: dict[str, Any] = {"runs": len(args.run_dir)}
    failures: list[str] = []
    for (cell, arm), runs in sorted(cells.items()):
        preservation = [run["preservation"] for run in runs if run["preservation"] is not None]
        report[f"{cell}_{arm}"] = {
            "preservation": preservation,
            "median_preservation": statistics.median(preservation) if preservation else None,
            "abandoned": [run["abandoned"] for run in runs],
        }
    loss_all_event = report.get("loss_all_event_only", {}).get("preservation", [])
    loss_all_hybrid = report.get("loss_all_hybrid", {}).get("preservation", [])
    loss_all_reconcile = report.get("loss_all_reconcile", {}).get("preservation", [])
    if loss_all_event and any(value != 0.0 for value in loss_all_event):
        failures.append("H1 event_only loss_all preservation != 0.0")
    if loss_all_hybrid and any(value != 1.0 for value in loss_all_hybrid):
        failures.append("H1 hybrid loss_all preservation != 1.0")
    if loss_all_reconcile and any(value != 1.0 for value in loss_all_reconcile):
        failures.append("H1 reconcile loss_all preservation != 1.0")
    alt = report.get("loss_alt_event_only", {}).get("preservation", [])
    if alt and not all(0.3 <= value <= 0.7 for value in alt):
        failures.append("H2 event_only loss_alt preservation outside [0.3,0.7]")
    alt_hybrid_units = [
        run["per_lan_preservation"][lan]
        for run in cells.get(("loss_alt", "hybrid"), [])
        for lan in (1, 2) if run["per_lan_preservation"][lan] is not None
    ]
    alt_reconcile_units = [
        run["per_lan_preservation"][lan]
        for run in cells.get(("loss_alt", "reconcile"), [])
        for lan in (1, 2) if run["per_lan_preservation"][lan] is not None
    ]
    if alt_hybrid_units and any(value != 1.0 for value in alt_hybrid_units):
        failures.append("H2 hybrid loss_alt per-LAN preservation != 1.0")
    if alt_reconcile_units and any(value != 1.0 for value in alt_reconcile_units):
        failures.append("H2 reconcile loss_alt per-LAN preservation != 1.0")
    hybrid_loss = [run for run in cells.get(("loss_all", "hybrid"), [])]
    hybrid_none = [run for run in cells.get(("none", "hybrid"), [])]
    if hybrid_loss and hybrid_none:
        def per_run_medians(runs: list[dict[str, Any]], field: str) -> list[float]:
            medians: list[float] = []
            for run in runs:
                values = [item[field] for item in run["backends"]
                          if item["outcome"] == "admitted" and item.get(field) is not None]
                if values:
                    medians.append(statistics.median(values))
            return medians

        # H3 PRIMARY (pre-registered): spawn->admit, the fallback-cadence
        # anchor (fallback 20 s + probe retry + timeout). Window [20, 26] s.
        spawn_loss = per_run_medians(hybrid_loss, "spawn_admit_s")
        spawn_none = per_run_medians(hybrid_none, "spawn_admit_s")
        # H3 SECONDARY (descriptive only, never gated): true-ready->admit.
        ready_loss = per_run_medians(hybrid_loss, "dark_s")
        ready_none = per_run_medians(hybrid_none, "dark_s")
        if spawn_loss and spawn_none:
            report["H3"] = {
                "metric": "spawn_admit_s",
                "per_run_median_spawn_admit_loss_all": spawn_loss,
                "per_run_median_spawn_admit_none": spawn_none,
                "mwu_p": exact_mwu(spawn_loss, spawn_none),
                "cliffs_delta": cliffs(spawn_loss, spawn_none),
                "within_20_26": 20.0 <= statistics.median(spawn_loss) <= 26.0,
                "true_ready_anchor_descriptive": {
                    "per_run_median_dark_loss_all": ready_loss,
                    "per_run_median_dark_none": ready_none,
                },
            }
            if not report["H3"]["within_20_26"]:
                failures.append(
                    "H3 hybrid loss_all median spawn->admit outside [20,26] s")
            if statistics.median(spawn_loss) <= statistics.median(spawn_none):
                failures.append(
                    "H3 hybrid loss_all spawn->admit not above none cell")
    report["failures"] = failures
    report["pass"] = not failures
    write_json(Path(args.out), report)
    print(json.dumps({"pass": report["pass"], "failures": failures}, indent=2))
    return 0 if report["pass"] else 2


def restart_command(args: argparse.Namespace) -> int:
    arms: dict[str, list[dict[str, Any]]] = {}
    for path in args.run_dir:
        run_dir = Path(path)
        markers = restart_markers(run_dir)
        if not markers:
            raise ValueError(f"no executed docker_restart marker in {run_dir.name}")
        restart_ts = min(marker["_ts"] for marker in markers)
        result = classify(run_dir)
        pending = []
        for backend in result["backends"]:
            admitted = backend["admitted_ts"]
            failed_before = (
                (backend["abandoned_ts"] is not None
                 and backend["abandoned_ts"] < restart_ts)
                or (backend["removed_ts"] is not None
                    and backend["removed_ts"] < restart_ts)
            )
            pending_at_restart = (backend["spawn_ts"] < restart_ts
                                  and not failed_before
                                  and (admitted is None or admitted > restart_ts))
            if pending_at_restart:
                pending.append({"container": backend["container"],
                                "spawn_ts": backend["spawn_ts"],
                                "outcome": backend["outcome"],
                                "admitted_ts": admitted,
                                "admitted_after_restart": admitted is not None and admitted > restart_ts})
        arm = label_parts(run_dir.name)[1] if label_parts(run_dir.name) else "?"
        arms.setdefault(arm, []).append({"run": run_dir.name,
                                         "restart_ts": restart_ts,
                                         "pending_at_restart": pending,
                                         "spawned": result["spawned"],
                                         "admitted": result["admitted"],
                                         "orphaned_dark": result["orphaned_dark"]})
    write_json(Path(args.out), {"arms": arms})
    print(json.dumps({"arms": {arm: [run["pending_at_restart"] for run in runs]
                               for arm, runs in arms.items()}}, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Family 3 (rq3_qoe): QoE-consequence measurement + discovery/evidence gates
# ---------------------------------------------------------------------------


def qoe_label_parts(run_name: str) -> tuple[str, str, str] | None:
    match = QOE_LABEL_RE.search(run_name)
    return (match.group(1), match.group(2), match.group(3)) if match else None


def _qoe_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """status_rates-equivalent with the aggregate bad_rate pre-registered in
    the QoE measurement contract:
      timeout_rate = timeouts / (completed + timeout)
      failure_rate = failures / completed
      bad_rate = (timeout + failure) / (completed + timeout)   # aggregate
    canceled/dropped are counted in offered but excluded from the rates.
    """
    offered = len(rows)
    canceled = sum(row.get("status") in {"canceled", "dropped"} for row in rows)
    service = [row for row in rows if row.get("status") in {"completed", "timeout"}]
    completed = sum(is_completed(row) for row in service)
    timeouts = sum(row.get("status") == "timeout" for row in service)
    failures = sum(is_completed(row) and not is_success(row) for row in service)
    service_n = len(service)
    # Slow-share axis: a timeout is slow unconditionally (its latency_s is
    # elapsed-to-cap, and a missing/unparseable latency must never drop it);
    # completed rows are slow when latency_s exceeds the threshold.
    slow = sum(1 for row in service
               if row.get("status") == "timeout"
               or (row.get("_latency") is not None
                   and row["_latency"] > QOE_SLOW_S))
    slow5 = sum(1 for row in service
                if row.get("status") == "timeout"
                or (row.get("_latency") is not None and row["_latency"] > 5.0))
    completed_rows = [row for row in service if is_completed(row)]
    lat = [row["_latency"] for row in completed_rows
           if row.get("_latency") is not None
           and math.isfinite(row["_latency"])]
    return {
        "offered": offered,
        "canceled_dropped": canceled,
        "cancel_rate": canceled / offered if offered else None,
        "http000": sum(row.get("http_status") == "000" for row in rows),
        "completed": completed,
        "timeouts": timeouts,
        "failures": failures,
        "timeout_rate": timeouts / service_n if service_n else None,
        "failure_rate": failures / completed if completed else None,
        "bad_rate": (timeouts + failures) / service_n if service_n else None,
        "service_requests": service_n,
        "slow_n": slow,
        "slow_rate": slow / service_n if service_n else None,
        "slow5_n": slow5,
        "slow5_rate": slow5 / service_n if service_n else None,
        "lat_completed_n": len(lat),
        "lat_completed_p50": percentile(lat, 0.50) if lat else None,
        "lat_completed_p95": percentile(lat, 0.95) if lat else None,
        "lat_completed_p99": percentile(lat, 0.99) if lat else None,
    }


def _qoe_window(run_dir: Path, rows: list[dict[str, Any]],
                bounds: dict[str, tuple[float, float]], name: str) -> dict[str, Any]:
    start, end = bounds[name]
    selected = [row for row in rows
                if start <= row["_sent"] < end and row.get("phase") == name]
    pooled = _qoe_status(selected)
    per_lan = {lan: _qoe_status([row for row in selected
                                 if row.get("client_lan") == f"lan{lan}"])
               for lan in (1, 2)}
    return {"pooled": pooled, "per_lan": per_lan}


def qoe_rate_metrics(run_dir: Path) -> dict[str, Any]:
    """Plateau/baseline QoE rates (pooled + per LAN) with contract floors."""
    rows = request_rows(run_dir)
    bounds = phase_bounds(run_dir)
    plateau = _qoe_window(run_dir, rows, bounds, PHASE)
    baseline = _qoe_window(run_dir, rows, bounds, "baseline")
    floors = {
        "plateau_pooled_ge_5000":
            (plateau["pooled"]["service_requests"] or 0) >= PLATEAU_MIN_POOLED,
        "plateau_per_lan_ge_1000": all(
            (plateau["per_lan"][lan]["service_requests"] or 0) >= PLATEAU_MIN_PER_LAN
            for lan in (1, 2)),
        "baseline_pooled_ge_100":
            (baseline["pooled"]["service_requests"] or 0) >= BASELINE_MIN_POOLED,
        "baseline_per_lan_ge_50": all(
            (baseline["per_lan"][lan]["service_requests"] or 0) >= BASELINE_MIN_PER_LAN
            for lan in (1, 2)),
    }
    return {
        "run": run_dir.name,
        "parts": qoe_label_parts(run_dir.name),
        "plateau": plateau,
        "baseline": baseline,
        "floors": floors,
        "driver_clean": (plateau["pooled"]["cancel_rate"] or 0) < 0.05,
        "baseline_http000_zero": (baseline["pooled"]["http000"] or 0) == 0,
    }


def qoe_mechanism(run_dir: Path) -> dict[str, Any]:
    """Per-arm admission mechanism facts from the admission logs."""
    adm = admission_rows(run_dir)
    admitted = [row for row in adm if row.get("result") == "admitted"]
    abandoned = [row for row in adm if row.get("result") == "abandoned"]
    per_lan_admitted = {lan: sum(1 for row in admitted if row["lan"] == lan)
                        for lan in (1, 2)}
    per_lan_abandoned = {lan: sum(1 for row in abandoned if row["lan"] == lan)
                         for lan in (1, 2)}
    per_lan_sources = {lan: sorted({row.get("admit_source", "")
                                    for row in admitted if row["lan"] == lan})
                       for lan in (1, 2)}
    probe_fallback = sum(1 for row in admitted
                         if row.get("admit_source") == "probe_fallback")
    event_admitted = sum(1 for row in admitted
                         if row.get("admit_source") == "event")
    probe_admitted = sum(1 for row in admitted
                         if row.get("admit_source") == "probe")
    return {
        "admitted_total": len(admitted),
        "abandoned_total": len(abandoned),
        "per_lan_admitted": per_lan_admitted,
        "per_lan_abandoned": per_lan_abandoned,
        "per_lan_sources": per_lan_sources,
        "probe_fallback_admitted": probe_fallback,
        "event_admitted": event_admitted,
        "probe_admitted": probe_admitted,
        "event_fraction": event_admitted / len(admitted) if admitted else None,
    }


def qoe_drop_modes(run_dir: Path) -> list[str]:
    modes: set[str] = set()
    for lan in (1, 2):
        path = run_dir / f"controller_lan{lan}.log"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            for match in re.finditer(r"\bmode=(all|alternate)\b", line):
                modes.add(match.group(1))
    return sorted(modes)


def qoe_scale_down_in_plateau(run_dir: Path) -> bool:
    bounds = phase_bounds(run_dir)
    start, end = bounds[PHASE]
    for event in read_csv(run_dir / "elasticity_events.csv"):
        event_time = timestamp(event.get("timestamp_s") or event.get("timestamp"))
        kind = (event.get("event_type") or event.get("event") or "").lower()
        if event_time is not None and start <= event_time < end and "scale_down" in kind:
            return True
    return False


def qoe_quota(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "quota_snapshot.json"
    if not path.exists():
        return {"requested_edge_cpus": None, "all_compute_containers_match": False,
                "present": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "requested_edge_cpus": data.get("requested_edge_cpus"),
        "all_compute_containers_match": bool(data.get("all_compute_containers_match")),
        "present": True,
    }


def qoe_pressure(run_dir: Path) -> dict[str, Any]:
    """Old-backend CPU pressure before first readiness in the plateau.

    Primary method: capacity_summary's 30 s pre-first-true-ready window, used
    only when the run has >=1 admission (the anchor is defined). The
    zero-admission cell (event_only loss_all: every spawn abandoned) has no
    first-ready anchor, so it uses the plateau_first_120s measure — the static
    tier carries the plateau while every spawn is abandoned. Any OTHER
    capacity_summary failure is surfaced as an error (never silently
    reinterpreted as the fallback).
    """
    admitted = sum(1 for row in admission_rows(run_dir)
                   if row.get("result") == "admitted")
    if admitted == 0:
        bounds = phase_bounds(run_dir)
        start = bounds[PHASE][0]
        end = start + 120.0
        pre: dict[int, dict[str, Any]] = {}
        for lan in (1, 2):
            window = load_window_log(run_dir, lan)
            early = [row for row in window if start <= row["_window_end"] < end]
            ids: set[str] = set()
            for row in early:
                ids.update((row.get("servers") or {}).keys())
            values = cpu_values(window, start, end, ids) if ids else []
            pre[lan] = {"median": statistics.median(values) if values else None,
                        "p95": percentile(values, 0.95)}
        return {"pre_cpu": pre, "method": "plateau_first_120s", "error": None}
    try:
        summary = capacity_summary(run_dir)
        pre = summary["pre_cpu"]
        return {"pre_cpu": {lan: {"median": pre[lan]["median"], "p95": pre[lan]["p95"]}
                            for lan in (1, 2)},
                "method": "pre_first_ready", "error": None}
    except Exception as exc:  # noqa: BLE001 - artifact-dependent by design
        return {"pre_cpu": {1: {"median": None, "p95": None},
                            2: {"median": None, "p95": None}},
                "method": "error",
                "error": f"capacity_summary failed: {exc}"}


def qoe_pressure_ok(pressure: dict[str, Any]) -> bool:
    return all(
        value["median"] is not None and value["p95"] is not None
        and 60.0 <= value["median"] <= 85.0 and value["p95"] <= 95.0
        for value in pressure["pre_cpu"].values()
    )


def qoe_run_metrics(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    metrics = qoe_rate_metrics(run_dir)
    metrics["mechanism"] = qoe_mechanism(run_dir)
    metrics["drop_modes"] = qoe_drop_modes(run_dir)
    metrics["scale_down_in_plateau"] = qoe_scale_down_in_plateau(run_dir)
    metrics["quota"] = qoe_quota(run_dir)
    metrics["pressure"] = qoe_pressure(run_dir)
    metrics["pressure_ok"] = qoe_pressure_ok(metrics["pressure"])
    return metrics


def _pooled_bad(m: dict[str, Any], window: str = "plateau") -> float | None:
    return m[window]["pooled"]["bad_rate"]


def _pooled_timeout(m: dict[str, Any], window: str = "plateau") -> float | None:
    return m[window]["pooled"]["timeout_rate"]


def _pooled_slow(m: dict[str, Any], window: str = "plateau") -> float | None:
    return m[window]["pooled"]["slow_rate"]


def _base_gates(m: dict[str, Any], rung: str) -> dict[str, bool]:
    """Run-level gates shared by every screen: floors, driver, baseline health,
    quota match, no plateau scale-down, pressure."""
    baseline_bad = _pooled_bad(m, "baseline")
    return {
        "floors": all(m["floors"].values()),
        "driver_clean": m["driver_clean"],
        "baseline_bad_le_1pct": (baseline_bad is not None
                                  and baseline_bad <= CONTROL_BAD_MAX),
        "baseline_http000_zero": m["baseline_http000_zero"],
        "quota_matches": (m["quota"]["present"]
                          and m["quota"]["all_compute_containers_match"]
                          and str(m["quota"]["requested_edge_cpus"]) == str(rung)),
        "no_plateau_scale_down": not m["scale_down_in_plateau"],
        "pressure_ok": m["pressure_ok"],
    }


def _mechanism_gate(m: dict[str, Any], arm: str, fault: str) -> dict[str, bool]:
    mech = m["mechanism"]
    if fault == "loss_all":
        if arm == "event_only":
            return {
                "zero_admitted": mech["admitted_total"] == 0,
                "abandoned_ge_2_per_lan": all(
                    mech["per_lan_abandoned"][lan] >= 2 for lan in (1, 2)),
                "zero_probe_fallback": mech["probe_fallback_admitted"] == 0,
                "drop_mode_all": "all" in m["drop_modes"],
            }
        if arm == "hybrid":
            return {
                "all_admitted_probe_fallback": (
                    mech["admitted_total"] > 0
                    and mech["probe_fallback_admitted"] == mech["admitted_total"]),
                "admit_ge_1_per_lan": all(
                    mech["per_lan_admitted"][lan] >= 1 for lan in (1, 2)),
                "zero_abandoned": mech["abandoned_total"] == 0,
            }
        if arm == "reconcile":
            return {
                "all_admitted_probe": (
                    mech["admitted_total"] > 0
                    and mech["probe_admitted"] == mech["admitted_total"]),
                "admit_ge_1_per_lan": all(
                    mech["per_lan_admitted"][lan] >= 1 for lan in (1, 2)),
                "zero_abandoned": mech["abandoned_total"] == 0,
            }
    if fault == "none" and arm == "event_only":
        return {
            "event_fraction_1": mech["event_fraction"] == 1.0,
            "event_admit_ge_1_per_lan": all(
                "event" in mech["per_lan_sources"][lan] for lan in (1, 2)),
            "zero_abandoned": mech["abandoned_total"] == 0,
        }
    return {}


def _contrast_delta(event_m: dict[str, Any], hybrid_m: dict[str, Any],
                    reconcile_m: dict[str, Any]) -> float | None:
    event_timeout = _pooled_timeout(event_m)
    controls = [value for value in (_pooled_timeout(hybrid_m),
                                    _pooled_timeout(reconcile_m))
                if value is not None]
    if event_timeout is None or not controls:
        return None
    return event_timeout - max(controls)


def _contrast_delta_slow(event_m: dict[str, Any], hybrid_m: dict[str, Any],
                         reconcile_m: dict[str, Any]) -> float | None:
    event_slow = _pooled_slow(event_m)
    controls = [value for value in (_pooled_slow(hybrid_m),
                                    _pooled_slow(reconcile_m))
                if value is not None]
    if event_slow is None or not controls:
        return None
    return event_slow - max(controls)


def _screen_run_row(m: dict[str, Any], rung: str, arm: str, fault: str) -> dict[str, Any]:
    base = _base_gates(m, rung)
    mechanism = _mechanism_gate(m, arm, fault)
    is_control = (fault == "loss_all" and arm in ("hybrid", "reconcile")) \
        or (fault == "none" and arm == "event_only")
    bad = _pooled_bad(m)
    slow = _pooled_slow(m)
    control = (bad is not None and bad <= CONTROL_BAD_MAX) if is_control else None
    control_slow = (slow is not None and slow <= SLOW_CONTROL_MAX) if is_control else None
    base_ok = all(base.values())
    mech_ok = bool(mechanism) and all(mechanism.values())
    gates = {**base, **mechanism}
    gates["control_bad_le_1pct"] = control
    gates["control_slow_le_2pp"] = control_slow
    return {
        "run": m["run"],
        "cell": m["parts"][0] if m["parts"] else None,
        "arm": m["parts"][1] if m["parts"] else None,
        "quota_requested": m["quota"]["requested_edge_cpus"],
        "bad_rate_plateau_pooled": _pooled_bad(m),
        "timeout_rate_plateau_pooled": _pooled_timeout(m),
        "slow_rate_plateau_pooled": slow,
        "slow5_rate_plateau_pooled": m["plateau"]["pooled"]["slow5_rate"],
        "lat_completed_p95_plateau_pooled": m["plateau"]["pooled"]["lat_completed_p95"],
        "cancel_rate_plateau_pooled": m["plateau"]["pooled"]["cancel_rate"],
        "gates": gates,
        "base_ok": base_ok,
        "mechanism_ok": mech_ok,
        "pressure_method": m["pressure"]["method"],
        "pressure_error": m["pressure"].get("error"),
        "eligible_run": base_ok and mech_ok
            and (control is None or control)
            and (control_slow is None or control_slow),
    }


def qoe_screen_exit(verdict: str) -> int:
    return {"lock": 0, "descend": 2, "stop_null": 3}[verdict]


def _qoe_stop(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 3


def _event_bounded(m: dict[str, Any]) -> tuple[float | None, float | None, bool]:
    bad = _pooled_bad(m)
    slow = _pooled_slow(m)
    return (bad, slow,
            bad is not None and bad <= BOUNDED_BAD_MAX
            and slow is not None and slow <= SLOW_BOUNDED_EVENT_MAX)


def qoe_screen_command(args: argparse.Namespace) -> int:
    try:
        return _qoe_screen_body(args)
    except ValueError as exc:
        # Malformed invocations (bad labels, wrong arm sets) must never be
        # mistaken for a legitimate "descend" (exit 2): STOP for diagnosis.
        return _qoe_stop(str(exc))


def _qoe_screen_body(args: argparse.Namespace) -> int:
    if args.stage in ("c2", "c3"):
        if not args.c1_json:
            raise ValueError(f"--c1-json is required for --stage {args.stage}")
    pairs = [(qoe_label_parts(Path(path).name), Path(path)) for path in args.run_dir]
    unparsed = [str(path) for label, path in pairs if label is None]
    if unparsed:
        raise ValueError(f"cannot parse qoe labels for: {unparsed}")
    labels = [label for label, _ in pairs]
    for label in labels:
        if labels.count(label) > 1:
            raise ValueError(f"duplicate run label in screen: {'_'.join(label)}")
    runs = {label: qoe_run_metrics(path) for label, path in pairs}
    rung = args.rung
    rows: list[dict[str, Any]] = []

    if args.stage == "c1":
        expected = {("loss_all", "event_only"), ("loss_all", "hybrid"),
                    ("loss_all", "reconcile")}
        found = {label[:2] for label in runs if label is not None}
        if found != expected:
            raise ValueError(f"C1 screen needs the 3 loss_all arms; got {sorted(found)}")
        by_arm = {label[1]: runs[label] for label in runs if label is not None}
        for arm in ("event_only", "hybrid", "reconcile"):
            rows.append(_screen_run_row(by_arm[arm], rung, arm, "loss_all"))
        delta = _contrast_delta(by_arm["event_only"], by_arm["hybrid"],
                                by_arm["reconcile"])
        delta_slow = _contrast_delta_slow(by_arm["event_only"], by_arm["hybrid"],
                                          by_arm["reconcile"])
        eligible = all(row["eligible_run"] for row in rows) \
            and (delta is not None or delta_slow is not None)
        # Slow-first precedence (deterministic, pre-registered): the slow
        # axis is the user-facing QoE axis.
        lock_component = None
        if delta_slow is not None and delta_slow >= CONTRAST_PP:
            lock_component = "slow"
        elif delta is not None and delta >= CONTRAST_PP:
            lock_component = "timeout"
        event_bad, event_slow, bounded_ok = _event_bounded(by_arm["event_only"])
        visible = lock_component is not None
        verdict = ("stop_null" if eligible and visible and not bounded_ok
                   else "lock" if eligible and visible and bounded_ok
                   else "descend")
        output = {"stage": "c1", "rung": rung, "seed": args.seed, "runs": rows,
                  "contract_version": 2,
                  "delta_pp": round(delta * 100.0, 2) if delta is not None else None,
                  "delta_slow_pp": round(delta_slow * 100.0, 2)
                  if delta_slow is not None else None,
                  "lock_component": lock_component,
                  "event_only_bounded": {
                      "bad_ok": event_bad is not None and event_bad <= BOUNDED_BAD_MAX,
                      "slow_ok": event_slow is not None
                      and event_slow <= SLOW_BOUNDED_EVENT_MAX,
                      "bad_rate": event_bad, "slow_rate": event_slow},
                  "bounded_ok": bounded_ok, "verdict": verdict,
                  "lock_candidate": verdict == "lock", "eligible": eligible}
    elif args.stage == "c2":
        c1_path = Path(args.c1_json)
        try:
            c1 = json.loads(c1_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _qoe_stop(f"cannot read c1 json {c1_path}: {exc}")
        if c1.get("contract_version") != 2:
            return _qoe_stop(
                f"c1 json must be contract_version 2 (slow-share amendment), "
                f"got {c1.get('contract_version')!r}: {c1_path}")
        verdict = c1.get("verdict")
        lock_component = c1.get("lock_component")
        bounded_ok = c1.get("bounded_ok")
        if verdict == "lock":
            component_delta = (c1.get("delta_slow_pp")
                               if lock_component == "slow" else c1.get("delta_pp"))
            coherent = (bounded_ok is True
                        and lock_component in ("timeout", "slow")
                        and component_delta is not None
                        and component_delta >= CONTRAST_PP * 100.0)
        elif verdict in ("descend", "stop_null"):
            coherent = True
        else:
            coherent = False
        if not coherent:
            return _qoe_stop(
                f"c1 json incoherent for verdict {verdict!r} "
                f"(lock_component={lock_component!r}, bounded_ok={bounded_ok!r}, "
                f"delta_pp={c1.get('delta_pp')!r}, "
                f"delta_slow_pp={c1.get('delta_slow_pp')!r}): {c1_path}")
        output = {"stage": "c2", "rung": rung, "seed": args.seed,
                  "c1_json": args.c1_json, "contract_version": 2,
                  "delta_pp": c1.get("delta_pp"),
                  "delta_slow_pp": c1.get("delta_slow_pp"),
                  "lock_component": lock_component,
                  "bounded_ok": bounded_ok,
                  "verdict": verdict,
                  "lock_candidate": verdict == "lock"}
    elif args.stage == "c3":
        c1_path = Path(args.c1_json)
        try:
            c1 = json.loads(c1_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _qoe_stop(f"cannot read c1 json {c1_path}: {exc}")
        if c1.get("contract_version") != 2:
            return _qoe_stop(
                f"c1 json must be contract_version 2 (slow-share amendment), "
                f"got {c1.get('contract_version')!r}: {c1_path}")
        lock_component = c1.get("lock_component")
        if c1.get("verdict") != "lock" or lock_component not in ("timeout", "slow"):
            return _qoe_stop(
                f"C3 requires a locked c1.json (verdict=lock), got "
                f"verdict={c1.get('verdict')!r} lock_component={lock_component!r}")
        expected = {("loss_all", "event_only"), ("loss_all", "hybrid"),
                    ("loss_all", "reconcile"), ("none", "event_only")}
        found = {label[:2] for label in runs if label is not None}
        if found != expected:
            raise ValueError(f"C3 screen needs 3 loss_all arms + none event_only; got {sorted(found)}")
        by_key = {label[:2]: runs[label] for label in runs if label is not None}
        for key in (("loss_all", "event_only"), ("loss_all", "hybrid"),
                    ("loss_all", "reconcile"), ("none", "event_only")):
            arm, fault = key[1], key[0]
            rows.append(_screen_run_row(by_key[key], rung, arm, fault))
        delta = _contrast_delta(by_key[("loss_all", "event_only")],
                                by_key[("loss_all", "hybrid")],
                                by_key[("loss_all", "reconcile")])
        delta_slow = _contrast_delta_slow(by_key[("loss_all", "event_only")],
                                          by_key[("loss_all", "hybrid")],
                                          by_key[("loss_all", "reconcile")])
        confirm_delta = delta_slow if lock_component == "slow" else delta
        event_bad, event_slow, bounded_ok = _event_bounded(
            by_key[("loss_all", "event_only")])
        none_row = next(row for row in rows
                        if row["arm"] == "event_only" and row["cell"] == "none")
        none_health_fail = (
            none_row["gates"].get("control_bad_le_1pct") is False
            or none_row["gates"].get("control_slow_le_2pp") is False)
        seed_gates_ok = all(row["eligible_run"] for row in rows)
        seed_confirm = bool(seed_gates_ok and confirm_delta is not None
                            and confirm_delta >= CONTRAST_PP and bounded_ok)
        # Monotone-gate failure (boundedness / none-cell health) at the first
        # visible rung terminates the family (STOP-NULL); contrast
        # non-reproduction follows the existing next-rung rule (descend).
        verdict = ("lock" if seed_confirm
                   else "stop_null" if (not bounded_ok or none_health_fail)
                   else "descend")
        output = {"stage": "c3", "rung": rung, "seed": args.seed, "runs": rows,
                  "contract_version": 2, "lock_component": lock_component,
                  "delta_pp": round(delta * 100.0, 2) if delta is not None else None,
                  "delta_slow_pp": round(delta_slow * 100.0, 2)
                  if delta_slow is not None else None,
                  "bounded_ok": bounded_ok,
                  "seed_gates_ok": seed_gates_ok,
                  "none_health_fail": none_health_fail,
                  "seed_confirm": seed_confirm,
                  "verdict": verdict}
    else:  # pragma: no cover - argparse restricts choices
        raise ValueError(f"unknown qoe-screen stage: {args.stage}")

    write_json(Path(args.out), output)
    print(json.dumps(output, indent=2, default=str))
    return qoe_screen_exit(output["verdict"])


def _campaign_run_rates(run_dir: Path) -> dict[str, Any]:
    m = qoe_rate_metrics(run_dir)
    return {"run": m["run"], "parts": m["parts"],
            "bad": _pooled_bad(m), "timeout": _pooled_timeout(m),
            "slow": _pooled_slow(m),
            "floors_ok": all(m["floors"].values()),
            "driver_clean": m["driver_clean"]}


def _campaign_mechanisms(run_dir: Path) -> dict[str, Any]:
    result = classify(run_dir)
    return {
        "run": run_dir.name,
        "parts": qoe_label_parts(run_dir.name),
        "preservation": result["preservation"],
        "per_lan_preservation": result["per_lan_preservation"],
        "admitted": result["admitted"],
        "abandoned": result["abandoned"],
        "spawned": result["spawned"],
    }


def qoe_campaign_command(args: argparse.Namespace) -> int:
    run_dirs = [Path(path) for path in args.run_dir]
    rate_rows = {path.name: _campaign_run_rates(path) for path in run_dirs}
    mech_rows = {path.name: _campaign_mechanisms(path) for path in run_dirs}
    cells: dict[tuple[int, str, str], dict[str, Any]] = {}
    for path in run_dirs:
        info = rate_rows[path.name]
        parts = info["parts"]
        if parts is None:
            raise ValueError(f"cannot parse qoe label: {path.name}")
        cell, arm, block = parts
        key = (int(block), cell, arm)
        if key in cells:
            raise ValueError(f"duplicate run for cell/arm/block {key}")
        cells[key] = info
    blocks = sorted({key[0] for key in cells})
    if blocks != list(range(1, 7)):
        raise ValueError(f"expected E blocks 1..6, got {blocks}")

    block_contrast: list[float] = []
    block_slow_contrast: list[float] = []
    block_bad: dict[tuple[int, str], float | None] = {}
    block_slow: dict[tuple[int, str], float | None] = {}
    assessable_blocks: list[int] = []
    assessable_slow_blocks: list[int] = []
    exclusions: dict[int, str] = {}
    exclusions_slow: dict[int, str] = {}
    for block in blocks:
        def val(cell: str, arm: str) -> dict[str, Any]:
            return cells[(block, cell, arm)]
        eo = val("loss_all", "event_only")
        hy = val("loss_all", "hybrid")
        rc = val("loss_all", "reconcile")
        none_eo = val("none", "event_only")
        none_hy = val("none", "hybrid")
        none_rc = val("none", "reconcile")
        for arm in ("event_only", "hybrid", "reconcile"):
            for cell in ("none", "loss_all", "loss_alt"):
                block_bad[(block, cell, arm)] = cells[(block, cell, arm)]["bad"]
                block_slow[(block, cell, arm)] = cells[(block, cell, arm)]["slow"]
        alt_eo = val("loss_alt", "event_only")
        alt_hy = val("loss_alt", "hybrid")
        alt_rc = val("loss_alt", "reconcile")
        controls = [hy["bad"], rc["bad"]]
        controls_slow = [hy["slow"], rc["slow"]]
        floors = all(info["floors_ok"] and info["driver_clean"]
                     for info in (eo, hy, rc, none_eo, none_hy, none_rc,
                                  alt_eo, alt_hy, alt_rc))
        controls_ok = all(value is not None and value <= CONTROL_BAD_MAX
                          for value in controls)
        controls_slow_ok = all(value is not None and value <= SLOW_CONTROL_MAX
                               for value in controls_slow)
        delta = None
        if eo["timeout"] is not None and hy["timeout"] is not None \
                and rc["timeout"] is not None:
            delta = eo["timeout"] - max(hy["timeout"], rc["timeout"])
        delta_slow = None
        if eo["slow"] is not None and hy["slow"] is not None \
                and rc["slow"] is not None:
            delta_slow = eo["slow"] - max(hy["slow"], rc["slow"])
        if floors and controls_ok and delta is not None:
            assessable_blocks.append(block)
            block_contrast.append(delta)
        else:
            reasons = []
            if not floors:
                reasons.append("floors_or_driver_clean")
            if not controls_ok:
                reasons.append("controls_bad_rate_gt_1pct")
            if delta is None:
                reasons.append("missing_timeout")
            exclusions[block] = "|".join(reasons)
        if floors and controls_ok and controls_slow_ok and delta_slow is not None:
            assessable_slow_blocks.append(block)
            block_slow_contrast.append(delta_slow)
        else:
            reasons_slow = []
            if not floors:
                reasons_slow.append("floors_or_driver_clean")
            if not controls_ok:
                reasons_slow.append("controls_bad_rate_gt_1pct")
            if not controls_slow_ok:
                reasons_slow.append("controls_slow_rate_gt_2pp")
            if delta_slow is None:
                reasons_slow.append("missing_slow")
            exclusions_slow[block] = "|".join(reasons_slow)

    # H-Q1 headline contrast + boundedness (timeout-assessable blocks).
    n_assessable = len(assessable_blocks)
    contrast_hits = sum(1 for delta in block_contrast if delta >= CONTRAST_PP)
    contrast_median = statistics.median(block_contrast) if block_contrast else None
    hq1_contrast_pass = (n_assessable >= 4 and contrast_hits >= 4
                         and contrast_median is not None
                         and contrast_median >= CONTRAST_PP)
    # H-Q1b slow-share co-primary (slow-assessable blocks; same statistic
    # shape as H-Q1: hits >=4/6 + median, one-sample MWU/Cliff vs zero).
    n_slow_assessable = len(assessable_slow_blocks)
    slow_hits = sum(1 for delta in block_slow_contrast if delta >= CONTRAST_PP)
    slow_contrast_median = statistics.median(block_slow_contrast) \
        if block_slow_contrast else None
    hq1b_contrast_pass = (n_slow_assessable >= 4 and slow_hits >= 4
                          and slow_contrast_median is not None
                          and slow_contrast_median >= CONTRAST_PP)
    # Boundedness: bad over timeout-assessable blocks; slow over
    # slow-assessable blocks. Both ceilings are hard family requirements
    # regardless of which co-primary fires ("visible AND bounded").
    eo_bads = [block_bad[(block, "loss_all", "event_only")] for block in assessable_blocks]
    eo_bads_num = [value for value in eo_bads if value is not None]
    bounded_hits = sum(1 for value in eo_bads_num if value <= BOUNDED_BAD_MAX)
    bounded_median = statistics.median(eo_bads_num) if eo_bads_num else None
    boundedness_pass = (len(eo_bads_num) >= 4 and bounded_hits >= 4
                        and bounded_median is not None and bounded_median <= BOUNDED_BAD_MAX)
    eo_slows = [block_slow[(block, "loss_all", "event_only")]
                for block in assessable_slow_blocks]
    eo_slows_num = [value for value in eo_slows if value is not None]
    slow_bounded_hits = sum(1 for value in eo_slows_num
                            if value <= SLOW_BOUNDED_EVENT_MAX)
    slow_bounded_median = statistics.median(eo_slows_num) if eo_slows_num else None
    slow_boundedness_pass = (len(eo_slows_num) >= 4 and slow_bounded_hits >= 4
                             and slow_bounded_median is not None
                             and slow_bounded_median <= SLOW_BOUNDED_EVENT_MAX)
    if n_assessable < 4:
        hq1_assessable = False
        boundedness_assessable = False
    else:
        hq1_assessable = True
        boundedness_assessable = True
    if n_slow_assessable < 4:
        hq1b_assessable = False
        slow_boundedness_assessable = False
    else:
        hq1b_assessable = True
        slow_boundedness_assessable = True

    # H-Q2 dose ordering per assessable block + supporting MWU (bad axis).
    order_hits = 0
    dose_pairs: list[tuple[float, float]] = []
    for block in assessable_blocks:
        bad_all = block_bad[(block, "loss_all", "event_only")]
        bad_alt = block_bad[(block, "loss_alt", "event_only")]
        bad_none = block_bad[(block, "none", "event_only")]
        if None in (bad_all, bad_alt, bad_none):
            continue
        if bad_all >= bad_alt >= bad_none:
            order_hits += 1
        dose_pairs.append((bad_all, bad_alt))
    dose_pass = order_hits >= 4
    mwu_dose = (exact_mwu([pair[0] for pair in dose_pairs],
                          [pair[1] for pair in dose_pairs])
                if len(dose_pairs) >= 4 else float("nan"))
    cliffs_dose = (cliffs([pair[0] for pair in dose_pairs],
                          [pair[1] for pair in dose_pairs])
                   if len(dose_pairs) >= 4 else float("nan"))
    # H-Q2b slow-dose ordering (slow axis, slow-assessable blocks).
    slow_order_hits = 0
    slow_dose_pairs: list[tuple[float, float]] = []
    for block in assessable_slow_blocks:
        slow_all = block_slow[(block, "loss_all", "event_only")]
        slow_alt = block_slow[(block, "loss_alt", "event_only")]
        slow_none = block_slow[(block, "none", "event_only")]
        if None in (slow_all, slow_alt, slow_none):
            continue
        if slow_all >= slow_alt >= slow_none:
            slow_order_hits += 1
        slow_dose_pairs.append((slow_all, slow_alt))
    slow_dose_pass = slow_order_hits >= 4
    mwu_slow_dose = (exact_mwu([pair[0] for pair in slow_dose_pairs],
                               [pair[1] for pair in slow_dose_pairs])
                     if len(slow_dose_pairs) >= 4 else float("nan"))
    cliffs_slow_dose = (cliffs([pair[0] for pair in slow_dose_pairs],
                               [pair[1] for pair in slow_dose_pairs])
                        if len(slow_dose_pairs) >= 4 else float("nan"))

    # H-Q3 mechanism carryover via classify().
    preservation_failures: list[str] = []
    loss_alt_observations: list[str] = []
    for name, info in mech_rows.items():
        parts = info["parts"]
        if parts is None:
            continue
        cell, arm, _ = parts
        preservation = info["preservation"]
        if cell == "loss_all":
            if arm == "event_only" and preservation is not None and preservation != 0.0:
                preservation_failures.append(f"H-Q3 {name}: event_only preservation {preservation}")
            if arm in ("hybrid", "reconcile") and preservation is not None \
                    and preservation != 1.0:
                preservation_failures.append(f"H-Q3 {name}: {arm} preservation {preservation}")
        if cell == "loss_alt" and arm == "event_only" and preservation is not None \
                and not (0.3 <= preservation <= 0.7):
            # Hypothesis range only — never a void reason, never a gate.
            loss_alt_observations.append(
                f"H-Q3 {name}: loss_alt event_only preservation {preservation} outside [0.3,0.7]")
        if cell == "loss_alt" and arm in ("hybrid", "reconcile"):
            for lan in (1, 2):
                value = info["per_lan_preservation"].get(lan)
                if value is not None and value != 1.0:
                    preservation_failures.append(
                        f"H-Q3 {name}: loss_alt {arm} lan{lan} preservation {value}")

    # H-Q4 none-cell health floor: bad <=1% AND slow <=2pp, all three arms.
    none_healthy = all(
        (block_bad[(block, "none", arm)] is not None
         and block_bad[(block, "none", arm)] <= CONTROL_BAD_MAX
         and block_slow[(block, "none", arm)] is not None
         and block_slow[(block, "none", arm)] <= SLOW_CONTROL_MAX)
        for block in blocks for arm in ("event_only", "hybrid", "reconcile")
    )

    # Direction checks at blocks 3 (2-of-3) and 5 (3-of-5), per axis.
    def direction_ok(contrasts: list[float], assessable: list[int],
                     limit: int, required: int) -> bool:
        deltas = [contrasts[i]
                  for i, block in enumerate(assessable) if block <= limit]
        return sum(1 for value in deltas if value >= CONTRAST_PP) >= required

    direction_b3 = direction_ok(block_contrast, assessable_blocks, 3, 2)
    direction_b5 = direction_ok(block_contrast, assessable_blocks, 5, 3)
    direction_slow_b3 = direction_ok(block_slow_contrast, assessable_slow_blocks, 3, 2)
    direction_slow_b5 = direction_ok(block_slow_contrast, assessable_slow_blocks, 5, 3)

    # Overall pass: either co-primary certifies (its contrast + its dose);
    # BOTH boundedness components, mechanism carryover and none-cell health
    # are hard requirements regardless of which co-primary fires.
    component_timeout = hq1_contrast_pass and dose_pass
    component_slow = hq1b_contrast_pass and slow_dose_pass
    bounded_all = boundedness_pass and slow_boundedness_pass
    campaign_pass = ((component_timeout or component_slow) and bounded_all
                     and not preservation_failures and none_healthy)

    report: dict[str, Any] = {
        "locked_rung": args.rung,
        "runs": len(run_dirs),
        "assessable_blocks": assessable_blocks,
        "assessable_slow_blocks": assessable_slow_blocks,
        "block_exclusions": exclusions,
        "block_slow_exclusions": exclusions_slow,
        "block_contrasts_pp": [round(value * 100.0, 2) for value in block_contrast],
        "block_slow_contrasts_pp": [round(value * 100.0, 2)
                                    for value in block_slow_contrast],
        "H-Q1": {
            "metric": "timeout_rate",
            "delta_definition": "event_only_loss_all - max(hybrid, reconcile)_loss_all",
            "contrast_ge_5pp_blocks": contrast_hits,
            "contrast_median_pp": round(contrast_median * 100.0, 2)
            if contrast_median is not None else None,
            "assessable": hq1_assessable,
            "pass": hq1_contrast_pass,
            "mwu_p": exact_mwu(block_contrast, [0.0] * len(block_contrast))
            if block_contrast else float("nan"),
            "cliffs_delta": cliffs(block_contrast, [0.0] * len(block_contrast))
            if block_contrast else float("nan"),
            "controls_bad_max_pp": CONTROL_BAD_MAX * 100.0,
        },
        "H-Q1b": {
            "metric": "slow_rate",
            "slow_threshold_s": QOE_SLOW_S,
            "delta_definition": "event_only_loss_all - max(hybrid, reconcile)_loss_all",
            "contrast_ge_5pp_blocks": slow_hits,
            "contrast_median_pp": round(slow_contrast_median * 100.0, 2)
            if slow_contrast_median is not None else None,
            "assessable": hq1b_assessable,
            "pass": hq1b_contrast_pass,
            "mwu_p": exact_mwu(block_slow_contrast, [0.0] * len(block_slow_contrast))
            if block_slow_contrast else float("nan"),
            "cliffs_delta": cliffs(block_slow_contrast,
                                   [0.0] * len(block_slow_contrast))
            if block_slow_contrast else float("nan"),
            "controls_slow_max_pp": SLOW_CONTROL_MAX * 100.0,
        },
        "H-Q1_boundedness": {
            "metric": "bad_rate",
            "event_only_bad_rate_blocks": [round(value, 4) for value in eo_bads_num],
            "median": bounded_median,
            "blocks_le_15pct": bounded_hits,
            "assessable": boundedness_assessable,
            "pass": boundedness_pass,
        },
        "H-Q1b_boundedness": {
            "metric": "slow_rate",
            "event_only_slow_rate_blocks": [round(value, 4) for value in eo_slows_num],
            "median": slow_bounded_median,
            "blocks_le_25pct": slow_bounded_hits,
            "assessable": slow_boundedness_assessable,
            "pass": slow_boundedness_pass,
        },
        "H-Q2": {
            "order_hits": order_hits,
            "pass": dose_pass,
            "mwu_p": mwu_dose,
            "cliffs_delta": cliffs_dose,
        },
        "H-Q2b": {
            "metric": "slow_rate",
            "order_hits": slow_order_hits,
            "pass": slow_dose_pass,
            "mwu_p": mwu_slow_dose,
            "cliffs_delta": cliffs_slow_dose,
        },
        "H-Q3": {"preservation_failures": preservation_failures,
                 "loss_alt_event_only_observations": loss_alt_observations,
                 "pass": not preservation_failures},
        "H-Q4": {"none_cell_healthy": none_healthy, "pass": none_healthy},
        "direction_checks": {"block3_2_of_3": direction_b3,
                             "block5_3_of_5": direction_b5},
        "direction_checks_slow": {"block3_2_of_3": direction_slow_b3,
                                  "block5_3_of_5": direction_slow_b5},
        "component_notes": [],
        "failures": [],
    }
    # Component-level misses are diagnostic notes; they enter "failures" only
    # when no co-primary certifies (pass = not failures, as in stage1).
    component_notes: list[str] = []
    if not hq1_assessable:
        component_notes.append("H-Q1 not assessable (<4 timeout-assessable blocks)")
    elif not hq1_contrast_pass:
        component_notes.append("H-Q1 contrast not reproduced (>=5 pp in >=4/6 + median)")
    if not hq1b_assessable:
        component_notes.append("H-Q1b not assessable (<4 slow-assessable blocks)")
    elif not hq1b_contrast_pass:
        component_notes.append("H-Q1b slow contrast not reproduced (>=5 pp in >=4/6 + median)")
    if not dose_pass:
        component_notes.append("H-Q2 bad-dose ordering not reproduced in >=4/6 blocks")
    if not slow_dose_pass:
        component_notes.append("H-Q2b slow-dose ordering not reproduced in >=4/6 blocks")
    failures: list[str] = []
    if not (component_timeout or component_slow):
        failures.extend(component_notes)
    if not boundedness_pass:
        failures.append(
            "H-Q1 boundedness not assessable (<4 assessable blocks)"
            if len(eo_bads_num) < 4 else
            "H-Q1 boundedness component FAIL "
            "(event_only bad_rate median/>=4 blocks >15%)")
    if not slow_boundedness_pass:
        failures.append(
            "H-Q1b boundedness not assessable (<4 slow-assessable blocks)"
            if len(eo_slows_num) < 4 else
            "H-Q1b boundedness component FAIL "
            "(event_only slow_rate median/>=4 blocks >25%)")
    failures.extend(preservation_failures)
    if not none_healthy:
        failures.append("H-Q4 none-cell health floor violated (bad>1% or slow>2pp)")
    report["component_notes"] = component_notes
    report["failures"] = failures
    # Direction checks are reported, never chased: they never enter "failures"
    # nor affect "pass".
    report["pass"] = campaign_pass
    write_json(Path(args.out), report)
    print(json.dumps({"pass": report["pass"], "assessable_blocks": assessable_blocks,
                      "assessable_slow_blocks": assessable_slow_blocks,
                      "failures": report["failures"]}, indent=2))
    return 0 if report["pass"] else 2


# ---------------------------------------------------------------------------
# Family rq3_timing (2026-09-08, user-approved): readiness-loss relief contrast
# under demand escalation. Single gated axis = slow-share (the timeout class is
# descriptive-only here — the per-phase driver drain truncates long in-flight
# requests, so timeouts can only come from the first ~300 s of the plateau).
# ---------------------------------------------------------------------------

TIMING_LABEL_RE = re.compile(
    r"rq3tim_(none|loss_all)_(event_only|hybrid|reconcile)_(\d+)$")
TIMING_RATE_LADDER = ("2.0", "2.5", "3.0")
TIMING_QUOTA = "0.12"
TIMING_TAIL_S = 300.0           # plateau tail = relief-completion window
TIMING_ONSET_S = 150.0          # onset window = acute demand transient (co-primary)
TIMING_CONTRACT = 2             # contract v2 (2026-09-08): onset-window co-primary
TIMING_TTR_MAX_S = 120.0        # H-T2 time-to-relief bound
TIMING_MEDIAN_MARGIN_PP = 0.07  # preflight power margin on the screen median
TIMING_PRESSURE_MEDIAN_MIN = 60.0  # rq3tim V1: old tier evidenced as bottleneck
TIMING_DYNAMIC_RE = re.compile(r"^edge_server_lan[12]_dyn")


def timing_label_parts(run_name: str) -> tuple[str, str, str] | None:
    match = TIMING_LABEL_RE.search(run_name)
    return (match.group(1), match.group(2), match.group(3)) if match else None


def timing_pressure_ok(m: dict[str, Any]) -> bool:
    """rq3tim V1: old tier evidenced as the bottleneck on the hybrid run.

    Pre-first-ready old-tier pool CPU median >= TIMING_PRESSURE_MEDIAN_MIN on
    both LANs (method pre_first_ready). No p95 upper cap (amendment
    2026-09-08, user-approved): the demand-escalation design makes pre-ready
    p95 spikes expected; regime reasonableness is enforced by relief
    completion, event_only boundedness, and driver-clean instead.
    """
    pressure = m.get("pressure") or {}
    if pressure.get("method") != "pre_first_ready":
        return False
    pre = pressure.get("pre_cpu") or {}
    return all(value.get("median") is not None
               and value["median"] >= TIMING_PRESSURE_MEDIAN_MIN
               for value in pre.values())


def timing_tail_slow(run_dir: Path) -> float | None:
    """slow_rate over the last TIMING_TAIL_S of compute_plateau (pooled).

    Relief-completion gate: controls must recover to healthy slowness in the
    plateau tail (a fixed temporal window, never per-admission attribution).
    """
    rows = request_rows(run_dir)
    bounds = phase_bounds(run_dir)
    start, end = bounds[PHASE]
    tail = [row for row in rows
            if end - TIMING_TAIL_S <= row["_sent"] < end
            and row.get("phase") == PHASE]
    return _qoe_status(tail)["slow_rate"]


def timing_onset_slow(run_dir: Path) -> float | None:
    """slow_rate over the first TIMING_ONSET_S of compute_plateau (pooled).

    Onset-window co-primary (amendment 2026-09-08, user-approved): the
    readiness-loss contrast is onset-transient-dominated, so the acute
    demand-onset window is measured alongside the full plateau. Both deltas
    are primary at the screen (>=5 pp bar) and both carry the lock median
    power margin (>=7 pp). Rows are driver-labeled compute_plateau rows with
    sent_at in [onset, onset+TIMING_ONSET_S).
    """
    rows = request_rows(run_dir)
    bounds = phase_bounds(run_dir)
    start, end = bounds[PHASE]
    onset = [row for row in rows
             if start <= row["_sent"] < min(start + TIMING_ONSET_S, end)
             and row.get("phase") == PHASE]
    return _qoe_status(onset)["slow_rate"]


def timing_relief_metrics(run_dir: Path) -> dict[str, Any]:
    """Spawn-anchored time-to-relief (H-T2 metric, re-anchored 2026-09-26).

    TTR' = (first successful request served by an admitted dynamic backend,
    any phase) - (earliest spawn_started_ts over the admitted relief wave),
    read from the admission logs (`result == "admitted"`) and the client rows
    (completed 2xx with a `backend_id` in the admitted-container set).

    The as-run metric anchored at the plateau onset, but the relief wave is
    spawned 27-35 s BEFORE onset, so the onset-anchored value measured the
    phase boundary, not the relief interval; see
    `docs/operation/testing/experiment/v3/rq3_timing/analysis/
    timing_campaign_reanchored.json`. The old value is retained as
    `ttr_onset_s` (diagnostic only).

    Returns `ttr_s` (used by the campaign), `spawn_to_admit_s`,
    `ttr_onset_s`, `new_backend_successes` (plateau-phase 2xx rows served by
    admitted dynamic containers, for the eo_zero gate) and
    `first_success_sent_s`.
    """
    rows = request_rows(run_dir)
    bounds = phase_bounds(run_dir)
    onset = bounds[PHASE][0] if PHASE in bounds else None
    admitted = [row for row in admission_rows(run_dir)
                if row.get("result") == "admitted"
                and TIMING_DYNAMIC_RE.search(row.get("container", "") or "")
                and timestamp(row.get("spawn_started_ts")) is not None]
    admitted_containers = {row.get("container") for row in admitted}
    spawn_times = [timestamp(row.get("spawn_started_ts")) for row in admitted]
    served_all = [row for row in rows
                  if is_completed(row) and is_success(row)
                  and (row.get("backend_id") or "") in admitted_containers]
    served_plateau = [row for row in served_all
                      if row.get("phase") == PHASE]

    result: dict[str, Any] = {
        "ttr_s": None,
        "spawn_to_admit_s": None,
        "ttr_onset_s": None,
        "new_backend_successes": len(served_plateau),
        "first_success_sent_s": None,
    }
    if admitted and served_all:
        chain_start = min(spawn_times)
        first = min(row["_sent"] for row in served_all)
        result["ttr_s"] = first - chain_start
        result["first_success_sent_s"] = first
    if admitted:
        first_admitted = min(
            admitted, key=lambda row: row.get("_admitted_ts") or float("inf"))
        spawn = timestamp(first_admitted.get("spawn_started_ts"))
        admit = first_admitted.get("_admitted_ts")
        if spawn is not None and admit is not None:
            result["spawn_to_admit_s"] = admit - spawn
    if onset is not None and served_plateau:
        result["ttr_onset_s"] = (
            min(row["_sent"] for row in served_plateau) - onset)
    return result


def _timing_row(path: Path, cell: str, arm: str) -> dict[str, Any]:
    """Screen row for a timing run.

    rq3tim evaluates V1 pressure and control health at the screen level, not
    per row:
      - V1 uses the hybrid run's pre-first-ready median (timing_pressure_ok);
        the reused qoe per-row pressure band ([60,85]/p95<=95) does not apply,
        so per-row pressure is neutralized (both the dict AND the precomputed
        pressure_ok flag that _base_gates actually reads).
      - loss_all control health is the plateau TAIL (relief completion),
        judged at the screen level; a control's full-plateau slow carries the
        legitimate early-window cost and must NOT gate the row.
      - none-cell health (amendment 2026-09-08, user-approved): the no-fault
        cell also pays the universal demand-onset transient (no dynamic
        backend is ready for the first ~40-60 s of the plateau), so a
        full-plateau <=2 pp slow ceiling is structurally unreachable at rate
        S even for a healthy no-fault run. The none-cell slow health floor is
        therefore the plateau TAIL (relief-completion), consistent with the
        loss_all controls; bad stays <=1% on the full plateau.
    """
    metrics = qoe_run_metrics(path)
    metrics = dict(metrics)
    metrics["parts"] = (cell, arm, "1")
    metrics["pressure"] = {
        "pre_cpu": {lan: {"median": 70.0, "p95": 80.0} for lan in (1, 2)},
        "method": "pre_first_ready", "error": None}
    metrics["pressure_ok"] = True
    row = _screen_run_row(metrics, TIMING_QUOTA, arm, cell)
    if cell == "loss_all" and arm in ("hybrid", "reconcile"):
        row["gates"]["control_bad_le_1pct"] = None
        row["gates"]["control_slow_le_2pp"] = None
        row["eligible_run"] = row["base_ok"] and row["mechanism_ok"]
    elif cell == "none":
        # Contract v2 (amendment 2026-09-08): none-cell slow health floor is
        # the plateau TAIL (relief-completion), not the full plateau. bad
        # stays <=1% on the full plateau (full-plateau bad is not inflated by
        # the onset transient the way pooled slow is).
        none_tail = timing_tail_slow(path)
        row["gates"]["control_slow_le_2pp"] = (
            none_tail is not None and none_tail <= SLOW_CONTROL_MAX)
        bad_ok = row["gates"].get("control_bad_le_1pct")
        row["eligible_run"] = row["base_ok"] and row["mechanism_ok"] \
            and (bad_ok is None or bad_ok is True)
    return row


def timing_screen_exit(verdict: str) -> int:
    return {"lock": 0, "candidate": 0, "confirm": 0, "descend": 2,
            "marginal": 2, "stop_null": 3}[verdict]


def _timing_tails(runs: dict[tuple[str, str, str], Path]) -> dict[str, float | None]:
    def tail(arm: str) -> float | None:
        for label, path in runs.items():
            if label[0] == "loss_all" and label[1] == arm:
                return timing_tail_slow(path)
        return None

    return {"hybrid": tail("hybrid"), "reconcile": tail("reconcile")}


def _timing_onsets(runs: dict[tuple[str, str, str], Path]) -> dict[str, float | None]:
    def onset(arm: str) -> float | None:
        for label, path in runs.items():
            if label[0] == "loss_all" and label[1] == arm:
                return timing_onset_slow(path)
        return None

    return {"event_only": onset("event_only"), "hybrid": onset("hybrid"),
            "reconcile": onset("reconcile")}


def _contrast_onset_delta(event_o: float | None, hybrid_o: float | None,
                          reconcile_o: float | None) -> float | None:
    controls = [value for value in (hybrid_o, reconcile_o)
                if value is not None]
    if event_o is None or not controls:
        return None
    return event_o - max(controls)


def timing_screen_command(args: argparse.Namespace) -> int:
    try:
        return _timing_screen_body(args)
    except ValueError as exc:
        # Malformed invocations must never be mistaken for "descend".
        return _qoe_stop(str(exc))


def _timing_screen_body(args: argparse.Namespace) -> int:
    pairs = [(timing_label_parts(Path(path).name), Path(path))
             for path in args.run_dir]
    unparsed = [str(path) for label, path in pairs if label is None]
    if unparsed:
        raise ValueError(f"cannot parse timing labels for: {unparsed}")
    runs = {label: path for label, path in pairs}

    if args.stage == "p1":
        expected = {("loss_all", "event_only"), ("loss_all", "hybrid"),
                    ("loss_all", "reconcile")}
        found = {label[:2] for label in runs}
        if found != expected:
            raise ValueError(f"P1 screen needs the 3 loss_all arms; got {sorted(found)}")
        rows = [_timing_row(runs[label], label[0], label[1])
                for label in sorted(runs)]
        by_arm = {label[1]: qoe_run_metrics(runs[label]) for label in runs}
        delta = _contrast_delta_slow(by_arm["event_only"], by_arm["hybrid"],
                                     by_arm["reconcile"])
        onsets = _timing_onsets(runs)
        onset_delta = _contrast_onset_delta(onsets["event_only"],
                                            onsets["hybrid"],
                                            onsets["reconcile"])
        _, _, bounded_ok = _event_bounded(by_arm["event_only"])
        tails = _timing_tails(runs)
        relief_ok = all(value is not None and value <= SLOW_CONTROL_MAX
                        for value in tails.values())
        # V1 pressure anchors on the hybrid run (30 s pre-first-ready); the
        # rung's spawn epoch is arm-independent, so event_only/reconcile
        # inherit the qualification. Amended 2026-09-08 (user-approved): the
        # escalation design makes pre-ready p95 spikes expected, so V1 = the
        # old tier is the bottleneck = pre-ready median CPU >=60 % (no p95
        # upper cap; collapse guarded by relief completion + boundedness +
        # driver-clean).
        pressure_ok = timing_pressure_ok(by_arm["hybrid"])
        eligible = all(row["eligible_run"] for row in rows) and pressure_ok \
            and relief_ok and delta is not None and onset_delta is not None
        # Contract v2 (amendment 2026-09-08): onset-window Delta is a
        # co-primary -- the candidate/confirm verdict requires BOTH the
        # full-plateau contrast >=5 pp AND the onset-window contrast >=5 pp.
        visible = (delta is not None and delta >= CONTRAST_PP
                   and onset_delta is not None and onset_delta >= CONTRAST_PP)
        verdict = ("stop_null" if eligible and visible and not bounded_ok
                   else "candidate" if eligible and visible and bounded_ok
                   else "descend")
        output = {"stage": "p1", "rate": args.rate, "seed": args.seed,
                  "timing_version": TIMING_CONTRACT, "runs": rows,
                  "delta_slow_pp": round(delta * 100.0, 2)
                  if delta is not None else None,
                  "onset_slow": onsets,
                  "onset_delta_slow_pp": round(onset_delta * 100.0, 2)
                  if onset_delta is not None else None,
                  "control_tail_slow": tails, "relief_ok": relief_ok,
                  "pressure_ok": pressure_ok, "bounded_ok": bounded_ok,
                  "eligible": eligible, "verdict": verdict}
    elif args.stage == "p2":
        expected = {("loss_all", "event_only"), ("loss_all", "hybrid"),
                    ("loss_all", "reconcile"), ("none", "event_only")}
        found = {label[:2] for label in runs}
        if found != expected:
            raise ValueError(
                f"P2 screen needs 3 loss_all arms + none event_only; got {sorted(found)}")
        rows = [_timing_row(runs[label], label[0], label[1])
                for label in sorted(runs)]
        by_arm = {label[1]: qoe_run_metrics(runs[label])
                  for label in runs if label[0] == "loss_all"}
        delta = _contrast_delta_slow(by_arm["event_only"], by_arm["hybrid"],
                                     by_arm["reconcile"])
        onsets = _timing_onsets(runs)
        onset_delta = _contrast_onset_delta(onsets["event_only"],
                                            onsets["hybrid"],
                                            onsets["reconcile"])
        _, _, bounded_ok = _event_bounded(by_arm["event_only"])
        tails = _timing_tails(runs)
        relief_ok = all(value is not None and value <= SLOW_CONTROL_MAX
                        for value in tails.values())
        none_row = next(row for row in rows if row["cell"] == "none")
        none_health_fail = (
            none_row["gates"].get("control_bad_le_1pct") is False
            or none_row["gates"].get("control_slow_le_2pp") is False)
        seed_gates_ok = all(row["eligible_run"] for row in rows)
        # Contract v2 (amendment 2026-09-08): onset-window Delta is a
        # co-primary -- confirm requires BOTH the full-plateau contrast >=5 pp
        # AND the onset-window contrast >=5 pp.
        confirm = bool(seed_gates_ok and relief_ok and bounded_ok
                       and not none_health_fail and delta is not None
                       and delta >= CONTRAST_PP and onset_delta is not None
                       and onset_delta >= CONTRAST_PP)
        # Monotone failures (controls never recover / none-cell sick /
        # unbounded) terminate the family; the rest descends the S ladder.
        verdict = ("confirm" if confirm
                   else "stop_null" if (not bounded_ok or none_health_fail
                                        or not relief_ok)
                   else "descend")
        output = {"stage": "p2", "rate": args.rate, "seed": args.seed,
                  "timing_version": TIMING_CONTRACT, "runs": rows,
                  "delta_slow_pp": round(delta * 100.0, 2)
                  if delta is not None else None,
                  "onset_slow": onsets,
                  "onset_delta_slow_pp": round(onset_delta * 100.0, 2)
                  if onset_delta is not None else None,
                  "control_tail_slow": tails, "relief_ok": relief_ok,
                  "bounded_ok": bounded_ok,
                  "none_health_fail": none_health_fail,
                  "seed_gates_ok": seed_gates_ok, "verdict": verdict}
    elif args.stage == "lock":
        if not args.screen_json:
            raise ValueError("--screen-json is required for --stage lock")
        screens = [json.loads(Path(path).read_text(encoding="utf-8"))
                   for path in args.screen_json]
        for screen in screens:
            if screen.get("timing_version") != TIMING_CONTRACT:
                raise ValueError(
                    f"screen must be timing_version {TIMING_CONTRACT}: "
                    f"{screen.get('timing_version')!r}")
        p1 = next((s for s in screens if s.get("stage") == "p1"), None)
        p2s = [s for s in screens if s.get("stage") == "p2"]
        if p1 is None or not p2s:
            raise ValueError("lock needs exactly one p1 screen plus p2 screens")
        # Contract v2: full-plateau AND onset-window deltas are co-primaries;
        # the lock bar (>=5 pp per screen, >=7 pp median power margin) applies
        # to BOTH.
        deltas = [screen.get("delta_slow_pp") for screen in screens]
        onset_deltas = [screen.get("onset_delta_slow_pp")
                        for screen in screens]
        valid = (all(value is not None and value >= CONTRAST_PP * 100.0
                     for value in deltas)
                 and all(value is not None and value >= CONTRAST_PP * 100.0
                         for value in onset_deltas))
        median = statistics.median([value for value in deltas
                                    if value is not None]) if deltas and all(
            value is not None for value in deltas) else None
        onset_median = statistics.median(
            [value for value in onset_deltas if value is not None]
        ) if onset_deltas and all(value is not None
                                  for value in onset_deltas) else None
        verdict_ok = (p1.get("verdict") == "candidate"
                      and all(s.get("verdict") == "confirm" for s in p2s))
        bounded_all = all(s.get("bounded_ok") is True for s in screens)
        relief_all = all(s.get("relief_ok") is True for s in screens)
        if verdict_ok and valid and bounded_all and relief_all:
            verdict = ("lock" if (median is not None and onset_median is not None
                                  and median >= TIMING_MEDIAN_MARGIN_PP * 100.0
                                  and onset_median
                                  >= TIMING_MEDIAN_MARGIN_PP * 100.0)
                       else "marginal")
        else:
            verdict = "descend"
        output = {"stage": "lock", "rate": args.rate,
                  "timing_version": TIMING_CONTRACT, "screens": len(screens),
                  "deltas_pp": deltas, "median_delta_pp": median,
                  "onset_deltas_pp": onset_deltas,
                  "onset_median_delta_pp": onset_median,
                  "verdict": verdict, "lock": verdict == "lock"}
        if verdict == "lock" and args.lock_out:
            Path(args.lock_out).write_text(json.dumps(
                {"family": "rq3_timing", "rate": args.rate,
                 "quota": TIMING_QUOTA, "timing_version": TIMING_CONTRACT,
                 "screens": len(screens),
                 "deltas_pp": deltas, "median_delta_pp": median,
                 "onset_deltas_pp": onset_deltas,
                 "onset_median_delta_pp": onset_median,
                 "seeds": [screen.get("seed") for screen in screens]},
                indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:  # pragma: no cover - argparse restricts choices
        raise ValueError(f"unknown timing-screen stage: {args.stage}")

    write_json(Path(args.out), output)
    print(json.dumps(output, indent=2, default=str))
    return timing_screen_exit(output["verdict"])


def timing_campaign_command(args: argparse.Namespace) -> int:
    run_dirs = [Path(path) for path in args.run_dir]
    cells: dict[tuple[int, str, str], Path] = {}
    for path in run_dirs:
        parts = timing_label_parts(path.name)
        if parts is None:
            raise ValueError(f"cannot parse timing label: {path.name}")
        cell, arm, block = parts
        key = (int(block), cell, arm)
        if key in cells:
            raise ValueError(f"duplicate run for cell/arm/block {key}")
        cells[key] = path
    blocks = sorted({key[0] for key in cells})
    if blocks != list(range(1, 7)):
        raise ValueError(f"expected E blocks 1..6, got {blocks}")

    block_delta: list[float] = []
    block_onset_delta: list[float] = []
    assessable: list[int] = []
    exclusions: dict[int, str] = {}
    tail_slow: dict[tuple[int, str], float | None] = {}
    onset_slow: dict[tuple[int, str], float | None] = {}
    none_bad: dict[tuple[int, str], float | None] = {}
    none_slow: dict[tuple[int, str], float | None] = {}
    eo_slows: dict[int, float | None] = {}
    ttrs: dict[int, dict[str, float | None]] = {}
    ttr_spawn_admit: dict[int, dict[str, float | None]] = {}
    ttr_onset_diag: dict[int, dict[str, float | None]] = {}
    eo_new_successes: dict[int, int] = {}
    for block in blocks:
        infos = {key: _campaign_run_rates(cells[(block, *key)])
                 for key in (("loss_all", "event_only"), ("loss_all", "hybrid"),
                             ("loss_all", "reconcile"), ("none", "event_only"),
                             ("none", "hybrid"), ("none", "reconcile"))}
        floors = all(info["floors_ok"] and info["driver_clean"]
                     for info in infos.values())
        eo = infos[("loss_all", "event_only")]
        hy = infos[("loss_all", "hybrid")]
        rc = infos[("loss_all", "reconcile")]
        tails = {arm: timing_tail_slow(cells[(block, "loss_all", arm)])
                 for arm in ("hybrid", "reconcile")}
        for arm in ("hybrid", "reconcile"):
            tail_slow[(block, arm)] = tails[arm]
        controls_tail_ok = all(value is not None and value <= SLOW_CONTROL_MAX_LOST
                               for value in tails.values())
        delta_slow = None
        if eo["slow"] is not None and hy["slow"] is not None \
                and rc["slow"] is not None:
            delta_slow = eo["slow"] - max(hy["slow"], rc["slow"])
        # Contract v2 co-primary: onset-window Delta (first 150 s of the
        # compute_plateau), event_only - max(hybrid, reconcile).
        on_vals = {arm: timing_onset_slow(cells[(block, "loss_all", arm)])
                   for arm in ("event_only", "hybrid", "reconcile")}
        for arm in ("event_only", "hybrid", "reconcile"):
            onset_slow[(block, arm)] = on_vals[arm]
        onset_delta_slow = None
        if on_vals["event_only"] is not None and on_vals["hybrid"] is not None \
                and on_vals["reconcile"] is not None:
            onset_delta_slow = (on_vals["event_only"]
                                - max(on_vals["hybrid"], on_vals["reconcile"]))
        if floors and controls_tail_ok and delta_slow is not None:
            assessable.append(block)
            block_delta.append(delta_slow)
            block_onset_delta.append(onset_delta_slow)
        else:
            reasons = []
            if not floors:
                reasons.append("floors_or_driver_clean")
            if not controls_tail_ok:
                reasons.append("controls_tail_slow_gt_2pp")
            if delta_slow is None:
                reasons.append("missing_slow")
            exclusions[block] = "|".join(reasons)
        eo_slows[block] = eo["slow"]
        for arm in ("event_only", "hybrid", "reconcile"):
            none_bad[(block, arm)] = infos[("none", arm)]["bad"]
            # Contract v2 (amendment 2026-09-08): none-cell slow health floor
            # is the plateau TAIL (relief-completion), consistent with the
            # loss_all controls and the P2 screen; the full-plateau slow of a
            # no-fault run carries the universal onset transient.
            none_slow[(block, arm)] = timing_tail_slow(
                cells[(block, "none", arm)])
        relief = {arm: timing_relief_metrics(cells[(block, "loss_all", arm)])
                  for arm in ("event_only", "hybrid", "reconcile")}
        ttrs[block] = {arm: relief[arm]["ttr_s"]
                       for arm in ("hybrid", "reconcile")}
        ttr_spawn_admit[block] = {arm: relief[arm].get("spawn_to_admit_s")
                                  for arm in ("hybrid", "reconcile")}
        ttr_onset_diag[block] = {arm: relief[arm].get("ttr_onset_s")
                                 for arm in ("hybrid", "reconcile")}
        eo_new_successes[block] = relief["event_only"]["new_backend_successes"]

    # H-T1 headline: per-block delta_slow AND onset delta (assessable blocks
    # only). Contract v2 (amendment 2026-09-08): the onset-window contrast is
    # a co-primary -- H-T1 passes only when BOTH the full-plateau contrast and
    # the onset-window contrast hold (>=5 pp in >=4/6 assessable blocks, each
    # median >=5 pp).
    hits = sum(1 for value in block_delta if value >= CONTRAST_PP)
    median = statistics.median(block_delta) if block_delta else None
    h1_full_pass = (len(assessable) >= 4 and hits >= 4
                    and median is not None and median >= CONTRAST_PP)
    onset_hits = sum(1 for value in block_onset_delta
                     if value is not None and value >= CONTRAST_PP)
    onset_deltas_num = [value for value in block_onset_delta
                        if value is not None]
    onset_median = (statistics.median(onset_deltas_num)
                    if onset_deltas_num else None)
    h1_onset_pass = (len(assessable) >= 4 and onset_hits >= 4
                     and onset_median is not None
                     and onset_median >= CONTRAST_PP)
    h1_pass = h1_full_pass and h1_onset_pass
    eo_slows_num = [value for value in (eo_slows[b] for b in assessable)
                    if value is not None]
    bounded_hits = sum(1 for value in eo_slows_num
                       if value <= SLOW_BOUNDED_EVENT_MAX)
    bounded_median = statistics.median(eo_slows_num) if eo_slows_num else None
    boundedness_pass = (len(eo_slows_num) >= 4 and bounded_hits >= 4
                        and bounded_median is not None
                        and bounded_median <= SLOW_BOUNDED_EVENT_MAX)

    # H-T2 relief completion + ordering (TTR re-anchored at the relief-wave
    # spawn, 2026-09-26; the onset-anchored value is kept as a diagnostic).
    relief_blocks = [block for block in blocks
                     if tail_slow[(block, "hybrid")] is not None
                     and tail_slow[(block, "hybrid")] <= SLOW_CONTROL_MAX
                     and tail_slow[(block, "reconcile")] is not None
                     and tail_slow[(block, "reconcile")] <= SLOW_CONTROL_MAX]
    ttr_hy = [value for value in (ttrs[b]["hybrid"] for b in blocks)
              if value is not None and value <= TIMING_TTR_MAX_S]
    ttr_rc = [value for value in (ttrs[b]["reconcile"] for b in blocks)
              if value is not None and value <= TIMING_TTR_MAX_S]
    ttr_order = bool(ttr_hy and ttr_rc
                     and statistics.median(ttr_hy) > statistics.median(ttr_rc))
    eo_zero = sum(1 for block in blocks if eo_new_successes[block] == 0) >= 5
    h2_pass = (len(relief_blocks) >= 5 and len(ttr_hy) >= 5 and len(ttr_rc) >= 5
               and ttr_order and eo_zero)

    # H-T3 none-cell health floor (bad <=1% full plateau AND plateau-TAIL
    # slow <=2pp, all arms) -- contract v2 none-cell amendment 2026-09-08.
    none_healthy = all(
        none_bad[(block, arm)] is not None
        and none_bad[(block, arm)] <= CONTROL_BAD_MAX
        and none_slow[(block, arm)] is not None
        and none_slow[(block, arm)] <= SLOW_CONTROL_MAX
        for block in blocks for arm in ("event_only", "hybrid", "reconcile"))

    campaign_pass = h1_pass and boundedness_pass and h2_pass and none_healthy

    report: dict[str, Any] = {
        "family": "rq3_timing", "rate": args.rate,
        "timing_version": TIMING_CONTRACT,
        "runs": len(run_dirs), "assessable_blocks": assessable,
        "block_exclusions": exclusions,
        "block_delta_slow_pp": [round(value * 100.0, 2)
                                 for value in block_delta],
        "block_onset_delta_slow_pp": [round(value * 100.0, 2)
                                       if value is not None else None
                                       for value in block_onset_delta],
        "onset_slow": {f"b{block}_{arm}": onset_slow[(block, arm)]
                       for block in blocks for arm in
                       ("event_only", "hybrid", "reconcile")},
        "H-T1": {"delta_slow_ge_5pp_blocks": hits,
                 "median_pp": round(median * 100.0, 2) if median is not None else None,
                 "onset_delta_ge_5pp_blocks": onset_hits,
                 "onset_median_pp": (round(onset_median * 100.0, 2)
                                      if onset_median is not None else None),
                 "full_pass": h1_full_pass, "onset_pass": h1_onset_pass,
                 "pass": h1_pass,
                 "mwu_p": exact_mwu(block_delta, [0.0] * len(block_delta))
                 if block_delta else float("nan"),
                 "cliffs_delta": cliffs(block_delta, [0.0] * len(block_delta))
                 if block_delta else float("nan")},
        "H-T1_boundedness": {"event_only_slow_blocks":
                             [round(value, 4) for value in eo_slows_num],
                             "median": bounded_median,
                             "blocks_le_25pct": bounded_hits,
                             "pass": boundedness_pass},
        "H-T2": {"relief_blocks": relief_blocks,
                 "ttr_hybrid_s": ttr_hy, "ttr_reconcile_s": ttr_rc,
                 "ttr_order_hybrid_gt_reconcile": ttr_order,
                 "spawn_to_admit_s": {f"b{block}_{arm}": ttr_spawn_admit[block][arm]
                                      for block in blocks
                                      for arm in ("hybrid", "reconcile")},
                 "ttr_onset_diagnostic_s": {f"b{block}_{arm}": ttr_onset_diag[block][arm]
                                            for block in blocks
                                            for arm in ("hybrid", "reconcile")},
                 "event_only_zero_new_success_blocks": eo_zero,
                 "pass": h2_pass},
        "H-T3": {"none_cell_healthy": none_healthy, "pass": none_healthy},
        "failures": [],
        "pass": campaign_pass,
    }
    if len(assessable) < 4:
        report["failures"].append("H-T1 not assessable (<4 assessable blocks)")
    elif not h1_full_pass:
        report["failures"].append(
            "H-T1 full-plateau contrast not reproduced (>=5 pp in >=4/6 + median)")
    elif not h1_onset_pass:
        report["failures"].append(
            "H-T1 onset-window contrast not reproduced (>=5 pp in >=4/6 + median)")
    if not boundedness_pass:
        report["failures"].append(
            "H-T1 boundedness FAIL (event_only slow_rate median/>=4 blocks >25%)")
    if not h2_pass:
        report["failures"].append("H-T2 relief completion/ordering not reproduced")
    if not none_healthy:
        report["failures"].append("H-T3 none-cell health floor violated (bad>1% or slow>2pp)")
    write_json(Path(args.out), report)
    print(json.dumps({"pass": report["pass"], "assessable_blocks": assessable,
                      "failures": report["failures"]}, indent=2))
    return 0 if report["pass"] else 2


# ---------------------------------------------------------------------------
# Family 5 (rq3_soundness): readiness-admission fault soundness
#
# Binding contract: docs/operation/testing/experiment/v3/rq3_soundness/
# experiment_plan.md §4 (measurement), §5 (P1 gates), §6 (hypotheses) and
# preflight_campaign.md (R1-R3b rows). One common damage currency (§4.1/§4.2):
#   classes, priority order (first match wins):
#     timeout (driver status=timeout; checked FIRST so its http_status=000
#     encoding cannot be swallowed) -> 000 (completed + http_status=000)
#     -> fail5xx (completed, HTTP >= 500) -> slow (completed, latency > 1.0 s)
#     -> good (completed, <= 1.0 s); canceled/dropped excluded from the
#     offered denominator and reported separately.
#   U = 100 * (#timeout + #000 + #fail5xx + #slow) / #offered
#   D_s(arm, cell) = U(arm, cell, s) - U(arm, none, s), paired by seed.
# Fault windows (§4.3): F2 = [t_bind - N - 10 s, t_bind + 30 s] per app spawn
# (t_bind = first serve-capable timestamp, parsed from the `edge-server
# listening` app-log line; upper bounds are exclusive, the repo's half-open
# window convention); knob-active runs also log READINESS_CLAIM/READINESS_BIND
# and the measured claim->bind deferral must be in [N-1, N+1] s.
# F3 = [t_bind, min(t_bind + S, plateau_end)] (S fixed at 1200 s). F1 = full
# plateau. Matched none-cell comparisons reuse the fault cell's N in the F2
# formula and the fixed S in the F3 formula.
# ---------------------------------------------------------------------------
# The ordinal group may carry a trailing `r` on pre-registered rerun labels
# (parallel launcher fix); it is kept as part of the ordinal.
SOUNDNESS_LABEL_RE = re.compile(
    r"rq3snd_(none|premature2|premature5|premature10|semantic|loss_all)_"
    r"(event_only|hybrid|reconcile|wake_verify)_(\d+r?)$")
SOUNDNESS_DYNAMIC_RE = re.compile(r"^edge_server_lan[12]_dyn\d+$")
SOUNDNESS_SLOW_S = 1.0                 # §4.1 slow boundary (Family-4 definition)
SOUNDNESS_F2_PRE_S = 10.0              # §4.3 F2 pre-roll before the claim
SOUNDNESS_F2_POST_S = 30.0             # §4.3 F2 post-bind tail
SOUNDNESS_F3_S_DEFAULT = 1200          # §7.3 fixed lie duration (locked by P1)
SOUNDNESS_GRID = (2, 5, 10)            # §4.4 fixed a priori; no ladder
SOUNDNESS_CENSORED_AS = 11.0           # raw infinity counted as 11 (just beyond)
SOUNDNESS_E_SEEDS = (6301, 6302, 6303)  # E-stage core seeds (run_matrix.md)
SOUNDNESS_TAIL_S = 300.0               # none-floor tail = last 300 s of plateau
SOUNDNESS_NONE_TAIL_SLOW_MAX = 0.02    # §4.5 none plateau-tail slow ceiling
SOUNDNESS_NONE_BAD_MAX = 0.01          # §4.5 none bad ceiling
SOUNDNESS_CANCEL_MAX = 0.05            # §4.5 driver-clean bound
SOUNDNESS_PLATEAU_MIN_POOLED = 5000    # §4.5 floors (reported, not gated)
SOUNDNESS_PLATEAU_MIN_PER_LAN = 1000
SOUNDNESS_F2_WINDOW_MIN_PER_LAN = 15
SOUNDNESS_SEMANTIC_MIN_PP = 30.0       # §6 H-S1/H-S2/H-S4 semantic bar
SOUNDNESS_F2_MAX_PP = 2.0              # §6 H-S2/H-S4 F2 bar
SOUNDNESS_GROWTH_MIN_PP_S = 0.5        # §4.4 growth gate point estimate
SOUNDNESS_GROWTH_CI_MIN_PP_S = 0.2     # §4.4 growth gate CI lower bound
SOUNDNESS_BOOTSTRAP_B = 2000           # seed-bootstrap resamples (95 % CI)
SOUNDNESS_BOOTSTRAP_SEED = 6300        # deterministic bootstrap RNG


def soundness_label_parts(run_name: str) -> tuple[str, str, str] | None:
    match = SOUNDNESS_LABEL_RE.search(run_name)
    return (match.group(1), match.group(2), match.group(3)) if match else None


def soundness_cell_fault(cell: str) -> str:
    if cell == "none":
        return "none"
    if cell.startswith("premature"):
        return "premature"
    return cell


def soundness_cell_n(cell: str) -> int | None:
    """Lead N encoded by a premature cell name (0 for none; None otherwise)."""
    if cell == "none":
        return 0
    match = re.fullmatch(r"premature(\d+)", cell)
    return int(match.group(1)) if match else None


def soundness_class_of(row: dict[str, Any]) -> str:
    """§4.1 outcome class, priority order, first match wins."""
    status = str(row.get("status") or "")
    if status == "timeout":
        return "timeout"
    if status in ("canceled", "dropped"):
        return status
    if status != "completed":
        return "other"
    http = str(row.get("http_status") or "")
    if http == "000":
        return "000"
    try:
        code = int(http)
    except ValueError:
        code = None
    if code is not None and code >= 500:
        return "fail5xx"
    latency = as_float(row.get("latency_s"))
    if latency is not None and latency > SOUNDNESS_SLOW_S:
        return "slow"
    return "good"


def soundness_u(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """§4.2 damage share for one row set (pooled)."""
    counts = {name: 0 for name in ("timeout", "000", "fail5xx", "slow", "good")}
    canceled = dropped = other = 0
    for row in rows:
        cls = soundness_class_of(row)
        if cls in counts:
            counts[cls] += 1
        elif cls == "canceled":
            canceled += 1
        elif cls == "dropped":
            dropped += 1
        else:
            other += 1
    offered = sum(counts.values())
    bad_n = offered - counts["good"]
    return {
        "offered": offered,
        "bad_n": bad_n,
        "u_pct": (100.0 * bad_n / offered) if offered else None,
        "classes": counts,
        "cancelled": canceled,
        "dropped": dropped,
        "other": other,
    }


def soundness_window_u(rows: list[dict[str, Any]],
                       spans: list[tuple[float, float]]) -> dict[str, Any]:
    clean = [(float(lo), float(hi)) for lo, hi in spans
             if lo is not None and hi is not None]
    selected = [row for row in rows
                if any(lo <= row["_sent"] < hi for lo, hi in clean)]
    return {**soundness_u(selected), "window_rows": len(selected)}


def soundness_log_line_ts(line: str) -> float | None:
    """Docker RFC3339 timestamp = first whitespace token (existing convention,
    mirroring readiness_low_headroom.parse_ready_epoch)."""
    parts = line.split()
    if not parts:
        return None
    return timestamp(parts[0])


def soundness_log_markers(run_dir: Path, container: str) -> dict[str, Any]:
    path = run_dir / "service_logs" / f"{container}.log"
    out: dict[str, Any] = {
        "t_listen": None, "t_claim": None, "t_bind_marker": None,
        "deferral_reported_s": None, "lie_until": None,
        "log_present": path.exists(),
    }
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ts = soundness_log_line_ts(line)
        if out["t_listen"] is None and "edge-server listening" in line and ts is not None:
            out["t_listen"] = ts
        if out["t_claim"] is None and "READINESS_CLAIM" in line and ts is not None:
            out["t_claim"] = ts
        if out["t_bind_marker"] is None and "READINESS_BIND" in line and ts is not None:
            out["t_bind_marker"] = ts
        if out["deferral_reported_s"] is None:
            match = re.search(r"deferral_s=([-+0-9.eE]+)", line)
            if match:
                out["deferral_reported_s"] = as_float(match.group(1))
        if out["lie_until"] is None:
            match = re.search(r"until=([-+0-9.eE]+)", line)
            if match:
                out["lie_until"] = as_float(match.group(1))
    return out


def soundness_app_spawns(run_dir: Path) -> list[dict[str, Any]]:
    """Per dynamic app spawn: t_bind (first serve-capable log timestamp) and
    the optional READINESS_CLAIM marker + measured deferral (F2 runs)."""
    logs_dir = run_dir / "service_logs"
    spawns: list[dict[str, Any]] = []
    if not logs_dir.is_dir():
        return spawns
    for path in sorted(logs_dir.glob("edge_server_lan[12]_dyn*.log")):
        container = path.stem
        if not SOUNDNESS_DYNAMIC_RE.match(container):
            continue
        markers = soundness_log_markers(run_dir, container)
        t_bind = (markers["t_listen"] if markers["t_listen"] is not None
                  else markers["t_bind_marker"])
        lan = int(re.search(r"lan(\d)", container).group(1))
        deferral = (t_bind - markers["t_claim"]
                    if t_bind is not None and markers["t_claim"] is not None
                    else None)
        spawns.append({
            "container": container, "lan": lan, "t_bind": t_bind,
            "t_claim": markers["t_claim"],
            "t_listen_logged": markers["t_listen"],
            "t_bind_marker": markers["t_bind_marker"],
            "deferral_s": deferral,
            "deferral_reported_s": markers["deferral_reported_s"],
            "lie_until": markers["lie_until"],
        })
    return spawns


def soundness_attestation(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "rq3snd_fault_attestation.txt"
    knobs: dict[str, int] = {}
    present = path.exists()
    if present:
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in ("EDGE_READY_PREMATURE_S", "EDGE_READY_SEMANTIC_LIE_S"):
            match = re.search(key + r"=(\d+)", text)
            if match:
                knobs[key] = int(match.group(1))
    return {"path": path.name, "present": present, "knobs": knobs}


def _soundness_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def soundness_check_violations(run_dir: Path) -> dict[str, Any]:
    """CHECK VIOLATION counts per LAN controller log. `logs_absent` marks a
    folder carrying neither controller LAN log — the recorded era predates
    that capture, so the log-based gates are then not evaluable, never
    silently passed or failed (plan §7.3 E-P1, 2026-10-13 (j))."""
    per_lan: dict[str, int] = {}
    logs_present = False
    for lan in (1, 2):
        path = run_dir / f"controller_lan{lan}.log"
        if path.exists():
            logs_present = True
            per_lan[f"lan{lan}"] = path.read_text(
                encoding="utf-8", errors="replace").count("CHECK VIOLATION")
        else:
            per_lan[f"lan{lan}"] = 0
    return {**per_lan, "total": sum(per_lan.values()),
            "logs_absent": not logs_present}


def soundness_run_metrics(run_dir: Path) -> dict[str, Any]:
    """Full §4 measurement record for one soundness run folder."""
    run_dir = Path(run_dir)
    label = soundness_label_parts(run_dir.name)
    if label is None:
        raise ValueError(f"cannot parse soundness label: {run_dir.name}")
    cell, arm, ordinal = label
    rows = request_rows(run_dir)
    if not rows:
        raise ValueError(f"no client rows: {run_dir.name}")
    bounds = phase_bounds(run_dir)
    if PHASE not in bounds:
        raise ValueError(f"missing {PHASE} phase: {run_dir.name}")
    plateau_start, plateau_end = bounds[PHASE]
    phases = load_phases(run_dir)
    plateau_cfg = next((phase for phase in phases if phase.get("name") == PHASE), None)
    env_path = run_dir / "controller_env_snapshot.env"
    env = parse_env(env_path) if env_path.exists() else {}
    attestation = soundness_attestation(run_dir)

    n_env = _soundness_int(env.get("EDGE_READY_PREMATURE_S"))
    n_att = attestation["knobs"].get("EDGE_READY_PREMATURE_S")
    coded_n = soundness_cell_n(cell)
    if n_env is not None:
        n, n_source = n_env, "env"
    elif n_att is not None:
        n, n_source = n_att, "attestation"
    elif coded_n is not None:
        n, n_source = coded_n, "cell"
    else:
        n, n_source = 0, "default"
    s_env = _soundness_int(env.get("EDGE_READY_SEMANTIC_LIE_S"))
    s_att = attestation["knobs"].get("EDGE_READY_SEMANTIC_LIE_S")
    if s_env is not None:
        s_lie, s_source = s_env, "env"
    elif s_att is not None:
        s_lie, s_source = s_att, "attestation"
    elif cell == "semantic":
        s_lie, s_source = SOUNDNESS_F3_S_DEFAULT, "default"
    else:
        s_lie, s_source = 0, "default"

    spawns = soundness_app_spawns(run_dir)
    binds = [spawn["t_bind"] for spawn in spawns if spawn["t_bind"] is not None]
    t_bind_min = min(binds) if binds else None
    t_claim_min = min((spawn["t_claim"] for spawn in spawns
                       if spawn["t_claim"] is not None), default=None)

    plateau_rows = [row for row in rows if row.get("phase") == PHASE]
    plateau_u = soundness_u(plateau_rows)
    plateau_per_lan = {
        f"lan{lan}": soundness_u([row for row in plateau_rows
                                  if row.get("client_lan") == f"lan{lan}"])
        for lan in (1, 2)
    }
    tail_rows = [row for row in plateau_rows
                 if plateau_end - SOUNDNESS_TAIL_S <= row["_sent"] < plateau_end]
    tail_u = soundness_u(tail_rows)
    tail_slow_n = tail_u["classes"]["timeout"] + tail_u["classes"]["slow"]
    tail_slow_share = (tail_slow_n / tail_u["offered"]) if tail_u["offered"] else None
    bad_n = (plateau_u["classes"]["timeout"] + plateau_u["classes"]["000"]
             + plateau_u["classes"]["fail5xx"])
    bad_share = (bad_n / plateau_u["offered"]) if plateau_u["offered"] else None

    # F2 windows per app spawn, for the run's own N and every grid N (matched
    # none-cell comparisons reuse the fault cell's N in the same formula).
    f2_u_by_n: dict[str, Any] = {}
    f2_pre_by_n: dict[str, Any] = {}
    f2_floor_by_n: dict[str, Any] = {}
    for n_value in (0, *SOUNDNESS_GRID):
        window_spans = [(spawn["t_bind"] - n_value - SOUNDNESS_F2_PRE_S,
                         spawn["t_bind"] + SOUNDNESS_F2_POST_S)
                        for spawn in spawns if spawn["t_bind"] is not None]
        f2_u_by_n[str(n_value)] = soundness_window_u(rows, window_spans)
        pre_spans = [(spawn["t_bind"] - n_value, spawn["t_bind"])
                     for spawn in spawns if spawn["t_bind"] is not None]
        pre: dict[str, Any] = {"http000": {"lan1": 0, "lan2": 0},
                               "offered": {"lan1": 0, "lan2": 0}}
        for row in rows:
            if not any(lo <= row["_sent"] < hi for lo, hi in pre_spans):
                continue
            lan_key = str(row.get("client_lan") or "")
            if lan_key not in pre["http000"]:
                continue
            pre["offered"][lan_key] += 1
            # §4.1 class, not the raw field: a timeout's http_status="000"
            # encoding must never count as a transport-000 row.
            if soundness_class_of(row) == "000":
                pre["http000"][lan_key] += 1
        f2_pre_by_n[str(n_value)] = pre
        floor: dict[str, int | None] = {"lan1": None, "lan2": None}
        for spawn in spawns:
            if spawn["t_bind"] is None:
                continue
            lo = spawn["t_bind"] - n_value - SOUNDNESS_F2_PRE_S
            hi = spawn["t_bind"] + SOUNDNESS_F2_POST_S
            for lan_key in ("lan1", "lan2"):
                count = sum(1 for row in rows
                            if row.get("client_lan") == lan_key
                            and lo <= row["_sent"] < hi)
                current = floor[lan_key]
                if current is None or count < current:
                    floor[lan_key] = count
        f2_floor_by_n[str(n_value)] = floor

    # F3 lie window (§4.3): [t_bind, min(t_bind + S, plateau_end)], clipped;
    # attribution is per lying backend inside its own window (same clip).
    f3 = None
    if t_bind_min is not None:
        s_use = s_lie if s_lie and s_lie > 0 else SOUNDNESS_F3_S_DEFAULT
        f3_start = t_bind_min
        f3_end = min(t_bind_min + s_use, plateau_end)
        cov_start = max(f3_start, plateau_start)
        coverage = max(0.0, f3_end - cov_start) / (plateau_end - plateau_start)
        window_rows = [row for row in rows if f3_start <= row["_sent"] < f3_end]
        u3 = soundness_u(window_rows)
        # R3(c): §4.1 class 000 only (timeouts keep their own class).
        http000 = sum(1 for row in window_rows
                      if soundness_class_of(row) == "000")
        attributed = {f"lan{lan}": {"completed": 0, "fivexx": 0} for lan in (1, 2)}
        for spawn in spawns:
            if spawn["t_bind"] is None:
                continue
            end = min(spawn["t_bind"] + s_use, plateau_end)
            lan_key = f"lan{spawn['lan']}"
            for row in rows:
                if str(row.get("backend_id") or "") != spawn["container"]:
                    continue
                if not (spawn["t_bind"] <= row["_sent"] < end):
                    continue
                if not is_completed(row):
                    continue
                attributed[lan_key]["completed"] += 1
                try:
                    code = int(str(row.get("http_status") or ""))
                except ValueError:
                    code = None
                if code is not None and code >= 500:
                    attributed[lan_key]["fivexx"] += 1
        total_completed = sum(item["completed"] for item in attributed.values())
        total_fivexx = sum(item["fivexx"] for item in attributed.values())
        f3 = {
            "s_s": s_use, "start": f3_start, "end": f3_end,
            "coverage": coverage, "u": u3, "http000": http000,
            "http000_share": (http000 / u3["offered"]) if u3["offered"] else None,
            "attributed": attributed,
            "attributed_completed": total_completed,
            "fivexx_fraction": (total_fivexx / total_completed)
            if total_completed else None,
        }

    # Admission-log + client-row timing chain (admitted -> first successful).
    admission = admission_rows(run_dir)
    admitted = [row for row in admission if row.get("result") == "admitted"]
    # Report-only spawn cross-check (service-log spawns vs admission-log
    # dynamic spawn rows); a mismatch is surfaced as a warning field and is
    # never gated (plan §4.4/§5 have no spawn-count gate).
    spawns_found = len(spawns)
    spawns_admission_rows = sum(
        1 for row in admission
        if SOUNDNESS_DYNAMIC_RE.match(str(row.get("container") or "")))
    spawns_mismatch = spawns_found != spawns_admission_rows
    sources: dict[str, int] = {}
    for row in admitted:
        key = str(row.get("admit_source") or "")
        sources[key] = sources.get(key, 0) + 1
    by_container = {spawn["container"]: spawn for spawn in spawns}
    first_success: dict[str, float] = {}
    pre_bind_successes = 0
    for row in rows:
        if not (is_completed(row) and is_success(row)):
            continue
        backend = str(row.get("backend_id") or "")
        spawn = by_container.get(backend)
        if spawn is None:
            continue
        if spawn["t_bind"] is not None and row["_sent"] < spawn["t_bind"]:
            pre_bind_successes += 1
        if backend not in first_success or row["_sent"] < first_success[backend]:
            first_success[backend] = row["_sent"]
    per_backend = []
    claim_values: list[float] = []
    admit_values: list[float] = []
    for row in admitted:
        container = str(row.get("container") or "")
        spawn = by_container.get(container)
        t_bind = spawn["t_bind"] if spawn else None
        if spawn is not None and spawn["t_claim"] is not None:
            t_claim = spawn["t_claim"]
        elif t_bind is not None and n > 0:
            t_claim = t_bind - n  # §4.3: t_claim = t_bind - N by construction
        else:
            t_claim = None
        admitted_ts = row.get("_admitted_ts")
        first = first_success.get(container)
        claim_s = ((first - t_claim) if first is not None and t_claim is not None
                   else None)
        admit_s = ((first - admitted_ts)
                   if first is not None and admitted_ts is not None else None)
        if claim_s is not None:
            claim_values.append(claim_s)
        if admit_s is not None:
            admit_values.append(admit_s)
        per_backend.append({
            "container": container, "lan": row.get("lan"),
            "admitted_ts": admitted_ts, "t_bind": t_bind, "t_claim": t_claim,
            "first_success_sent_s": first,
            "claim_to_first_s": claim_s, "admit_to_first_s": admit_s,
        })
    adm_flow = {
        "per_backend": per_backend,
        "claim_to_first_median_s": statistics.median(claim_values)
        if claim_values else None,
        "admit_to_first_median_s": statistics.median(admit_values)
        if admit_values else None,
        "n_used": len(claim_values),
    }

    run_wide = soundness_u(rows)
    # R1(b)/R2(a): §4.1 class 000, never the raw field (timeouts encode 000).
    http000 = sum(1 for row in rows if soundness_class_of(row) == "000")
    total_rows = len(rows)
    cancel_rate = ((run_wide["cancelled"] + run_wide["dropped"]) / total_rows
                   if total_rows else None)
    quota = qoe_quota(run_dir)
    restarts = len(restart_markers(run_dir))
    provenance = {
        "phases_snapshot": (run_dir / "phases_snapshot.json").exists(),
        "controller_env_snapshot": env_path.exists(),
        "fault_attestation": attestation["present"],
        "open_loop_schedule": (run_dir / "open_loop_schedule.json").exists(),
    }
    validity = {
        "provenance_ok": all(provenance.values()),
        "driver_clean": cancel_rate is not None and cancel_rate < SOUNDNESS_CANCEL_MAX,
        "quota_ok": (quota["present"] and quota["all_compute_containers_match"]
                     and str(quota["requested_edge_cpus"]) == "0.12"),
        "no_restart": restarts == 0,
    }
    floors = {
        "plateau_pooled_ge_5000": plateau_u["offered"] >= SOUNDNESS_PLATEAU_MIN_POOLED,
        "plateau_per_lan_ge_1000": all(
            plateau_per_lan[key]["offered"] >= SOUNDNESS_PLATEAU_MIN_PER_LAN
            for key in ("lan1", "lan2")),
        "f2_window_min_per_lan_ge_15": (
            all(value is not None and value >= SOUNDNESS_F2_WINDOW_MIN_PER_LAN
                for value in f2_floor_by_n[str(n)].values())
            if spawns and str(n) in f2_floor_by_n else None),
    }
    none_health = None
    if cell == "none":
        tail_breach = (tail_slow_share is None
                       or tail_slow_share > SOUNDNESS_NONE_TAIL_SLOW_MAX)
        bad_breach = (bad_share is None or bad_share > SOUNDNESS_NONE_BAD_MAX)
        none_health = {
            "tail_slow_share": tail_slow_share, "bad_share": bad_share,
            "tail_slow_breach": tail_breach, "bad_breach": bad_breach,
            "breach": tail_breach or bad_breach,
        }
    return {
        "run": run_dir.name, "cell": cell, "arm": arm, "ordinal": ordinal,
        "fault": soundness_cell_fault(cell), "n": n, "n_source": n_source,
        "s_lie": s_lie, "s_source": s_source, "seed": run_seed(run_dir),
        "plateau": {
            "start": plateau_start, "end": plateau_end,
            "config_duration_s": (plateau_cfg or {}).get("duration_s"),
            "offered": plateau_u["offered"],
            "offered_per_lan": {key: value["offered"]
                                for key, value in plateau_per_lan.items()},
            "u_pct": plateau_u["u_pct"], "classes": plateau_u["classes"],
            "tail_slow_share": tail_slow_share, "bad_share": bad_share,
        },
        "spawns": spawns,
        "spawns_found": spawns_found,
        "spawns_admission_rows": spawns_admission_rows,
        "spawns_mismatch": spawns_mismatch,
        "missing_bind": [spawn["container"] for spawn in spawns
                         if spawn["t_bind"] is None],
        "t_bind": t_bind_min, "t_claim": t_claim_min,
        "deferrals_s": [spawn["deferral_s"] for spawn in spawns],
        "u": {
            "f1_pct": plateau_u["u_pct"],
            "f2_by_n": {key: value["u_pct"]
                        for key, value in f2_u_by_n.items()},
            "f3_pct": f3["u"]["u_pct"] if f3 else None,
            "run_wide_pct": run_wide["u_pct"],
        },
        "f1": {"start": plateau_start, "end": plateau_end, "u": plateau_u},
        "f2": {"u_by_n": f2_u_by_n, "pre_by_n": f2_pre_by_n,
               "window_floor_by_n": f2_floor_by_n},
        "f3": f3,
        "run_wide": {
            **run_wide, "http000": http000,
            "http000_share": (http000 / run_wide["offered"])
            if run_wide["offered"] else None,
            "cancel_rate": cancel_rate,
        },
        "check_violations": soundness_check_violations(run_dir),
        "adm_flow": adm_flow,
        "pre_bind_successes": pre_bind_successes,
        "admission_sources": sources, "admitted_total": len(admitted),
        "floors": floors, "none_health": none_health,
        "quota": quota, "provenance": provenance, "validity": validity,
        "valid": all(validity.values()),
        "attestation": attestation,
        "env_knobs": {
            "EDGE_READY_PREMATURE_S": env.get("EDGE_READY_PREMATURE_S"),
            "EDGE_READY_SEMANTIC_LIE_S": env.get("EDGE_READY_SEMANTIC_LIE_S"),
        },
    }


def _soundness_pair_none(record: dict[str, Any],
                         none_records: list[dict[str, Any]]
                         ) -> tuple[dict[str, Any] | None, str]:
    """Same-arm none partner: seed is the §4.2 pairing key; the label
    ordinal is only a fallback when the seed metadata is unavailable."""
    candidates = [other for other in none_records
                  if other.get("arm") == record.get("arm")]
    seed = record.get("seed")
    if seed is not None:
        same_seed = [other for other in candidates if other.get("seed") == seed]
        if len(same_seed) == 1:
            return same_seed[0], "seed"
        if len(same_seed) > 1:
            return None, "ambiguous_seed"
    ordinal = record.get("ordinal")
    same_ordinal = [other for other in candidates if other.get("ordinal") == ordinal]
    if len(same_ordinal) == 1:
        return same_ordinal[0], "ordinal"
    if not candidates:
        return None, "missing"
    return None, "unpaired"


def _soundness_pair_u(record: dict[str, Any], partner: dict[str, Any],
                      cell: str) -> tuple[float | None, float | None]:
    if soundness_cell_fault(cell) == "premature":
        key = str(record["n"])
        return (record["f2"]["u_by_n"].get(key, {}).get("u_pct"),
                partner["f2"]["u_by_n"].get(key, {}).get("u_pct"))
    if cell == "semantic":
        return record["u"]["f3_pct"], partner["u"]["f3_pct"]
    if cell == "loss_all":
        return record["u"]["f1_pct"], partner["u"]["f1_pct"]
    return None, None


def soundness_damage_command(args: argparse.Namespace) -> int:
    """A2 damage tables: U per window, D_s per seed, D-bar medians."""
    records: list[dict[str, Any]] = []
    for path in args.run_dir:
        run_dir = Path(path)
        try:
            records.append(soundness_run_metrics(run_dir))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            label = soundness_label_parts(run_dir.name)
            records.append({
                "run": run_dir.name, "error": str(exc),
                "cell": label[0] if label else None,
                "arm": label[1] if label else None,
                "ordinal": label[2] if label else None,
            })
    label_ok = [record for record in records if "error" not in record]
    none_records = [record for record in label_ok if record["cell"] == "none"]
    contrasts: list[dict[str, Any]] = []
    for record in label_ok:
        cell = record["cell"]
        if cell == "none":
            continue
        partner, pairing = _soundness_pair_none(record, none_records)
        entry: dict[str, Any] = {
            "run": record["run"], "arm": record["arm"], "cell": cell,
            "seed": record["seed"], "n": record["n"], "pairing": pairing,
            "u_cell": None, "u_none": None, "d_s": None, "status": "",
        }
        if partner is None:
            entry["status"] = ("missing_none" if pairing == "missing"
                               else f"unpaired_none_{pairing}")
            contrasts.append(entry)
            continue
        entry["none_run"] = partner["run"]
        if partner.get("none_health") and partner["none_health"]["breach"]:
            entry["status"] = "none_breach"
            entry["none_breach_reason"] = {
                key: partner["none_health"][key]
                for key in ("tail_slow_share", "bad_share")}
            contrasts.append(entry)
            continue
        u_cell, u_none = _soundness_pair_u(record, partner, cell)
        entry["u_cell"], entry["u_none"] = u_cell, u_none
        if u_cell is None or u_none is None:
            entry["status"] = "missing_u"
        else:
            entry["d_s"] = u_cell - u_none
            entry["status"] = "ok"
        contrasts.append(entry)

    medians: dict[str, Any] = {}
    for entry in contrasts:
        key = f"{entry['arm']}|{entry['cell']}"
        bucket = medians.setdefault(
            key, {"seeds_ok": {}, "seeds_breached": [], "seeds_missing": []})
        if entry["status"] == "ok":
            bucket["seeds_ok"][str(entry["seed"])] = entry["d_s"]
        elif entry["status"] == "none_breach":
            bucket["seeds_breached"].append(entry["seed"])
        else:
            bucket["seeds_missing"].append(entry["seed"])
    for bucket in medians.values():
        values = list(bucket["seeds_ok"].values())
        bucket["d_bar_pp"] = statistics.median(values) if values else None
        bucket["n_usable"] = len(values)

    report = {
        "family": "rq3_soundness", "command": "soundness-damage",
        "runs": records, "contrasts": contrasts, "medians": medians,
        "none_breaches": [
            {"run": record["run"], "arm": record["arm"], "seed": record["seed"],
             **record["none_health"]}
            for record in label_ok
            if record.get("none_health") and record["none_health"]["breach"]],
    }
    write_json(Path(args.out), report)
    print(json.dumps({
        "runs": len(records), "contrasts": len(contrasts),
        "medians": {key: bucket["d_bar_pp"] for key, bucket in medians.items()},
        "errors": [record["run"] for record in records if "error" in record],
    }, indent=2, default=str))
    return 0


def _soundness_gate_r1(run: dict[str, Any]) -> dict[str, Any]:
    pre = run["f2"]["pre_by_n"][str(run["n"])]
    measured = [value for value in run["deferrals_s"] if value is not None]
    gates = {
        "a_in_window_000_ge_10_per_lan": (pre["http000"]["lan1"] >= 10
                                          and pre["http000"]["lan2"] >= 10),
        "b_run_wide_000_ge_0.3pct_and_50": (
            run["run_wide"]["http000"] >= 50
            and run["run_wide"]["http000_share"] is not None
            and run["run_wide"]["http000_share"] >= 0.003),
        "c_median_admit_first_in_8_12": (
            run["adm_flow"]["admit_to_first_median_s"] is not None
            and 8.0 <= run["adm_flow"]["admit_to_first_median_s"] <= 12.0),
        "d_check_violation_ge_1": run["check_violations"]["total"] >= 1,
        "e_attestation_premature_s_matches": (
            run["attestation"]["present"]
            and run["attestation"]["knobs"].get("EDGE_READY_PREMATURE_S") == run["n"]
            and _soundness_int(run["env_knobs"].get("EDGE_READY_PREMATURE_S"))
            == run["n"]),
        "f_measured_deferral_in_9_11": (
            bool(measured) and len(measured) == len(run["deferrals_s"])
            and all(9.0 <= value <= 11.0 for value in measured)),
    }
    return {"gates": gates, "pass": all(gates.values())}


def _soundness_gate_r2(run: dict[str, Any]) -> dict[str, Any]:
    sources = {key for key in run["admission_sources"] if key}
    gates = {
        "a_000_le_1_and_le_0.2pct": (
            run["run_wide"]["http000"] <= 1
            and run["run_wide"]["http000_share"] is not None
            and run["run_wide"]["http000_share"] <= 0.002),
        "b_zero_pre_bind_successes": run["pre_bind_successes"] == 0,
        "c_zero_check_violations": run["check_violations"]["total"] == 0,
        "d_median_claim_first_in_10_22": (
            run["adm_flow"]["claim_to_first_median_s"] is not None
            and 10.0 <= run["adm_flow"]["claim_to_first_median_s"] <= 22.0),
        "e_admission_source_probe": (run["admitted_total"] >= 1
                                     and sources == {"probe"}),
    }
    descriptive = {
        "admit_to_first_median_s": run["adm_flow"]["admit_to_first_median_s"],
        "admit_to_first_in_0_2": (
            run["adm_flow"]["admit_to_first_median_s"] is not None
            and 0.0 <= run["adm_flow"]["admit_to_first_median_s"] <= 2.0),
    }
    return {"gates": gates, "pass": all(gates.values()),
            "descriptive": descriptive}


def _soundness_gate_r3(run: dict[str, Any]) -> dict[str, Any]:
    f3 = run["f3"] or {}
    attributed = f3.get("attributed") or {}
    gates = {
        "a_lie_window_coverage_ge_90pct": (
            f3.get("coverage") is not None and f3["coverage"] >= 0.9),
        "b_attributed_5xx_ge_90pct_and_15_per_lan": (
            f3.get("fivexx_fraction") is not None and f3["fivexx_fraction"] >= 0.9
            and attributed.get("lan1", {}).get("completed", 0) >= 15
            and attributed.get("lan2", {}).get("completed", 0) >= 15),
        "c_transport_000_le_1pct_window": (
            f3.get("http000_share") is not None and f3["http000_share"] <= 0.01),
        "d_zero_check_violations": run["check_violations"]["total"] == 0,
    }
    return {"gates": gates, "pass": all(gates.values())}


def _soundness_r1_floors(run: dict[str, Any]) -> dict[str, Any]:
    """§4.5 measurement floors evaluated on the R1 calibration run.

    §4.4: "if the floors are unmet there, STOP and re-think the regime —
    never raise N". Raw computed values are reported alongside the checks.
    """
    floor_key = str(run.get("n"))
    window_floor = dict(
        (run.get("f2", {}).get("window_floor_by_n", {}) or {}).get(floor_key) or {})
    pooled = run["plateau"]["offered"]
    per_lan = dict(run["plateau"]["offered_per_lan"])
    cancel_rate = run["run_wide"].get("cancel_rate")
    checks = {
        "plateau_pooled_ge_5000": pooled >= SOUNDNESS_PLATEAU_MIN_POOLED,
        "plateau_per_lan_ge_1000": all(
            (per_lan.get(f"lan{lan}") or 0) >= SOUNDNESS_PLATEAU_MIN_PER_LAN
            for lan in (1, 2)),
        "f2_window_min_per_lan_ge_15": (
            bool(window_floor) and all(
                value is not None and value >= SOUNDNESS_F2_WINDOW_MIN_PER_LAN
                for value in window_floor.values())),
        "driver_cancel_rate_lt_5pct": (
            cancel_rate is not None and cancel_rate < SOUNDNESS_CANCEL_MAX),
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "pooled_offered": pooled,
        "per_lan_offered": per_lan,
        "f2_window_min_per_lan": window_floor,
        "cancel_rate": cancel_rate,
    }


def soundness_preflight_command(args: argparse.Namespace) -> int:
    """P1 go/no-go gates R1-R3b -> results JSON + lock JSON (exit 0/2/3).

    With ``--rerun-attempted`` (pre-registered rerun consumed), persisting
    DIAGNOSE-only reasons become STOP (exit 3) — exit 2 is reserved for a
    genuine (not yet re-run) DIAGNOSE verdict.
    """
    pairs = [(soundness_label_parts(Path(path).name), Path(path))
             for path in args.run_dir]
    unparsed = [str(path) for label, path in pairs if label is None]
    if unparsed:
        raise ValueError(f"cannot parse soundness labels for: {unparsed}")
    by_key: dict[tuple[str, str], Path] = {}
    for label, path in pairs:
        key = (label[0], label[1])
        if key in by_key:
            raise ValueError(f"duplicate run for cell/arm {key}: {path.name}")
        by_key[key] = path
    roles = {
        "R1": ("premature10", "event_only"),
        "R2": ("premature10", "reconcile"),
        "R3": ("semantic", "event_only"),
        "R3b": ("semantic", "reconcile"),
    }
    metrics: dict[str, dict[str, Any]] = {}
    for role, key in roles.items():
        path = by_key.get(key)
        if path is None:
            continue
        try:
            metrics[role] = soundness_run_metrics(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            metrics[role] = {"run": path.name, "error": str(exc)}

    def _attestation_ok(run: dict[str, Any], key: str, value: int) -> bool:
        return (run["attestation"]["present"]
                and run["attestation"]["knobs"].get(key) == value
                and _soundness_int(run["env_knobs"].get(key)) == value)

    results: dict[str, Any] = {}
    for role in ("R1", "R2", "R3", "R3b"):
        run = metrics.get(role)
        if run is None:
            results[role] = {"run": None, "evaluated": False,
                             "reason": "run not provided"}
            continue
        if "error" in run:
            results[role] = {"run": run["run"], "evaluated": False,
                             "reason": f"artifact error: {run['error']}"}
            continue
        if not run["valid"]:
            failed = [name for name, ok in run["validity"].items() if not ok]
            results[role] = {"run": run["run"], "evaluated": False,
                             "reason": "invalid run (base gates): "
                                       + ", ".join(failed)}
            continue
        if role in ("R1", "R2"):
            binds = [spawn for spawn in run["spawns"] if spawn["t_bind"] is not None]
            claims = [spawn for spawn in run["spawns"] if spawn["t_claim"] is not None]
            if (not binds or len(claims) != len(binds) or run["n"] != 10
                    or not _attestation_ok(run, "EDGE_READY_PREMATURE_S", 10)):
                results[role] = {"run": run["run"], "evaluated": False,
                                 "reason": "t_bind/t_claim/attestation extraction "
                                           "incomplete (P1 fail)"}
                continue
        if role in ("R3", "R3b"):
            if not any(spawn["t_bind"] is not None for spawn in run["spawns"]):
                results[role] = {"run": run["run"], "evaluated": False,
                                 "reason": "t_bind extraction failed (P1 fail)"}
                continue
            if not _attestation_ok(run, "EDGE_READY_SEMANTIC_LIE_S",
                                   SOUNDNESS_F3_S_DEFAULT):
                results[role] = {"run": run["run"], "evaluated": False,
                                 "reason": "semantic-lie attestation extraction "
                                           "incomplete (P1 fail)"}
                continue
        gate_function = {"R1": _soundness_gate_r1, "R2": _soundness_gate_r2,
                         "R3": _soundness_gate_r3, "R3b": _soundness_gate_r3}[role]
        evaluated = gate_function(run)
        results[role] = {
            "run": run["run"], "seed": run["seed"], "evaluated": True,
            **evaluated,
            "check_violations": run["check_violations"]["total"],
            "measured_deferrals_s": run["deferrals_s"],
            "f3_coverage": run["f3"]["coverage"] if run["f3"] else None,
            "spawns_found": run["spawns_found"],
            "spawns_admission_rows": run["spawns_admission_rows"],
            "spawns_mismatch": run["spawns_mismatch"],
        }

    # R1 §4.5 floors are computed whenever the record parsed (also for runs
    # later marked invalid) so the results JSON always carries the values.
    r1_metrics = metrics.get("R1")
    if r1_metrics is not None and "error" not in r1_metrics:
        results["R1"]["floors"] = _soundness_r1_floors(r1_metrics)

    def _failed(role: str) -> bool:
        item = results.get(role)
        return bool(item and item.get("evaluated") and not item.get("pass"))

    rerun_attempted = bool(getattr(args, "rerun_attempted", False))
    stop_reasons: list[str] = []
    diagnose_reasons: list[str] = []
    r1_floors = results.get("R1", {}).get("floors")
    if r1_floors is not None and not r1_floors["pass"]:
        stop_reasons.append(
            "R1 measurement floors unmet — calibrate regime; no N escalation")
    if _failed("R1"):
        stop_reasons.append(
            "R1 F2-reproduction gates failed (kill rule: re-design before spend)")
    if _failed("R2"):
        stop_reasons.append(
            "R2 F2-immunity gates failed (kill rule: structural claim broken)")
    r3 = results.get("R3")
    if r3 is not None and r3.get("evaluated"):
        gates = r3["gates"]
        if not gates["a_lie_window_coverage_ge_90pct"]:
            diagnose_reasons.append(
                "R3 lie-window coverage shortfall (late bind; DIAGNOSE rerun "
                "allowance, max 1)")
        elif not (gates["b_attributed_5xx_ge_90pct_and_15_per_lan"]
                  and gates["c_transport_000_le_1pct_window"]
                  and gates["d_zero_check_violations"]):
            stop_reasons.append(
                "R3 F3 signature gates failed (scope reduction; E blocked)")
    for role in ("R1", "R2", "R3"):
        item = results.get(role)
        if item is None or not item.get("evaluated"):
            diagnose_reasons.append(
                f"{role} not evaluable "
                f"({item.get('reason') if item else 'missing run'})")

    if rerun_attempted and diagnose_reasons:
        # The pre-registered same-config rerun has been consumed: a persisting
        # DIAGNOSE becomes a STOP (exit 3), never another exit-2 allowance.
        stop_reasons.extend(f"{reason} — persisting after permitted rerun"
                            for reason in diagnose_reasons)
        diagnose_reasons = []

    if stop_reasons:
        verdict = "stop"
    elif diagnose_reasons:
        verdict = "diagnose"
    else:
        verdict = "lock"
    lock_written = False
    if verdict == "lock":
        lock = {
            "family": "rq3_soundness", "stage": "P1", "locked": True,
            "semantic_lie_s": SOUNDNESS_F3_S_DEFAULT,
            "premature_lead_s": 10,
            "gate_results": {role: results[role].get("gates")
                             for role in results},
            "runs": {role: results[role].get("run") for role in results},
            "seeds": {role: results[role].get("seed") for role in results},
            "r3b_informative": results.get("R3b", {}).get("pass"),
        }
        write_json(Path(args.lock_out), lock)
        lock_written = True
    report = {
        "family": "rq3_soundness", "stage": "P1",
        "semantic_lie_s": SOUNDNESS_F3_S_DEFAULT, "premature_lead_s": 10,
        "verdict": verdict,
        "stop_reasons": stop_reasons, "diagnose_reasons": diagnose_reasons,
        "runs": results,
        "r3b_informative": results.get("R3b", {}).get("pass"),
        "lock_written": lock_written,
    }
    write_json(Path(args.out), report)
    print(json.dumps({
        "verdict": verdict, "stop_reasons": stop_reasons,
        "diagnose_reasons": diagnose_reasons, "lock_written": lock_written,
        "runs": {role: results[role].get("pass") for role in results},
    }, indent=2, default=str))
    return {"lock": 0, "diagnose": 2, "stop": 3}[verdict]


def _soundness_series(damage: dict[str, Any]
                      ) -> dict[tuple[str, str], dict[int, float]]:
    series: dict[tuple[str, str], dict[int, float]] = {}
    for entry in damage.get("contrasts", []):
        if entry.get("status") != "ok" or entry.get("d_s") is None:
            continue
        seed = _soundness_int(entry.get("seed"))
        if seed is None:
            continue
        series.setdefault((entry.get("arm"), entry.get("cell")), {})[seed] = \
            float(entry["d_s"])
    return series


def _soundness_ols(points: list[tuple[float, float]]
                   ) -> dict[str, float] | None:
    count = len(points)
    if count < 2:
        return None
    x_mean = sum(x for x, _ in points) / count
    y_mean = sum(y for _, y in points) / count
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    if denominator <= 0:
        return None
    alpha = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    return {"beta": y_mean - alpha * x_mean, "alpha": alpha, "points": count}


def _soundness_component(values: dict[Any, Any]) -> dict[str, Any]:
    """≥2/3-seed verdict with §4.5 NA handling (≥2 lost seeds → NA)."""
    lost = sum(1 for value in values.values() if value is None)
    hits = sum(1 for value in values.values() if value is True)
    if lost >= 2:
        verdict = "na"
    else:
        verdict = "pass" if hits >= 2 else "fail"
    return {"seeds": {str(key): value for key, value in values.items()},
            "lost": lost, "hits": hits, "verdict": verdict,
            "pass": verdict == "pass"}


def _soundness_run_index(damage: dict[str, Any]
                         ) -> dict[tuple[str, str], dict[int, dict[str, Any]]]:
    index: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for run in damage.get("runs", []):
        if run.get("error"):
            continue
        seed = _soundness_int(run.get("seed"))
        if seed is None:
            continue
        index.setdefault((run.get("arm"), run.get("cell")), {})[seed] = run
    return index


def soundness_crossover_command(args: argparse.Namespace) -> int:
    """§4.4 crossover fits + §6 H-S1..H-S4 verdict tables (damage JSON in)."""
    damage = json.loads(Path(args.damage_json).read_text(encoding="utf-8"))
    series = _soundness_series(damage)
    run_index = _soundness_run_index(damage)
    grid = list(SOUNDNESS_GRID)
    push_arm, pull_arm = "event_only", "reconcile"

    # ---- Pairwise crossover (primary push/pull, per seed) ----
    seeds = sorted({seed for cell in grid
                    for seed in series.get((push_arm, f"premature{cell}"), {})}
                   | {seed for cell in grid
                      for seed in series.get((pull_arm, f"premature{cell}"), {})})
    per_seed: dict[int, float] = {}
    lost_seeds: list[int] = []
    for seed in seeds:
        pairs: dict[int, tuple[float, float]] = {}
        for cell in grid:
            push = series.get((push_arm, f"premature{cell}"), {}).get(seed)
            pull = series.get((pull_arm, f"premature{cell}"), {}).get(seed)
            if push is None or pull is None:
                pairs = {}
                break
            pairs[cell] = (push, pull)
        if not pairs:
            lost_seeds.append(seed)
            continue
        crossing = next((cell for cell in grid
                         if pairs[cell][0] >= pairs[cell][1]), None)
        per_seed[seed] = float(crossing) if crossing is not None else float("inf")
    crossover_na = len(lost_seeds) >= 2 or len(per_seed) < 2
    censored = sum(1 for value in per_seed.values() if math.isinf(value))
    censored_fraction = (censored / len(per_seed)) if per_seed else None
    median_n_star = (
        statistics.median([SOUNDNESS_CENSORED_AS if math.isinf(value) else value
                           for value in per_seed.values()])
        if per_seed else None)
    if crossover_na:
        band = None
    elif (censored * 3 >= len(per_seed) * 2   # ≥2/3 censored (∞ on the grid)
          or (median_n_star is not None and median_n_star > 10.0)):
        # Exact-by-value mapping (§4.4): any median beyond N=10 — including a
        # censored seed pulling a two-seed median to 10.5 — is
        # push-advantage-through-N10, never `crossover`.
        band = "push-advantage-through-N10"
    elif median_n_star is not None and median_n_star <= 2:
        band = "pull-dominates-immediately"
    else:
        band = "crossover"
    crossover = {
        "push_arm": push_arm, "pull_arm": pull_arm,
        "n_star_per_seed": {str(seed): ("inf" if math.isinf(value) else value)
                            for seed, value in per_seed.items()},
        "lost_seeds": lost_seeds, "usable_seeds": sorted(per_seed),
        "n_star": median_n_star, "censored": censored,
        "censored_fraction": censored_fraction, "band": band,
        "na": crossover_na,
    }

    # ---- OLS D_bar(N) = beta + alpha*N per arm + seed-bootstrap 95 % CI ----
    def d_bar(arm: str, cell: str) -> float | None:
        values = list(series.get((arm, f"premature{cell}"), {}).values())
        return statistics.median(values) if values else None

    fits: dict[str, Any] = {}
    for arm in ("event_only", "hybrid", "reconcile"):
        points = [(float(cell), d_bar(arm, cell)) for cell in grid]
        fits[arm] = (_soundness_ols([(x, y) for x, y in points
                                     if y is not None])
                     if all(y is not None for _, y in points) else None)
    bootstrap_seeds = sorted({seed for arm in ("event_only", "hybrid", "reconcile")
                              for cell in grid
                              for seed in series.get((arm, f"premature{cell}"), {})})
    rng = random.Random(SOUNDNESS_BOOTSTRAP_SEED)
    alpha_boot: dict[str, list[float]] = {
        arm: [] for arm in ("event_only", "hybrid", "reconcile")}
    ratio_boot: list[float] = []
    if len(bootstrap_seeds) >= 2:
        for _ in range(SOUNDNESS_BOOTSTRAP_B):
            sample = [rng.choice(bootstrap_seeds) for _ in bootstrap_seeds]
            sample_fits: dict[str, Any] = {}
            for arm in ("event_only", "hybrid", "reconcile"):
                points = []
                for cell in grid:
                    values = [series.get((arm, f"premature{cell}"), {}).get(seed)
                              for seed in sample]
                    values = [value for value in values if value is not None]
                    points.append((float(cell),
                                   statistics.median(values) if values else None))
                sample_fits[arm] = (_soundness_ols(points)
                                    if all(y is not None for _, y in points)
                                    else None)
            for arm in ("event_only", "hybrid", "reconcile"):
                if sample_fits[arm] is not None:
                    alpha_boot[arm].append(sample_fits[arm]["alpha"])
            if sample_fits["event_only"] and sample_fits["reconcile"]:
                denominator = (sample_fits["event_only"]["alpha"]
                               - sample_fits["reconcile"]["alpha"])
                if denominator > 1e-12:
                    ratio_boot.append(
                        (sample_fits["reconcile"]["beta"]
                         - sample_fits["event_only"]["beta"]) / denominator)

    def ci(values: list[float]) -> list[float | None] | None:
        if not values:
            return None
        return [percentile(values, 0.025), percentile(values, 0.975)]

    n_star_fit = None
    non_identifiable = True
    extrapolated = None
    if fits["event_only"] is not None and fits["reconcile"] is not None:
        denominator = fits["event_only"]["alpha"] - fits["reconcile"]["alpha"]
        if denominator > 1e-12:
            non_identifiable = False
            n_star_fit = ((fits["reconcile"]["beta"] - fits["event_only"]["beta"])
                          / denominator)
            extrapolated = not (2.0 <= n_star_fit <= 10.0)
    n_star_fit_ci = (ci(ratio_boot)
                     if ratio_boot and len(ratio_boot) >= 0.9 * SOUNDNESS_BOOTSTRAP_B
                     else None)
    fit_report = {
        arm: ({"beta": fits[arm]["beta"], "alpha": fits[arm]["alpha"],
               "ci_alpha": ci(alpha_boot[arm]), "points": fits[arm]["points"]}
              if fits[arm] is not None else None)
        for arm in ("event_only", "hybrid", "reconcile")
    }
    fit_report.update({
        "n_star_fit": n_star_fit, "n_star_fit_ci": n_star_fit_ci,
        "extrapolated": extrapolated, "non_identifiable": non_identifiable,
    })

    # ---- Growth gate + §4.4 decision precedence ----
    growth_alpha = fits["event_only"]["alpha"] if fits["event_only"] else None
    growth_ci = (fit_report["event_only"] or {}).get("ci_alpha")
    growth_pass = (growth_alpha is not None
                   and growth_alpha >= SOUNDNESS_GROWTH_MIN_PP_S
                   and growth_ci is not None and growth_ci[0] is not None
                   and growth_ci[0] > SOUNDNESS_GROWTH_CI_MIN_PP_S)
    eo_d_bars = [d_bar(push_arm, cell) for cell in grid]
    available = [value for value in eo_d_bars if value is not None]
    max_d_bar = max(available) if available else None
    if crossover_na:
        branch = "na"
    elif max_d_bar is not None and max_d_bar < 2.0:
        branch = "stop_null"
    elif not growth_pass and max_d_bar is not None and max_d_bar < 5.0:
        branch = "sub_threshold"
    elif not growth_pass:
        branch = "growth_gate_failure"
    else:
        branch = "reported"
    growth = {
        "alpha_push_pp_per_s": growth_alpha, "ci_alpha": growth_ci,
        "gate_pass": growth_pass,
        "thresholds": {"point_min_pp_per_s": SOUNDNESS_GROWTH_MIN_PP_S,
                       "ci_lower_min_pp_per_s": SOUNDNESS_GROWTH_CI_MIN_PP_S},
        "max_d_bar_push_pp": max_d_bar,
    }
    h_s3 = {"status": branch, "pass": branch == "reported",
            "crossover": crossover, "fit": fit_report, "growth": growth}

    # ---- H-S1: every existing rule fails a fault family ----
    def f2_counts_component(arm: str) -> dict[str, Any]:
        # Max lead (premature10) is the canonical F2 mechanism cell: it is
        # the P1-R1-calibrated lead and the H-S2 comparison point.
        values: dict[Any, Any] = {}
        for seed in SOUNDNESS_E_SEEDS:
            run = run_index.get((arm, "premature10"), {}).get(seed)
            if run is None or not run.get("f2"):
                values[seed] = None
                continue
            pre = run["f2"]["pre_by_n"].get("10", {})
            counts = pre.get("http000", {})
            values[seed] = (counts.get("lan1", 0) >= 10
                            and counts.get("lan2", 0) >= 10)
        return _soundness_component(values)

    def d_semantic_component(arm: str) -> dict[str, Any]:
        values: dict[Any, Any] = {}
        for seed in SOUNDNESS_E_SEEDS:
            value = series.get((arm, "semantic"), {}).get(seed)
            values[seed] = (value >= SOUNDNESS_SEMANTIC_MIN_PP
                            if value is not None else None)
        return _soundness_component(values)

    h1_components = {
        "event_only_f2_in_window_000": f2_counts_component("event_only"),
        "hybrid_f2_in_window_000": f2_counts_component("hybrid"),
        "event_only_semantic_ge_30pp": d_semantic_component("event_only"),
        "reconcile_semantic_ge_30pp": d_semantic_component("reconcile"),
        "f1_event_only_defeat": {"verdict": "pass", "pass": True,
                                 "note": "cited from Family 4 (no re-gating)"},
    }
    h_s1 = {"components": h1_components,
            "status": _combined_status(h1_components),
            "pass": all(item["verdict"] == "pass"
                        for item in h1_components.values())}

    # ---- H-S2: veridicality is not verification (reconcile) ----
    def reconcile_joint_values() -> dict[Any, Any]:
        """Per-seed JOINT pattern (§6 SC-6): the SAME seed must show
        D_s(F2) <= 2 pp AND D_s(F3) >= 30 pp AND zero CHECK VIOLATION in its
        F3 run; the marginals below are reported descriptively alongside."""
        values: dict[Any, Any] = {}
        for seed in SOUNDNESS_E_SEEDS:
            f2_value = series.get((pull_arm, "premature10"), {}).get(seed)
            semantic_value = series.get((pull_arm, "semantic"), {}).get(seed)
            run = run_index.get((pull_arm, "semantic"), {}).get(seed)
            if f2_value is None or semantic_value is None or run is None:
                values[seed] = None
                continue
            values[seed] = (f2_value <= SOUNDNESS_F2_MAX_PP
                            and semantic_value >= SOUNDNESS_SEMANTIC_MIN_PP
                            and run["check_violations"]["total"] == 0)
        return _soundness_component(values)

    def reconcile_f2_values() -> dict[Any, Any]:
        values: dict[Any, Any] = {}
        for seed in SOUNDNESS_E_SEEDS:
            value = series.get((pull_arm, "premature10"), {}).get(seed)
            values[seed] = (value <= SOUNDNESS_F2_MAX_PP
                            if value is not None else None)
        return _soundness_component(values)

    def reconcile_semantic_values() -> dict[Any, Any]:
        values: dict[Any, Any] = {}
        for seed in SOUNDNESS_E_SEEDS:
            value = series.get((pull_arm, "semantic"), {}).get(seed)
            values[seed] = (value >= SOUNDNESS_SEMANTIC_MIN_PP
                            if value is not None else None)
        return _soundness_component(values)

    def reconcile_violation_values() -> dict[Any, Any]:
        values: dict[Any, Any] = {}
        for seed in SOUNDNESS_E_SEEDS:
            run = run_index.get((pull_arm, "semantic"), {}).get(seed)
            values[seed] = (run["check_violations"]["total"] == 0
                            if run is not None else None)
        return _soundness_component(values)

    joint = reconcile_joint_values()
    gaps = []
    for seed in SOUNDNESS_E_SEEDS:
        f2_value = series.get((pull_arm, "premature10"), {}).get(seed)
        semantic_value = series.get((pull_arm, "semantic"), {}).get(seed)
        if f2_value is not None and semantic_value is not None:
            gaps.append(semantic_value - f2_value)
    gap_median = statistics.median(gaps) if gaps else None
    h2_components = {}
    h2_components[
        "reconcile_same_seed_joint_f2_le_2pp_semantic_ge_30pp_zero_violations"
    ] = joint
    h2_components["reconcile_f2_le_2pp"] = reconcile_f2_values()
    h2_components["reconcile_semantic_ge_30pp"] = reconcile_semantic_values()
    h2_components["zero_check_violations_f3"] = reconcile_violation_values()
    h_s2 = {
        "components": h2_components,
        "gap_median_pp": gap_median,
        "gap_ge_28pp": gap_median is not None and gap_median >= 28.0,
        "status": joint["verdict"],
        "pass": joint["pass"],
    }

    # ---- H-S4: optional wake_verify (excluded from primary verdicts) ----
    wake_cells = [(arm, cell) for arm, cell in series
                  if arm == "wake_verify" and cell in ("premature10", "semantic")]
    wake_cells += [(arm, cell) for arm, cell in run_index
                   if arm == "wake_verify" and cell in ("none", "premature10",
                                                        "semantic")]
    if not any(arm == "wake_verify" for arm, _ in wake_cells):
        h_s4 = {"status": "not_run", "pass": None,
                "note": "optional arm absent (absence is not a campaign failure)"}
    else:
        def wake_component(cell: str, predicate: Any) -> dict[str, Any]:
            values: dict[Any, Any] = {}
            for seed in SOUNDNESS_E_SEEDS:
                value = series.get(("wake_verify", cell), {}).get(seed)
                values[seed] = predicate(value) if value is not None else None
            return _soundness_component(values)

        cost_values: dict[Any, Any] = {}
        for seed in SOUNDNESS_E_SEEDS:
            wake_none = run_index.get(("wake_verify", "none"), {}).get(seed)
            event_none = run_index.get(("event_only", "none"), {}).get(seed)
            if wake_none is None or event_none is None:
                cost_values[seed] = None
                continue
            wake_u = wake_none.get("u", {}).get("f1_pct")
            event_u = event_none.get("u", {}).get("f1_pct")
            cost_values[seed] = ((wake_u - event_u) <= 2.0
                                 if wake_u is not None and event_u is not None
                                 else None)
        h4_components = {
            "wake_verify_f2_le_2pp": wake_component(
                "premature10", lambda value: value <= SOUNDNESS_F2_MAX_PP),
            "wake_verify_semantic_ge_30pp": wake_component(
                "semantic", lambda value: value >= SOUNDNESS_SEMANTIC_MIN_PP),
            "healthy_cell_cost_le_2pp": _soundness_component(cost_values),
        }
        h_s4 = {"components": h4_components,
                "status": _combined_status(h4_components),
                "pass": all(item["verdict"] == "pass"
                            for item in h4_components.values())}

    # ---- F1 anchor (descriptive, non-gated) ----
    loss_all_d_bar = d_bar("reconcile", "loss_all")
    lead10_d_bar = d_bar("reconcile", "premature10")
    f1_anchor = {
        "reconcile_loss_all_d_bar_pp": loss_all_d_bar,
        "reconcile_premature10_d_bar_pp": lead10_d_bar,
        "abs_diff_pp": (abs(loss_all_d_bar - lead10_d_bar)
                        if loss_all_d_bar is not None
                        and lead10_d_bar is not None else None),
        "within_10pp": (abs(loss_all_d_bar - lead10_d_bar) <= 10.0
                        if loss_all_d_bar is not None
                        and lead10_d_bar is not None else None),
    }

    report = {
        "family": "rq3_soundness", "command": "soundness-crossover",
        "damage_json": args.damage_json, "grid": grid,
        "d_bar_pp": {f"{arm}|{cell}": d_bar(arm, f"premature{cell}")
                     for arm in ("event_only", "hybrid", "reconcile")
                     for cell in grid},
        "crossover": crossover, "fit": fit_report, "growth": growth,
        "branch": branch,
        "H-S1": h_s1, "H-S2": h_s2, "H-S3": h_s3, "H-S4": h_s4,
        "f1_anchor": f1_anchor,
    }
    write_json(Path(args.out), report)
    print(json.dumps({
        "branch": branch, "band": band, "n_star": median_n_star,
        "censored": censored, "growth_gate_pass": growth_pass,
        "H-S1": h_s1["status"], "H-S2": h_s2["status"], "H-S3": h_s3["status"],
        "H-S4": h_s4["status"],
    }, indent=2, default=str))
    return 0


def _combined_status(components: dict[str, Any]) -> str:
    verdicts = [item["verdict"] for item in components.values()]
    if "fail" in verdicts:
        return "fail"
    if "na" in verdicts:
        return "na"
    return "pass"


# ---------------------------------------------------------------------------
# Family 6 (rq3_admission_modes): seven-block completion campaign.
#
# Binding contract: docs/operation/testing/experiment/v3/rq3_admission_modes/
# experiment_plan.md §7.1/§7.2/§7.3/§7.4 + §8 + run_matrix.md. The campaign
# completes the Family-4 cells to 7 seed-matched blocks (block 7, seed 5207),
# extends the premature 10 s-lead cells to blocks 4-7 (seeds 6304-6307) with
# matched no-fault partners added by the 2026-10-11 (b) amendment, and raises
# the intact-source supporting sets to seven runs (5 new runs). Findings
# restated from the recorded family rules keep the fixed pre-registered
# denominators; the added seventh block is reported per block under the same
# rules and never re-scopes an accept count (plan §7).
# ---------------------------------------------------------------------------
COMPLETION_BLOCKS = (1, 2, 3, 4, 5, 6, 7)
# The lost-message contrasts use blocks 1-7 with block 4 out on the
# relief-completion gate, leaving six assessable blocks.
COMPLETION_LOST_DENOM_BLOCKS = (1, 2, 3, 4, 5, 6, 7)
COMPLETION_LOST_RECORDED_EXCLUSION = 4  # seed 5204, pre-registered relief-completion gate
COMPLETION_LOST_SEEDS = {1: 5201, 2: 5202, 3: 5203, 4: 5204,
                         5: 5205, 6: 5206, 7: 5207}
COMPLETION_PREMATURE_SEEDS = {1: 6301, 2: 6302, 3: 6303, 4: 6304,
                              5: 6305, 6: 6306, 7: 6307}
COMPLETION_PREMATURE_RECORDED_BLOCKS = (1, 2, 3)  # keep their recorded attestations
COMPLETION_PREMATURE_NEW_MIN_BLOCK = 4            # blocks 4-7 are the 12 new runs
COMPLETION_PREMATURE_MAX_LOST_PAIRS = 1           # recorded soundness 4.5 rule
COMPLETION_ASSESSABLE_MIN_BLOCKS = 5              # NA below 5/7 assessable (plan §9)
COMPLETION_DENOM_MIN_HITS = 4                     # E-L1/E-L3 accept count x/6
COMPLETION_DENOM_MIN_HITS_5 = 5                   # E-L2/E-P3 accept count x/6 -> x/7
COMPLETION_RECORDED_FLOOR = {
    # Recorded H-T3 outcome for the intact set (plan §7.1 E-I4): 16 of the 18
    # recorded runs were clean; the two breaches are the single-window
    # transients in blocks 3 and 5, which remain recorded and are never
    # re-chased. The floor is a qualification of the recorded set, not a
    # construction that makes a green verdict impossible.
    "clean": 16, "recorded_runs": 18, "transient_blocks": (3, 5),
}
COMPLETION_FAST_BIND_S = 1.0
COMPLETION_SLOW_BIND_S = 5.0
COMPLETION_POLL_PERIOD_S = 10.0
COMPLETION_DISC15_POLL_S = 15.0
COMPLETION_INITIAL_WINDOW_S = 30.0  # v2 useful_initial_share window tail
COMPLETION_INTACT_RUNS_EXPECTED = 21  # 3 arms x 7 blocks (E-I4 denominator)
# Recorded anchors, restated for the report only (never re-derived here):
# v2 fixed-image handover quality (12 runs / 47 backends), v2 set statistics
# (E-S1/E-S2) and the disc15 quantization precedent (E-S3).
COMPLETION_HANDOVER_ANCHORS = {
    "campaign": "v2 fixed-image (12 runs / 47 backends)",
    "useful_share": 1.000,
    "http000_fast_fails": 0,
}
# E-I1 re-scope (2026-10-12 (h)): the gated intact leg is spawn -> first
# successful response; the ready -> admission leg is unmeasurable in this
# build (instrument degeneracy — it read ~0.000 s for all three arms across
# the intact set). Recorded same-regime anchor for the gated leg: 11.27 s
# (direct) vs 17.70 s (discovery), exact p = 0.0005; the earlier regime-note
# d = -0.837 belongs to the adjacent admit -> first-routed interval. Block-7
# runs keep their planned `_7` labels (a voided run is quarantined as
# `_7_void` and re-run under the planned label per the recorded §9 mechanic;
# the `_void` folders never match the label regex).
COMPLETION_INTACT_ANCHORS = {
    "leg": "spawn -> first successful response",
    "campaign": "recorded timing campaign (same regime)",
    "direct_median_s": 11.27, "discovery_median_s": 17.70, "mwu_p": 0.0005,
    "note": ("d = -0.837 (earlier regime note) belongs to the adjacent "
             "admit -> first-routed interval; the C1 stage screen read "
             "A 13.19 / B 13.15 / C 15.36 s on this leg (spawn -> first "
             "routed 74.58 / 73.97 / 81.13 s)"),
}
COMPLETION_SUPPORT_ANCHORS = {
    "camp": {"ready_to_admit_direct_s": 0.001, "ready_to_admit_discovery_s": 6.82,
             "cliffs_d": -1.000, "scope": "set-level 14 runs / 55 backends",
             # 2026-10-26: pooled over the 14 runs / 55 backends, i.e. the six
             # 2026-08-06 runs per mechanism plus the 2026-10-13 seventh. The
             # seventh postdates the socket-bind stall, so this interval carries
             # two platform regimes; the thesis discloses that in the caption.
             "spawn_to_first_pooled_direct_median_s": 11.07,
             "spawn_to_first_pooled_discovery_median_s": 17.43,
             "spawn_to_first_pooled_mwu_p": 0.0007,
             "spawn_to_first_pooled_cliffs_d": -0.534,
             # 2026-10-13 (l) E-S1 re-scope: the gated leg is spawn -> first
             # successful response (the registered ready -> admit leg is
             # unmeasurable in this build); C3 screen probe values.
             "spawn_to_first_success_direct_median_s": 1.93,
             "spawn_to_first_success_discovery_median_s": 6.16},
    "cell12": {"scale_to_first_success_direct_s": 2.13,
               "scale_to_first_success_discovery_s": 6.69,
               "mwu_p": 0.000583, "cliffs_d": -1.000, "gap_null": "7x7 all-zero",
               "window": 4096},
    "disc15": {"poll_s": COMPLETION_DISC15_POLL_S,
               "precedent_10s_s": 9.6, "precedent_15s_s": 15.2},
}
COMPLETION_SUPPORT_LABEL_RE = re.compile(
    r"(rq3_camp_(?:direct|disc)|rq3_probe_cell12_(?:direct|disc)|rq3_disc15)_(\d+)$")
# (h)/(l) instrument-degeneracy marker: the registered ready -> admit leg
# reads 0.000 s (identical `app_ready_ts`/`admitted_ts` values) for every
# backend of the completion build, so it is reported as a note, never gating.
COMPLETION_DEGENERATE_LEG_EPS_S = 1e-9
# 2026-10-13 (m) wake-and-verify un-parking: label families and anchors.
# The wake `none` cell carries the premature-cell partners at ordinals 1-7
# (seeds 6301-6307; 1-3 are the recorded pilots) and the intact/lost family
# at ordinals 8-14 (seeds 5201-5207, mirroring COMPLETION_LOST_SEEDS) — the
# snd launcher refuses duplicate labels, so the two seed families must not
# share ordinals. Loss/intact pairs are ordinal-matched (8..14).
COMPLETION_WAKE_ORDINALS = (8, 9, 10, 11, 12, 13, 14)
COMPLETION_WAKE_SEED_FAMILY = {8: 5201, 9: 5202, 10: 5203, 11: 5204,
                               12: 5205, 13: 5206, 14: 5207}
# Frozen code (`readiness_gate.py`): with `READINESS_EVENT_DROP_MODE=all` the
# wake probe never fires (`wake_verify_wall_s` is None) and the event-absence
# fallback admits with propagation=direct -> `admit_source="probe_fallback"`.
COMPLETION_WAKE_MECHANISM_SOURCE = "probe_fallback"
COMPLETION_WAKE_LOSS_TIMING_ANCHOR_S = 21.0   # recorded hybrid loss recovery
COMPLETION_WAKE_NEAR_TIE_S = 2.0              # descriptive wake-vs-event_only read


def completion_support_label_parts(run_name: str) -> tuple[str, str, str] | None:
    """('camp'|'cell12'|'disc15', 'direct'|'discovery', ordinal)."""
    match = COMPLETION_SUPPORT_LABEL_RE.search(run_name)
    if match is None:
        return None
    prefix, ordinal = match.group(1), match.group(2)
    sets = {"rq3_camp_direct": ("camp", "direct"),
            "rq3_camp_disc": ("camp", "discovery"),
            "rq3_probe_cell12_direct": ("cell12", "direct"),
            "rq3_probe_cell12_disc": ("cell12", "discovery"),
            "rq3_disc15": ("disc15", "discovery")}
    name, arm = sets[prefix]
    return name, arm, ordinal


def _completion_ordinal(ordinal: str) -> int:
    text = ordinal.rstrip("r")
    return int(text)


def _completion_edge_log_fields(run_dir: Path, container: str) -> dict[str, Any]:
    """Per-backend bind stall + true app-ready epoch from the edge log.

    Mirrors tools/rq3_stratified_analysis._parse_edge_log: the covariate is
    the `bind-timing make_server(bind+listen)=X s` line when present, else the
    start->listen gap on legacy runs; the true app-ready anchor is the
    `app ready: MongoDB ping OK` line (plan §7.1 start-up class).
    """
    path = run_dir / "service_logs" / f"{container}.log"
    out: dict[str, Any] = {"bind_s": None, "ready_epoch": None, "log_present": path.exists()}
    if not path.exists():
        return out
    start_t = listen_t = None
    bind_s = None
    ready = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ts = soundness_log_line_ts(line)
        if start_t is None and "Starting edge-server" in line and ts is not None:
            start_t = ts
        if bind_s is None and "bind-timing" in line:
            match = re.search(r"make_server\(bind\+listen\)=([0-9.]+)s", line)
            if match:
                bind_s = float(match.group(1))
        if listen_t is None and "edge-server listening" in line and ts is not None:
            listen_t = ts
        if ready is None and "app ready: MongoDB ping" in line and ts is not None:
            ready = ts
    if bind_s is not None:
        out["bind_s"] = bind_s
    elif start_t is not None and listen_t is not None:
        out["bind_s"] = listen_t - start_t
    out["ready_epoch"] = ready
    return out


def completion_bind_stratum(bind_s: float | None) -> str:
    """Start-up class (plan §8): fast <1.0 s, slow >=5.0 s; the [1, 5) band is
    recorded and included in the pooled "all" stratum only."""
    if bind_s is None:
        return "unknown"
    if bind_s < COMPLETION_FAST_BIND_S:
        return "fast"
    if bind_s >= COMPLETION_SLOW_BIND_S:
        return "slow"
    return "mid"


def _completion_first_flows(run_dir: Path, containers: set[str]) -> dict[str, dict[str, float]]:
    """First routed request and first successful response per admitted backend."""
    out: dict[str, dict[str, float]] = {container: {} for container in containers}
    for row in request_rows(run_dir):
        backend = str(row.get("backend_id") or "")
        if backend not in out:
            continue
        sent = row["_sent"]
        entry = out[backend]
        if "first_routed_s" not in entry or sent < entry["first_routed_s"]:
            entry["first_routed_s"] = sent
        if is_completed(row) and is_success(row):
            if "first_success_s" not in entry or sent < entry["first_success_s"]:
                entry["first_success_s"] = sent
    return out


def completion_backend_records(run_dir: Path) -> list[dict[str, Any]]:
    """Per-admitted-backend interval record for the intact/support reads.

    ready -> admit: admission-log `app_ready_ts` -> `admitted_ts` (plan §8),
    with the service-log true-ready anchor as the documented fallback; spawn ->
    first success and admit -> first routed request from client_requests.csv
    by `backend_id`; start-up class from the per-backend bind record.
    """
    records: list[dict[str, Any]] = []
    for row in admission_rows(run_dir):
        if row.get("result") != "admitted" or row.get("_admitted_ts") is None:
            continue
        container = str(row.get("container") or "")
        log = _completion_edge_log_fields(run_dir, container)
        app_ready = timestamp(row.get("app_ready_ts"))
        ready_source = "admission_log"
        if app_ready is None:
            app_ready = row.get("_true_ready")
            ready_source = "service_log"
        admitted = row.get("_admitted_ts")
        spawn = timestamp(row.get("spawn_started_ts"))
        ready_to_admit = (admitted - app_ready
                          if admitted is not None and app_ready is not None else None)
        flows: dict[str, float] = {}
        record = {
            "run": run_dir.name, "container": container, "lan": row.get("lan"),
            "admit_source": str(row.get("admit_source") or ""),
            "bind_s": log["bind_s"],
            "stratum": completion_bind_stratum(log["bind_s"]),
            "ready_source": ready_source,
            "ready_to_admit_s": ready_to_admit,
            "spawn_to_admitted_s": (admitted - spawn
                                    if admitted is not None and spawn is not None else None),
            "spawn_started_ts": spawn, "admitted_ts": admitted,
            "_flows": flows,
        }
        records.append(record)
    by_container = {record["container"]: record for record in records}
    flows = _completion_first_flows(run_dir, set(by_container))
    for record in records:
        entry = flows.get(record["container"], {})
        record.update({
            "admitted_to_first_routed_s": (
                entry["first_routed_s"] - record["admitted_ts"]
                if "first_routed_s" in entry and record["admitted_ts"] is not None
                else None),
            "spawn_to_first_success_s": (
                entry["first_success_s"] - record["spawn_started_ts"]
                if "first_success_s" in entry and record["spawn_started_ts"] is not None
                else None),
            "spawn_to_first_routed_s": (
                entry["first_routed_s"] - record["spawn_started_ts"]
                if "first_routed_s" in entry and record["spawn_started_ts"] is not None
                else None),
            "first_routed_sent_s": entry.get("first_routed_s"),
            "first_success_sent_s": entry.get("first_success_s"),
        })
        record.pop("_flows", None)
    return records


def _completion_exact_mwu(x: list[float], y: list[float]) -> float:
    """Exact two-sided MWU p (exhaustive permutation) with the campaign's
    tie-corrected normal-approximation fallback above ~1e6 partitions.

    The module-level ``exact_mwu`` enumerates every partition unconditionally,
    which is only feasible for the small E/P-stage delta lists; the E-I1
    pooled-backend contrast (dozens of values) uses this guarded variant,
    mirroring tools/rq3_stratified_analysis.py (same U statistic, standard
    asymptotically-exact fallback).
    """
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        return float("nan")
    allv = x + y
    order = sorted(range(len(allv)), key=lambda index: allv[index])
    ranked = [0.0] * len(allv)
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[order[j + 1]] == allv[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranked[order[k]] = avg
        i = j + 1

    def u_for(selection) -> float:
        return sum(ranked[k] for k in selection) - n1 * (n1 + 1) / 2

    observed = u_for(range(n1))
    total_n = n1 + n2
    if math.comb(total_n, n1) <= 1_000_000:
        total = extreme = 0
        for selection in itertools.combinations(range(total_n), n1):
            total += 1
            if abs(u_for(selection) - n1 * n2 / 2) >= abs(observed - n1 * n2 / 2):
                extreme += 1
        return extreme / total
    mean = n1 * n2 / 2.0
    ties: dict[float, int] = {}
    for value in allv:
        ties[value] = ties.get(value, 0) + 1
    tie_corr = sum(count ** 3 - count for count in ties.values())
    var = (n1 * n2 / 12.0) * ((total_n + 1) - tie_corr / (total_n * (total_n - 1)))
    if var <= 0:
        return float("nan")
    z = (abs(observed - mean) - 0.5) / math.sqrt(var)  # continuity correction
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _completion_contrast(xs: list[float], ys: list[float]) -> dict[str, Any]:
    """Exact MWU + Cliff's delta for x-before-y direction reads."""
    return {
        "n_x": len(xs), "n_y": len(ys),
        "median_x": statistics.median(xs) if xs else None,
        "median_y": statistics.median(ys) if ys else None,
        "mwu_p": _completion_exact_mwu(xs, ys) if xs and ys else None,
        "cliffs_d": cliffs(xs, ys) if xs and ys else None,
        "x_before_y_every": (cliffs(xs, ys) == -1.0) if xs and ys else None,
    }


def _completion_health_floor(paths: list[Path],
                             expected: int = COMPLETION_INTACT_RUNS_EXPECTED
                             ) -> dict[str, Any]:
    """E-I4/E-L4: the recorded H-T3 run-level floor (bad <=1 % full plateau AND
    plateau-tail slow <=2 pp) over the explicit 21-run denominator (3 arms x
    7 blocks).

    The floor is reported exactly as recorded (16/18 clean;
    ``none_cell_healthy=false`` where it applies) as a qualification of the
    recorded set: the recorded blocks 3 and 5 transients stay recorded and are
    never re-chased. The campaign conformance read is
    ``recorded_qualification.conforms`` = the recorded 18-run outcome
    reproduces (16 clean, breaches only in blocks 3/5) AND the fresh block-7
    runs pass the same per-run gate; a fresh breach outside the recorded
    transients (or on block 7) fails it. The floor never re-scopes an accept
    count to the surviving subset.
    """
    per_run: list[dict[str, Any]] = []
    clean = 0
    for path in paths:
        label = timing_label_parts(path.name)
        block = int(label[2]) if label is not None else None
        metrics = qoe_run_metrics(path)
        bad = _pooled_bad(metrics)
        tail = timing_tail_slow(path)
        ok = (bad is not None and bad <= CONTROL_BAD_MAX
              and tail is not None and tail <= SLOW_CONTROL_MAX)
        if ok:
            clean += 1
        per_run.append({
            "run": path.name, "block": block,
            "bad_rate_full_plateau": bad, "tail_slow": tail,
            "floor_ok": bool(ok),
            "recorded_transient": block in COMPLETION_RECORDED_FLOOR["transient_blocks"],
        })
    recorded_runs = [entry for entry in per_run
                     if entry["block"] in (1, 2, 3, 4, 5, 6)]
    new_runs = [entry for entry in per_run if entry["block"] == 7]
    recorded_clean = sum(1 for entry in recorded_runs if entry["floor_ok"])
    breaching_blocks = sorted({entry["block"] for entry in recorded_runs
                               if not entry["floor_ok"]})
    reproduced = bool(
        len(recorded_runs) == COMPLETION_RECORDED_FLOOR["recorded_runs"]
        and recorded_clean == COMPLETION_RECORDED_FLOOR["clean"]
        and tuple(breaching_blocks)
        == tuple(COMPLETION_RECORDED_FLOOR["transient_blocks"]))
    new_clean = all(entry["floor_ok"] for entry in new_runs) if new_runs else None
    conforms = None
    if recorded_runs and new_runs:
        conforms = bool(reproduced and new_clean)
    elif not recorded_runs and new_runs:
        # Only the fresh block-7 runs supplied: the recorded outcome cannot be
        # re-verified here, so the qualification read is NA (never a fail).
        conforms = None
    return {
        "denominator": expected, "provided": len(paths), "clean": clean,
        "none_cell_healthy": bool(len(paths) == expected and clean == expected),
        "per_run": per_run,
        "recorded_floor": dict(COMPLETION_RECORDED_FLOOR),
        "recorded_qualification": {
            "recorded_runs": len(recorded_runs),
            "recorded_clean": recorded_clean,
            "breaching_blocks": breaching_blocks,
            "reproduced": reproduced,
            "block7_clean": new_clean,
            "conforms": conforms,
            "na": conforms is None,
        },
        "note": ("recorded H-T3 run-level floor reported exactly as recorded "
                 "(16/18, none_cell_healthy=false where it applies; blocks 3 "
                 "and 5 transients stay recorded, never re-chased); conformance "
                 "requires the recorded outcome reproduced and block 7 clean "
                 "(plan §7.1 E-I4 / §7.2 E-L4, §9 NA != stop)"),
    }


def _completion_intact_inputs(args: argparse.Namespace
                              ) -> tuple[dict[tuple[int, str], Path],
                                         dict[int, Path], list[str]]:
    """Split the completion-intact input set (plan §7.1 + §7.5, 2026-10-13 (m)).

    Accepted: the `rq3tim_none_*` timing runs (as before) and the optional
    wake-and-verify intact runs `rq3snd_none_wake_verify_8..14` — the wake
    `none` cell's 8-14 ordinals are the 5201-5207 seed family (its 1-7
    ordinals are the premature-cell partners and are refused here). Any other
    input is a problem (STOP)."""
    runs: dict[tuple[int, str], Path] = {}
    wake: dict[int, Path] = {}
    problems: list[str] = []
    for path_text in args.run_dir:
        path = Path(path_text)
        parts = timing_label_parts(path.name)
        if parts is not None:
            label_cell, arm, block = parts
            if label_cell != "none":
                problems.append(f"wrong cell for none read: {path.name}")
                continue
            key = (int(block), arm)
            if key in runs:
                problems.append(f"duplicate run for block/arm {key}: {path.name}")
                continue
            runs[key] = path
            continue
        wake_parts = soundness_label_parts(path.name)
        if wake_parts is None:
            problems.append(f"cannot parse label: {path.name}")
            continue
        cell, arm, ordinal = wake_parts
        if arm != "wake_verify" or cell != "none":
            problems.append(
                f"non-timing input is not a wake_verify none run: {path.name}")
            continue
        try:
            block = _completion_ordinal(ordinal)
        except ValueError:
            problems.append(f"cannot parse block ordinal: {path.name}")
            continue
        if block not in COMPLETION_WAKE_ORDINALS:
            problems.append(
                f"wake_verify none ordinal {block} is not an intact input "
                f"(expected {COMPLETION_WAKE_ORDINALS[0]}.."
                f"{COMPLETION_WAKE_ORDINALS[-1]}; ordinals 1-7 are the "
                f"premature-cell partners): {path.name}")
            continue
        if block in wake:
            problems.append(
                f"duplicate wake_verify none ordinal {block}: {path.name}")
            continue
        wake[block] = path
    return runs, wake, problems


def _completion_wake_loss_read(run_dir: Path) -> dict[str, Any]:
    """One-pass E-W2 read (plan §7.5): plateau slow-class share over offered
    and the admission-source mix (`probe_fallback` is the expected wake-under-
    loss mechanism, frozen `readiness_gate.py`)."""
    rows = request_rows(run_dir)
    bounds = phase_bounds(run_dir)
    slow_share = None
    window = bounds.get(PHASE)
    if window is not None:
        lo, hi = window
        plateau = [row for row in rows
                   if row.get("phase") == PHASE and lo <= row["_sent"] <= hi]
        unit = soundness_u(plateau)
        if unit["offered"]:
            slow_share = unit["classes"]["slow"] / unit["offered"]
    sources: dict[str, int] = {}
    for row in admission_rows(run_dir):
        key = str(row.get("admit_source") or "")
        sources[key] = sources.get(key, 0) + 1
    return {"slow_share": slow_share, "sources": sources, "rows": len(rows)}


def _completion_wake_mechanism_ok(sources: dict[str, int]) -> bool:
    keys = {key for key, count in sources.items() if key and count}
    return keys == {COMPLETION_WAKE_MECHANISM_SOURCE}


def _completion_timing_runs(args: argparse.Namespace, cell: str
                            ) -> tuple[dict[tuple[int, str], Path], list[str]]:
    runs: dict[tuple[int, str], Path] = {}
    problems: list[str] = []
    for path_text in args.run_dir:
        path = Path(path_text)
        parts = timing_label_parts(path.name)
        if parts is None:
            problems.append(f"cannot parse timing label: {path.name}")
            continue
        label_cell, arm, block = parts
        if label_cell != cell:
            problems.append(f"wrong cell for {cell} read: {path.name}")
            continue
        key = (int(block), arm)
        if key in runs:
            problems.append(f"duplicate run for block/arm {key}: {path.name}")
            continue
        runs[key] = path
    return runs, problems


def _completion_intact_blocks(runs: dict[tuple[int, str], Path]
                              ) -> dict[int, dict[str, Path]]:
    blocks: dict[int, dict[str, Path]] = {}
    for (block, arm), path in runs.items():
        blocks.setdefault(block, {})[arm] = path
    return blocks


def completion_intact_command(args: argparse.Namespace) -> int:
    """E-I1..E-I4: the completed seven-block intact cells (plan §7.1).

    E-I1 gates the **spawn -> first successful response** leg, per start-up
    class (2026-10-12 (h) re-scope: the ready -> admission leg is unmeasurable
    in this build — instrument degeneracy — and is reported as a note only).
    Block-7 runs keep their planned `_7` labels; a voided run is quarantined
    as `_7_void` (never matched by the set-selection regex) per the recorded
    §9 mechanic.

    Exit codes (documented in run_matrix.md and plan §11): 0 = every gated
    component passes (E-I1 fast/slow strata direction; E-I4 recorded-floor
    conformance) with no NA; 2 = a gated component failed or is NA (a report
    signal — NA != stop); 3 = analyzer/argument error (STOP). E-I2/E-I3 are
    descriptive and never gate. The E-I4 floor is reported exactly as recorded
    (16/18, none_cell_healthy=false where it applies) as a qualification of
    the recorded set; the between-arm contrast (E-I1) is untouched by it.
    """
    runs, wake_none, problems = _completion_intact_inputs(args)
    if problems:
        raise ValueError("; ".join(problems))
    blocks = _completion_intact_blocks(runs)
    arms = ("event_only", "hybrid", "reconcile")

    backend_records: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for (block, arm), path in runs.items():
        backend_records[(block, arm)] = completion_backend_records(path)

    # E-I1 -- per-backend spawn -> first successful response (2026-10-12 (h)
    # re-scope), pooled over the assessable blocks, contrasted within start-up
    # class (A vs C primary, B vs C secondary; exact MWU + Cliff's delta;
    # medians reported). The ready -> admission leg is unmeasurable in this
    # build and is reported as an informational note below.
    assessable_blocks = [block for block in sorted(blocks)
                         if all(arm in blocks[block] for arm in arms)
                         and all(backend_records.get((block, arm)) for arm in arms)]
    stratum_names = ("all", "fast", "slow")
    # Primary gating strata per plan §7.1: fast-binding vs slow-binding only;
    # the pooled "all" stratum (which includes the recorded [1, 5) s mid band)
    # is descriptive and never gates.
    primary_strata = ("fast", "slow")
    e_i1: dict[str, Any] = {
        "assessable_blocks": assessable_blocks,
        "strata": {}, "primary_strata": list(primary_strata),
        "primary_strata_evidence": 0,
        "primary_strata_ok": 0, "secondary_strata_ok": 0,
    }
    pooled: dict[str, dict[str, list[float]]] = {
        name: {arm: [] for arm in arms} for name in stratum_names}
    pooled_ready_to_admit: dict[str, dict[str, list[float]]] = {
        name: {arm: [] for arm in arms} for name in stratum_names}
    for block in assessable_blocks:
        for arm in arms:
            for record in backend_records[(block, arm)]:
                value = record["spawn_to_first_success_s"]
                ready_value = record["ready_to_admit_s"]
                stratum = record["stratum"]
                if value is not None:
                    pooled["all"][arm].append(value)
                    if stratum in ("fast", "slow"):
                        pooled[stratum][arm].append(value)
                if ready_value is not None:
                    pooled_ready_to_admit["all"][arm].append(ready_value)
                    if stratum in ("fast", "slow"):
                        pooled_ready_to_admit[stratum][arm].append(ready_value)
    source_mix = {arm: {} for arm in arms}
    for arm in arms:
        counts: dict[str, int] = {}
        for (block, run_arm), path in runs.items():
            if run_arm != arm:
                continue
            for record in backend_records[(block, arm)]:
                counts[record["admit_source"]] = counts.get(record["admit_source"], 0) + 1
        total = sum(counts.values())
        source_mix[arm] = {
            "sources": counts, "total": total,
            "event_fraction": (counts.get("event", 0) / total) if total else None,
        }
    level_flags = {"c_later_by_one_period": None,
                   "a_median_lt_c": None, "b_median_lt_c": None}
    for name in stratum_names:
        entry: dict[str, Any] = {}
        for arm in arms:
            values = pooled[name][arm]
            entry[f"n_{arm}"] = len(values)
            entry[f"median_{arm}"] = statistics.median(values) if values else None
        entry["primary_a_vs_c"] = _completion_contrast(
            pooled[name]["event_only"], pooled[name]["reconcile"])
        entry["secondary_b_vs_c"] = _completion_contrast(
            pooled[name]["hybrid"], pooled[name]["reconcile"])
        median_a = entry["median_event_only"]
        median_b = entry["median_hybrid"]
        median_c = entry["median_reconcile"]
        entry["direction_a_before_c"] = bool(
            median_a is not None and median_c is not None and median_a < median_c)
        entry["direction_b_before_c"] = bool(
            median_b is not None and median_c is not None and median_b < median_c)
        entry["c_delta_over_ab_s"] = (
            median_c - max(median_a, median_b)
            if median_a is not None and median_b is not None
            and median_c is not None else None)
        usable = (entry["primary_a_vs_c"]["n_x"] > 0
                  and entry["primary_a_vs_c"]["n_y"] > 0)
        if name in primary_strata:
            if usable:
                e_i1["primary_strata_evidence"] += 1
                if entry["direction_a_before_c"]:
                    e_i1["primary_strata_ok"] += 1
                if (entry["secondary_b_vs_c"]["n_x"] > 0
                        and entry["direction_b_before_c"]):
                    e_i1["secondary_strata_ok"] += 1
        else:
            entry["descriptive"] = True
        e_i1["strata"][name] = entry
    all_entry = e_i1["strata"]["all"]
    # C-band adapted to the gated leg: C's median later than A/B by
    # approximately one check period — the recorded-valid band is (0, 10 s]
    # (recorded same-regime anchor delta 6.43 s; C1 screen delta ≈2.2 s).
    delta_all = all_entry["c_delta_over_ab_s"]
    level_flags["c_later_by_one_period"] = bool(
        delta_all is not None and 0.0 < delta_all <= COMPLETION_POLL_PERIOD_S)
    level_flags["a_median_lt_c"] = all_entry["direction_a_before_c"]
    level_flags["b_median_lt_c"] = all_entry["direction_b_before_c"]
    ready_all = pooled_ready_to_admit["all"]
    e_i1.update({
        "leg": "spawn -> first successful response",
        "anchors": COMPLETION_INTACT_ANCHORS,
        "source_mix": source_mix,
        "b_event_fraction_ge_80pct": (
            source_mix["hybrid"]["event_fraction"] is not None
            and source_mix["hybrid"]["event_fraction"] >= 0.80),
        "level_flags": level_flags,
        "ready_to_admit_leg": {
            "leg": "app_ready_ts -> admitted_ts",
            "status": "unmeasurable in this build (instrument degeneracy)",
            "medians_s": {arm: (statistics.median(ready_all[arm])
                                if ready_all[arm] else None)
                          for arm in arms},
            "values_s": {arm: sorted(ready_all[arm]) for arm in arms},
            "diagnostic": ("reads ~0.000 s for all three arms across the "
                           "intact set; the service-log trace shows C's "
                           "≈2.9 s quantization instead — reported as a note, "
                           "never gating (plan §7.1, 2026-10-12 (h))"),
        },
        "assessable_ok": len(assessable_blocks) >= COMPLETION_ASSESSABLE_MIN_BLOCKS,
    })
    e_i1["pass"] = bool(
        e_i1["assessable_ok"] and e_i1["primary_strata_evidence"] > 0
        and e_i1["primary_strata_ok"] == e_i1["primary_strata_evidence"]
        and e_i1["secondary_strata_ok"] == e_i1["primary_strata_evidence"])

    # E-I2 -- handover quality (descriptive, no gate): admit -> first routed
    # request, spawn -> first success, the v2 initial-mix share and zero
    # unattributed 000 fast-fails.
    e_i2: dict[str, Any] = {"anchors": COMPLETION_HANDOVER_ANCHORS, "modes": {}}
    for arm in arms:
        admit_to_first: list[float] = []
        spawn_to_first: list[float] = []
        unattributed_000 = 0
        total_000 = 0
        for (block, run_arm), path in runs.items():
            if run_arm != arm:
                continue
            records = backend_records[(block, arm)]
            windows: list[tuple[float, float]] = []
            for record in records:
                if record["admitted_to_first_routed_s"] is not None:
                    admit_to_first.append(record["admitted_to_first_routed_s"])
                if record["spawn_to_first_success_s"] is not None:
                    spawn_to_first.append(record["spawn_to_first_success_s"])
                if record["spawn_started_ts"] is not None \
                        and record["admitted_ts"] is not None:
                    windows.append((record["spawn_started_ts"],
                                    record["admitted_ts"] + COMPLETION_INITIAL_WINDOW_S))
            offered = successes = 0
            for row in request_rows(path):
                classification = soundness_class_of(row)
                if classification == "000":
                    total_000 += 1
                    backend = str(row.get("backend_id") or "")
                    if backend in ("", "unknown"):
                        unattributed_000 += 1
                if classification in ("canceled", "dropped"):
                    continue
                if any(lo <= row["_sent"] < hi for lo, hi in windows):
                    offered += 1
                    if classification in ("good", "slow"):
                        successes += 1
            e_i2["modes"].setdefault(arm, {"initial_windows": []})
            e_i2["modes"][arm]["initial_windows"].append({
                "run": path.name, "offered": offered, "successes": successes})
        per_arm: dict[str, Any] = e_i2["modes"][arm]
        per_arm["admit_to_first_median_s"] = (
            statistics.median(admit_to_first) if admit_to_first else None)
        per_arm["admit_to_first_n"] = len(admit_to_first)
        per_arm["spawn_to_first_success_median_s"] = (
            statistics.median(spawn_to_first) if spawn_to_first else None)
        per_arm["spawn_to_first_success_n"] = len(spawn_to_first)
        offered_all = sum(item["offered"] for item in per_arm["initial_windows"])
        success_all = sum(item["successes"] for item in per_arm["initial_windows"])
        per_arm["initial_mix_share"] = (success_all / offered_all) if offered_all else None
        per_arm["unattributed_000"] = unattributed_000
        per_arm["total_000"] = total_000
        per_arm["zero_unattributed_000"] = unattributed_000 == 0
    e_i2["pass"] = all(e_i2["modes"][arm]["zero_unattributed_000"] for arm in arms)
    e_i2["spawn_to_first_note"] = (
        "the spawn -> first-success interval reported here carries the gated "
        "E-I1 direction check (2026-10-12 (h)); its numbers stay descriptive "
        "in this block")

    # E-I3 -- redistribution onset (descriptive): the first served request
    # reaches the new backend and how much is redistributed, per mode.
    e_i3: dict[str, Any] = {"modes": {}, "note": "descriptive only (plan §7.1 E-I3)"}
    for arm in arms:
        first_routed: list[float] = []
        redistributed = plateau_offered = 0
        for (block, run_arm), path in runs.items():
            if run_arm != arm:
                continue
            records = backend_records[(block, arm)]
            containers = {record["container"] for record in records}
            spawns = [record["spawn_started_ts"] for record in records
                      if record["spawn_started_ts"] is not None]
            flows = _completion_first_flows(path, containers)
            for entry in flows.values():
                if "first_routed_s" in entry and spawns:
                    first_routed.append(entry["first_routed_s"] - min(spawns))
            bounds = phase_bounds(path)
            start, end = bounds[PHASE]
            for row in request_rows(path):
                if row.get("phase") != PHASE or not (start <= row["_sent"] <= end):
                    continue
                plateau_offered += 1
                if str(row.get("backend_id") or "") in containers:
                    redistributed += 1
        e_i3["modes"][arm] = {
            "spawn_to_first_routed_median_s": (statistics.median(first_routed)
                                               if first_routed else None),
            "n_backends_with_flow": len(first_routed),
            "redistributed_share": (redistributed / plateau_offered)
            if plateau_offered else None,
        }

    # E-I4 -- recorded H-T3 run-level floor over the explicit 21-run denominator.
    e_i4 = _completion_health_floor(
        [blocks[block][arm] for block in sorted(blocks) for arm in arms
         if arm in blocks[block]])

    # E-W1 (2026-10-13 (m)): wake-and-verify intact direction on the same (h)
    # leg. Gated: in every primary stratum the wake spawn -> first-success
    # median is earlier than periodic (reconcile); wake vs event_only is
    # reported descriptively (near-tie expected: the event wake adds only a
    # probe round-trip).
    e_w1: dict[str, Any] = {
        "present": bool(wake_none),
        "leg": "spawn -> first successful response",
        "runs_provided": len(wake_none),
        "assessable_ok": len(wake_none) >= COMPLETION_ASSESSABLE_MIN_BLOCKS,
        "primary_strata": list(primary_strata),
        "strata": {},
    }
    if wake_none:
        wake_pooled: dict[str, list[float]] = {"all": [], "fast": [], "slow": []}
        per_run: dict[str, Any] = {}
        for ordinal, path in sorted(wake_none.items()):
            records = completion_backend_records(path)
            values = [record["spawn_to_first_success_s"] for record in records
                      if record["spawn_to_first_success_s"] is not None]
            per_run[str(ordinal)] = {
                "ordinal": ordinal,
                "seed": COMPLETION_WAKE_SEED_FAMILY.get(ordinal),
                "run": path.name, "backends": len(records),
                "median_s": statistics.median(values) if values else None,
            }
            for record in records:
                value = record["spawn_to_first_success_s"]
                if value is None:
                    continue
                wake_pooled["all"].append(value)
                if record["stratum"] in ("fast", "slow"):
                    wake_pooled[record["stratum"]].append(value)
        e_w1["per_run"] = per_run
        evidence = ok = 0
        for name in primary_strata:
            wake_values = wake_pooled[name]
            c_values = pooled[name]["reconcile"]
            a_values = pooled[name]["event_only"]
            entry: dict[str, Any] = {
                "wake_n": len(wake_values), "reconcile_n": len(c_values),
                "wake_median_s": (statistics.median(wake_values)
                                  if wake_values else None),
                "reconcile_median_s": (statistics.median(c_values)
                                       if c_values else None),
                "event_only_median_s": (statistics.median(a_values)
                                        if a_values else None),
            }
            if wake_values and c_values:
                evidence += 1
                entry["direction_wake_before_reconcile"] = bool(
                    entry["wake_median_s"] < entry["reconcile_median_s"])
                entry["median_gap_s"] = (entry["reconcile_median_s"]
                                         - entry["wake_median_s"])
                if entry["direction_wake_before_reconcile"]:
                    ok += 1
            if wake_values and a_values:
                entry["vs_event_only_median_gap_s"] = abs(
                    entry["wake_median_s"] - entry["event_only_median_s"])
                entry["vs_event_only_near_tie"] = bool(
                    entry["vs_event_only_median_gap_s"] <= COMPLETION_WAKE_NEAR_TIE_S)
            e_w1["strata"][name] = entry
        e_w1["primary_strata_evidence"] = evidence
        e_w1["primary_strata_ok"] = ok
        e_w1["pass"] = bool(
            e_w1["assessable_ok"]
            and evidence == len(primary_strata)
            and ok == evidence)
        e_w1["note"] = (
            "wake earlier than periodic in every primary stratum on the (h) "
            "leg, gated; wake vs event_only is descriptive (near-tie expected "
            "— the event wake adds a probe round-trip); the recorded pilot "
            "no-fault cost (+0.2/+3.1 pp at two seeds) is re-resolved by this "
            "set (plan §7.5 E-W1)")
    else:
        e_w1["note"] = ("wake_verify intact runs not provided — E-W1 not "
                        "evaluated (provide rq3snd_none_wake_verify_8..14)")

    gated = {
        "E-I1": e_i1["pass"],
        "E-I4_conformance":
            e_i4["recorded_qualification"]["conforms"] is True,
    }
    if e_w1["present"]:
        gated["E-W1"] = e_w1["pass"]
    na_components = []
    if len(assessable_blocks) < COMPLETION_ASSESSABLE_MIN_BLOCKS:
        na_components.append("E-I1 (fewer than 5/7 blocks assessable)")
    if e_i4["recorded_qualification"]["na"]:
        na_components.append("E-I4 (recorded-outcome conformance not verifiable)")
    if e_w1["present"] and not e_w1["assessable_ok"]:
        na_components.append(
            f"E-W1 (fewer than 5/7 wake runs provided: {e_w1['runs_provided']})")
    report = {
        "family": "rq3_admission_modes", "command": "completion-intact",
        "rate": getattr(args, "rate", "2.0"),
        "runs": len(runs), "blocks": sorted(blocks),
        "E-I1": e_i1, "E-I2": e_i2, "E-I3": e_i3, "E-I4": e_i4, "E-W1": e_w1,
        "wake_runs": len(wake_none),
        "gated_components": gated,
        "na_components": na_components,
        "failures": [],
        "descriptive_findings": [],
        "exit_codes": {"0": "all gated components pass; no NA",
                       "2": "a gated component failed or is NA (report)",
                       "3": "analyzer/argument error (STOP)"},
        "note": ("seven-block completion; E-I1 gates the spawn -> first-"
                 "success leg (2026-10-12 (h); the ready -> admission leg is "
                 "unmeasurable in this build and is a note only); the E-I4 "
                 "floor is reported exactly "
                 "as recorded (16/18, none_cell_healthy=false where it "
                 "applies) as a qualification of the recorded set, and the "
                 "conformance read requires the recorded outcome reproduced "
                 "plus a clean block 7; accept counts restate the recorded "
                 "family rules on their fixed pre-registered denominators "
                 "(plan §7, §9 NA != stop)"),
    }
    if not e_i1["pass"]:
        report["failures"].append(
            "E-I1 direction (A and B earlier than C by median, per start-up "
            "class, on the spawn -> first-success leg) not reproduced")
    if e_i4["recorded_qualification"]["conforms"] is False:
        report["failures"].append(
            "E-I4 recorded-qualification conformance failed (a fresh breach "
            "outside the recorded blocks 3/5 transients, or block 7 not clean)")
    if e_w1["present"] and not e_w1["pass"]:
        report["failures"].append(
            "E-W1 wake vs reconcile direction on the spawn -> first-success "
            "leg not reproduced in every primary stratum")
    report["descriptive_findings"] = [
        flag for flag, passed in (
            ("E-I2 unattributed 000 rows present", e_i2["pass"]),
            ("E-I2 initial-mix share below the recorded 1.000 anchor",
             all((e_i2["modes"][arm]["initial_mix_share"] or 0.0) >=
                 COMPLETION_HANDOVER_ANCHORS["useful_share"]
                 for arm in arms)),
        ) if not passed]
    report["pass"] = all(gated.values()) and not na_components
    report["na"] = bool(na_components)
    if na_components:
        report["failures"].append("NA: " + "; ".join(na_components))
    write_json(Path(args.out), report)
    print(json.dumps({"pass": report["pass"],
                      "assessable_blocks": assessable_blocks,
                      "gated": gated, "na_components": na_components,
                      "E-W1_pass": (e_w1["pass"] if e_w1["present"] else None),
                      "e_i4_clean": e_i4["clean"],
                      "e_i4_none_cell_healthy": e_i4["none_cell_healthy"],
                      "failures": report["failures"],
                      "descriptive_findings": report["descriptive_findings"]},
                     indent=2, default=str))
    return 0 if report["pass"] else 2


def completion_lost_command(args: argparse.Namespace) -> int:
    """E-L1..E-L4: the completed seven-block lost cells (plan §7.2).

    The recorded block-4 (seed 5204) exclusion is applied; the accept counts
    restate the recorded H-T1/H-T2 rules on the fixed six-block denominator
    and the added block 7 is reported per block under the same rules (the
    per-block report never re-scopes an accept count).

    Exit codes (documented in run_matrix.md and plan §11): 0 = every gated
    component passes (E-L1, E-L2, E-L3; E-L4 recorded-floor conformance) with
    no NA; 2 = a gated component failed or is NA (a report signal — NA !=
    stop); 3 = analyzer/argument error (STOP). The E-L4 floor is reported
    exactly as recorded (16/18, none_cell_healthy=false where it applies) as
    a qualification of the recorded intact set; the between-arm contrasts are
    untouched by it.
    """
    runs: dict[tuple[int, str], Path] = {}
    none_runs: dict[tuple[int, str], Path] = {}
    wake_loss: dict[int, Path] = {}
    wake_none: dict[int, Path] = {}
    problems: list[str] = []
    for path_text in args.run_dir:
        path = Path(path_text)
        parts = timing_label_parts(path.name)
        if parts is not None:
            label_cell, arm, block = parts
            key = (int(block), arm)
            target = None
            if label_cell == "loss_all":
                target = runs
            elif label_cell == "none":
                target = none_runs
            if target is None:
                problems.append(f"unexpected cell for completion-lost: {path.name}")
                continue
            if key in target:
                problems.append(f"duplicate run for block/arm {key}: {path.name}")
                continue
            target[key] = path
            continue
        wake_parts = soundness_label_parts(path.name)
        if wake_parts is None:
            problems.append(f"cannot parse label: {path.name}")
            continue
        cell, arm, ordinal = wake_parts
        if arm != "wake_verify" or cell not in ("loss_all", "none"):
            problems.append(
                f"non-timing input is not a wake_verify loss/none run: {path.name}")
            continue
        try:
            block = _completion_ordinal(ordinal)
        except ValueError:
            problems.append(f"cannot parse block ordinal: {path.name}")
            continue
        if block not in COMPLETION_WAKE_ORDINALS:
            problems.append(
                f"wake_verify {cell} ordinal {block} is not a lost input "
                f"(expected {COMPLETION_WAKE_ORDINALS[0]}.."
                f"{COMPLETION_WAKE_ORDINALS[-1]}; ordinals 1-7 are the "
                f"premature-cell partners): {path.name}")
            continue
        target = wake_loss if cell == "loss_all" else wake_none
        if block in target:
            problems.append(
                f"duplicate wake_verify {cell} ordinal {block}: {path.name}")
            continue
        target[block] = path
    if problems:
        raise ValueError("; ".join(problems))

    blocks = _completion_intact_blocks(runs)
    arms = ("event_only", "hybrid", "reconcile")
    block_rows: dict[int, dict[str, Any]] = {}
    for block in sorted(blocks):
        present = all(arm in blocks[block] for arm in arms)
        entry: dict[str, Any] = {"present": present, "assessable": False,
                                 "exclusion": None}
        if not present:
            entry["exclusion"] = "missing_arm"
            block_rows[block] = entry
            continue
        infos = {arm: _campaign_run_rates(blocks[block][arm]) for arm in arms}
        seeds = [run_seed(blocks[block][arm]) for arm in arms]
        if seeds[0] is not None and COMPLETION_LOST_SEEDS.get(block) is not None \
                and any(seed != COMPLETION_LOST_SEEDS[block] for seed in seeds):
            entry["seed_mismatch"] = seeds
        floors = all(info["floors_ok"] and info["driver_clean"]
                     for info in infos.values())
        tails = {arm: timing_tail_slow(blocks[block][arm])
                 for arm in ("hybrid", "reconcile")}
        controls_tail_ok = all(value is not None and value <= SLOW_CONTROL_MAX_LOST
                               for value in tails.values())
        delta = None
        if infos["event_only"]["slow"] is not None \
                and infos["hybrid"]["slow"] is not None \
                and infos["reconcile"]["slow"] is not None:
            delta = infos["event_only"]["slow"] - max(infos["hybrid"]["slow"],
                                                      infos["reconcile"]["slow"])
        onsets = {arm: timing_onset_slow(blocks[block][arm]) for arm in arms}
        onset_delta = None
        if all(onsets[arm] is not None for arm in arms):
            onset_delta = onsets["event_only"] - max(onsets["hybrid"],
                                                     onsets["reconcile"])
        entry.update({
            "floors_ok": floors, "driver_clean": all(info["driver_clean"]
                                                     for info in infos.values()),
            "control_tails": tails, "controls_tail_ok": controls_tail_ok,
            "delta_slow": delta, "onset_delta_slow": onset_delta,
            "event_only_slow": infos["event_only"]["slow"],
        })
        if block == COMPLETION_LOST_RECORDED_EXCLUSION:
            entry["exclusion"] = "recorded_exclusion_seed_5204"
        elif not floors:
            entry["exclusion"] = "floors_or_driver_clean"
        elif not controls_tail_ok:
            entry["exclusion"] = "controls_tail_slow_gt_2pp"
        elif delta is None or onset_delta is None:
            entry["exclusion"] = "missing_slow"
        else:
            entry["assessable"] = True
        block_rows[block] = entry

    denom = [block for block in COMPLETION_LOST_DENOM_BLOCKS
             if block_rows.get(block, {}).get("assessable")]
    extra = [block for block in COMPLETION_BLOCKS
             if block not in COMPLETION_LOST_DENOM_BLOCKS
             and block_rows.get(block, {}).get("assessable")]
    assessable_all = denom + extra
    deltas = [block_rows[block]["delta_slow"] for block in denom]
    onset_deltas = [block_rows[block]["onset_delta_slow"] for block in denom]
    hits = sum(1 for value in deltas if value >= CONTRAST_PP)
    onset_hits = sum(1 for value in onset_deltas if value >= CONTRAST_PP)
    median = statistics.median(deltas) if deltas else None
    onset_median = statistics.median(onset_deltas) if onset_deltas else None
    e_l1 = {
        "denominator_blocks": list(COMPLETION_LOST_DENOM_BLOCKS),
        "assessable_blocks": assessable_all,
        "denominator_assessable": denom,
        "delta_ge_5pp_blocks": hits,
        "onset_delta_ge_5pp_blocks": onset_hits,
        "median_pp": round(median * 100.0, 2) if median is not None else None,
        "onset_median_pp": (round(onset_median * 100.0, 2)
                            if onset_median is not None else None),
        "mwu_p": (exact_mwu(deltas, [0.0] * len(deltas)) if deltas else None),
        "cliffs_delta": (cliffs(deltas, [0.0] * len(deltas)) if deltas else None),
        "block7": None,
    }
    block7 = block_rows.get(7, {})
    if block7.get("delta_slow") is not None:
        e_l1["block7"] = {
            "delta_slow_pp": round(block7["delta_slow"] * 100.0, 2),
            "onset_delta_slow_pp": (round(block7["onset_delta_slow"] * 100.0, 2)
                                    if block7.get("onset_delta_slow") is not None
                                    else None),
            "assessable": bool(block7.get("assessable")),
        }
    e_l1["pass"] = bool(
        len(denom) >= COMPLETION_ASSESSABLE_MIN_BLOCKS  # 5 of the 6-block set
        and hits >= COMPLETION_DENOM_MIN_HITS
        and median is not None and median >= CONTRAST_PP
        and onset_hits >= COMPLETION_DENOM_MIN_HITS
        and onset_median is not None and onset_median >= CONTRAST_PP)

    # E-L2 -- mechanism + relief completion (recorded H-T2 accept counts x/6).
    e_l2: dict[str, Any] = {"per_block": {}, "relief_blocks": []}
    for block in sorted(blocks):
        entry = block_rows[block]
        if not entry.get("assessable"):
            continue
        mechanism = qoe_mechanism(blocks[block]["event_only"])
        relief = timing_relief_metrics(blocks[block]["event_only"])
        hybrid_mech = qoe_mechanism(blocks[block]["hybrid"])
        reconcile_mech = qoe_mechanism(blocks[block]["reconcile"])
        a_ok = (mechanism["admitted_total"] == 0
                and all(mechanism["per_lan_abandoned"][lan] >= 2 for lan in (1, 2))
                and relief["new_backend_successes"] == 0)
        b_ok = (hybrid_mech["admitted_total"] >= 1
                and hybrid_mech["probe_fallback_admitted"] == hybrid_mech["admitted_total"]
                and all(hybrid_mech["per_lan_admitted"][lan] >= 1 for lan in (1, 2)))
        c_ok = (reconcile_mech["admitted_total"] >= 1
                and reconcile_mech["probe_admitted"] == reconcile_mech["admitted_total"]
                and all(reconcile_mech["per_lan_admitted"][lan] >= 1 for lan in (1, 2)))
        relief_ok = all(entry["control_tails"].get(arm) is not None
                        and entry["control_tails"][arm] <= SLOW_CONTROL_MAX
                        for arm in ("hybrid", "reconcile"))
        if relief_ok and block in COMPLETION_LOST_DENOM_BLOCKS:
            e_l2["relief_blocks"].append(block)
        e_l2["per_block"][str(block)] = {
            "a_zero_admit_abandon_ge_2_zero_success": a_ok,
            "b_all_probe_fallback_ge_1_per_lan": b_ok,
            "c_all_probe_ge_1_per_lan": c_ok,
            "relief_completion_ok": relief_ok,
        }
    denom_items = [e_l2["per_block"][str(block)]
                   for block in COMPLETION_LOST_DENOM_BLOCKS
                   if str(block) in e_l2["per_block"]]
    e_l2["a_blocks"] = sum(1 for item in denom_items
                           if item["a_zero_admit_abandon_ge_2_zero_success"])
    e_l2["b_blocks"] = sum(1 for item in denom_items
                           if item["b_all_probe_fallback_ge_1_per_lan"])
    e_l2["c_blocks"] = sum(1 for item in denom_items
                           if item["c_all_probe_ge_1_per_lan"])
    e_l2["relief_block_count"] = len(e_l2["relief_blocks"])
    e_l2["block7"] = e_l2["per_block"].get("7")
    e_l2["pass"] = bool(
        e_l2["a_blocks"] >= COMPLETION_DENOM_MIN_HITS_5
        and e_l2["b_blocks"] >= COMPLETION_DENOM_MIN_HITS_5
        and e_l2["c_blocks"] >= COMPLETION_DENOM_MIN_HITS_5
        and e_l2["relief_block_count"] >= COMPLETION_DENOM_MIN_HITS_5)

    # E-L3 -- boundedness of the trusting rule (recorded H-T1 accept count);
    # the added block 7 is reported per block under the same rule.
    event_slows = [block_rows[block]["event_only_slow"] for block in denom
                   if block_rows[block].get("event_only_slow") is not None]
    bounded_hits = sum(1 for value in event_slows if value <= SLOW_BOUNDED_EVENT_MAX)
    bounded_median = statistics.median(event_slows) if event_slows else None
    e_l3 = {
        "event_only_slow": [round(value, 4) for value in event_slows],
        "median": bounded_median,
        "blocks_le_25pct": bounded_hits,
        "block7": None,
        "pass": bool(len(event_slows) >= 4 and bounded_hits >= COMPLETION_DENOM_MIN_HITS
                     and bounded_median is not None
                     and bounded_median <= SLOW_BOUNDED_EVENT_MAX),
    }
    block7_entry = block_rows.get(7, {})
    if block7_entry.get("event_only_slow") is not None:
        e_l3["block7"] = {
            "event_only_slow": round(block7_entry["event_only_slow"], 4),
            "le_25pct": block7_entry["event_only_slow"] <= SLOW_BOUNDED_EVENT_MAX,
            "assessable": bool(block7_entry.get("assessable")),
        }

    # E-L4 -- recorded H-T3 floor on the no-fault (intact) runs.
    e_l4 = (_completion_health_floor([path for _, path in sorted(none_runs.items())])
            if none_runs else {
                "denominator": COMPLETION_INTACT_RUNS_EXPECTED, "provided": 0,
                "clean": 0, "none_cell_healthy": False, "per_run": [],
                "recorded_floor": dict(COMPLETION_RECORDED_FLOOR),
                "recorded_qualification": {
                    "recorded_runs": 0, "recorded_clean": 0,
                    "breaching_blocks": [], "reproduced": False,
                    "block7_clean": None, "conforms": None, "na": True},
                "note": "intact (none-cell) runs not provided to completion-lost",
            })

    # E-W2 (2026-10-13 (m)): wake-and-verify recovery under loss. The event
    # is dropped, so the 20 s event-absence fallback admits (frozen code:
    # `admit_source="probe_fallback"` with direct propagation). Gated: the
    # per-block paired slow-share delta vs its own none partner is <= 2 pp in
    # >= 5/7 blocks (no user harm); the mechanism source and the ~21 s
    # recovery timing (recorded hybrid anchor) are recorded alongside.
    e_w2: dict[str, Any] = {
        "present": bool(wake_loss),
        "runs_provided": len(wake_loss),
        "partners_provided": len(wake_none),
        "leg": "event dropped -> 20 s fallback probe -> admission",
        "mechanism_source_expected": COMPLETION_WAKE_MECHANISM_SOURCE,
        "timing_anchor_s": COMPLETION_WAKE_LOSS_TIMING_ANCHOR_S,
        "seed_family": dict(COMPLETION_WAKE_SEED_FAMILY),
    }
    if wake_loss:
        pairs: list[dict[str, Any]] = []
        deltas: list[float] = []
        lost = 0
        sources_loss: dict[str, int] = {}
        sources_none: dict[str, int] = {}
        loss_admit_first: list[float] = []
        loss_claim_first: list[float] = []
        for ordinal in COMPLETION_WAKE_ORDINALS:
            loss_path = wake_loss.get(ordinal)
            none_path = wake_none.get(ordinal)
            entry: dict[str, Any] = {
                "block": COMPLETION_WAKE_ORDINALS.index(ordinal) + 1,
                "ordinal": ordinal,
                "seed": COMPLETION_WAKE_SEED_FAMILY.get(ordinal)}
            if loss_path is None or none_path is None:
                entry["status"] = ("missing_none_partner" if loss_path is not None
                                   else "missing_run")
                lost += 1
                pairs.append(entry)
                continue
            entry["run"] = loss_path.name
            entry["none_run"] = none_path.name
            metrics = soundness_run_metrics(loss_path)
            none_metrics = soundness_run_metrics(none_path)
            health = none_metrics.get("none_health")
            if not none_metrics.get("valid", True) or (health and health["breach"]):
                entry["status"] = "partner_breach"
                entry["partner_health"] = health
                lost += 1
                pairs.append(entry)
                continue
            actual_seeds = [value for value in (metrics.get("seed"),
                                                none_metrics.get("seed"))
                            if value is not None]
            if actual_seeds and any(
                    value != COMPLETION_WAKE_SEED_FAMILY.get(ordinal)
                    for value in actual_seeds):
                entry["seed_mismatch"] = actual_seeds
            loss_read = _completion_wake_loss_read(loss_path)
            none_read = _completion_wake_loss_read(none_path)
            for key, count in loss_read["sources"].items():
                sources_loss[key] = sources_loss.get(key, 0) + count
            for key, count in none_read["sources"].items():
                sources_none[key] = sources_none.get(key, 0) + count
            if loss_read["slow_share"] is None or none_read["slow_share"] is None:
                entry["status"] = "missing_slow"
                pairs.append(entry)
                continue
            delta_pp = (loss_read["slow_share"] - none_read["slow_share"]) * 100.0
            entry.update({
                "loss_slow_pp": round(loss_read["slow_share"] * 100.0, 4),
                "none_slow_pp": round(none_read["slow_share"] * 100.0, 4),
                "delta_pp": round(delta_pp, 4),
                "le_2pp": bool(delta_pp <= SOUNDNESS_F2_MAX_PP),
                "status": "ok",
            })
            if delta_pp <= SOUNDNESS_F2_MAX_PP:
                deltas.append(delta_pp)
            admit_first = (metrics.get("adm_flow") or {}).get("admit_to_first_median_s")
            claim_first = (metrics.get("adm_flow") or {}).get("claim_to_first_median_s")
            if admit_first is not None:
                loss_admit_first.append(admit_first)
            if claim_first is not None:
                loss_claim_first.append(claim_first)
            entry["admit_to_first_median_s"] = admit_first
            entry["claim_to_first_median_s"] = claim_first
            pairs.append(entry)
        usable = sum(1 for pair in pairs if pair.get("status") == "ok")
        le_2pp = sum(1 for pair in pairs if pair.get("le_2pp"))
        median_delta = statistics.median(
            [pair["delta_pp"] for pair in pairs
             if pair.get("status") == "ok"]) if usable else None
        e_w2.update({
            "pairs": pairs,
            "usable_pairs": usable,
            "lost_pairs": lost,
            "le_2pp_pairs": le_2pp,
            "median_delta_pp": (round(median_delta, 4)
                                if median_delta is not None else None),
            "assessable": bool(lost <= COMPLETION_PREMATURE_MAX_LOST_PAIRS),
            "mechanism_sources_loss": dict(sorted(sources_loss.items())),
            "mechanism_sources_none": dict(sorted(sources_none.items())),
            "mechanism_source_ok": _completion_wake_mechanism_ok(sources_loss),
            "recovery_admit_to_first_median_s": (
                statistics.median(loss_admit_first) if loss_admit_first else None),
            "recovery_claim_to_first_median_s": (
                statistics.median(loss_claim_first) if loss_claim_first else None),
        })
        e_w2["pass"] = bool(
            e_w2["assessable"] and usable >= COMPLETION_ASSESSABLE_MIN_BLOCKS
            and le_2pp >= COMPLETION_DENOM_MIN_HITS_5)
        e_w2["note"] = (
            "per-block paired slow-share delta (loss vs its own none partner, "
            "full plateau) <= 2 pp in >= 5/7 blocks — no user harm, gated; the "
            "recovery rides the 20 s event-absence fallback "
            "(`probe_fallback`) and its timing is reported against the "
            "recorded hybrid ~21 s anchor (descriptive; plan §7.5 E-W2)")
    else:
        e_w2["note"] = ("wake_verify loss runs not provided — E-W2 not "
                        "evaluated (provide rq3snd_loss_all_wake_verify_8..14 "
                        "with the none partners expected)")

    gated = {
        "E-L1": e_l1["pass"], "E-L2": e_l2["pass"], "E-L3": e_l3["pass"],
        "E-L4_conformance":
            e_l4["recorded_qualification"]["conforms"] is True,
    }
    if e_w2["present"]:
        gated["E-W2"] = e_w2["pass"]
    na_components = []
    if len(assessable_all) < COMPLETION_ASSESSABLE_MIN_BLOCKS:
        na_components.append("assessable blocks (<5/7)")
    if e_l4["recorded_qualification"]["na"]:
        na_components.append("E-L4 (recorded-outcome conformance not verifiable)")
    if e_w2["present"] and not e_w2["assessable"]:
        na_components.append("E-W2 (two or more wake loss pairs lost)")
    report = {
        "family": "rq3_admission_modes", "command": "completion-lost",
        "rate": getattr(args, "rate", "2.0"),
        "runs": len(runs), "blocks": sorted(blocks),
        "block_rows": {str(block): block_rows[block] for block in sorted(blocks)},
        "E-L1": e_l1, "E-L2": e_l2, "E-L3": e_l3, "E-L4": e_l4, "E-W2": e_w2,
        "wake_runs": len(wake_loss), "wake_partners": len(wake_none),
        "gated_components": gated,
        "na_components": na_components,
        "failures": [],
        "exit_codes": {"0": "all gated components pass; no NA",
                       "2": "a gated component failed or is NA (report)",
                       "3": "analyzer/argument error (STOP)"},
        "note": ("recorded block-4 (seed 5204) exclusion applied; accept "
                 "counts restate the recorded family rules on the fixed "
                 "six-block denominator; the added block 7 is reported per "
                 "block (plan §7.2). A voided timing run is quarantined as "
                 "`_7_void` and re-run under its planned label (recorded §9 "
                 "mechanic; `_void` folders never match the label regex). "
                 "The E-L4 floor is reported exactly as "
                 "recorded (16/18, none_cell_healthy=false where it applies) "
                 "as a qualification of the recorded intact set; conformance "
                 "requires the recorded outcome reproduced plus a clean "
                 "block 7 (§9 NA != stop). E-W2 (2026-10-13 (m)) adds the "
                 "wake-and-verify loss read on its own ordinal family "
                 "(8-14 -> seeds 5201-5207)"),
    }
    if not e_l1["pass"]:
        report["failures"].append("E-L1 headline contrast not reproduced")
    if not e_l2["pass"]:
        report["failures"].append("E-L2 mechanism/relief accept counts not reproduced")
    if not e_l3["pass"]:
        report["failures"].append("E-L3 boundedness accept count not reproduced")
    if e_l4["recorded_qualification"]["conforms"] is False:
        report["failures"].append(
            "E-L4 recorded-qualification conformance failed (a fresh breach "
            "outside the recorded blocks 3/5 transients, or block 7 not clean)")
    if e_w2["present"] and not e_w2["pass"]:
        report["failures"].append(
            "E-W2 wake loss paired slow-share delta > 2 pp in more than "
            "2/7 blocks (or not assessable)")
    if e_w2["present"] and not e_w2["mechanism_source_ok"]:
        report["descriptive_findings"].append(
            "E-W2 mechanism source not the recorded `probe_fallback` (found: "
            f"{sorted(e_w2['mechanism_sources_loss'])}) — a finding, not a "
            "rerun trigger")
    report["pass"] = all(gated.values()) and not na_components
    report["na"] = bool(na_components)
    if na_components:
        report["failures"].append("NA: " + "; ".join(na_components))
    write_json(Path(args.out), report)
    print(json.dumps({"pass": report["pass"],
                      "assessable_blocks": assessable_all,
                      "gated": gated, "na_components": na_components,
                      "e_l4_clean": e_l4["clean"],
                      "E-W2_pass": (e_w2["pass"] if e_w2["present"] else None),
                      "E-W2_le_2pp_pairs": e_w2.get("le_2pp_pairs"),
                      "failures": report["failures"]}, indent=2, default=str))
    return 0 if report["pass"] else 2


def _completion_premature_runs(args: argparse.Namespace):
    premature: dict[tuple[int, str], Path] = {}
    none: dict[tuple[int, str], Path] = {}
    problems: list[str] = []
    for path_text in args.run_dir:
        path = Path(path_text)
        parts = soundness_label_parts(path.name)
        if parts is None:
            problems.append(f"cannot parse soundness label: {path.name}")
            continue
        cell, arm, ordinal = parts
        try:
            block = _completion_ordinal(ordinal)
        except ValueError:
            problems.append(f"cannot parse block ordinal: {path.name}")
            continue
        if (arm == "wake_verify" and cell == "none"
                and block in COMPLETION_WAKE_ORDINALS):
            problems.append(
                f"wake_verify none ordinal {block} belongs to the intact/lost "
                f"family (completion-intact / completion-lost), not "
                f"completion-premature: {path.name}")
            continue
        if block not in COMPLETION_BLOCKS:
            problems.append(f"block {block} outside 1..7: {path.name}")
            continue
        target = premature if cell == "premature10" else none if cell == "none" else None
        if target is None:
            problems.append(f"unexpected cell for completion-premature: {path.name}")
            continue
        key = (block, arm)
        if key in target:
            problems.append(f"duplicate run for block/arm {key}: {path.name}")
            continue
        target[key] = path
    return premature, none, problems


def _completion_premature_run_check(run: dict[str, Any]) -> dict[str, Any]:
    """E-P1 per-new-run checks (plan §7.3): validity + attestation + the
    false-claim signature (A/B) or the immunity signature (C), plus the
    recorded cell band.

    Log-based gates (`check_violation_ge_1`, `zero_check_violations`) are not
    evaluable when the run folder carries neither `controller_lan1.log` nor
    `controller_lan2.log` — the recorded-era folders predate that capture
    (2026-10-13 (j)). A not-evaluable gate is reported explicitly (it is
    excluded from `gates`/`failed`) and is never counted as passed or failed.
    """
    arm = run["arm"]
    deferrals = [value for value in run["deferrals_s"] if value is not None]
    pre = run["f2"]["pre_by_n"][str(run["n"])]
    gates: dict[str, bool] = {
        "validity": bool(run["valid"]),
        "attestation_premature_s_10": bool(
            run["attestation"]["present"]
            and run["attestation"]["knobs"].get("EDGE_READY_PREMATURE_S") == 10
            and _soundness_int(run["env_knobs"].get("EDGE_READY_PREMATURE_S")) == 10),
        "measured_deferral_in_9_11": bool(
            deferrals and len(deferrals) == len(run["deferrals_s"])
            and all(9.0 <= value <= 11.0 for value in deferrals)),
    }
    not_evaluable: dict[str, str] = {}
    logs_absent = bool(run["check_violations"].get("logs_absent"))
    logs_reason = ("controller_lan{1,2}.log absent from the run folder: "
                   "log-based gate not evaluable (recorded era predates the capture)")
    if arm in ("event_only", "hybrid"):
        gates["in_window_000_ge_10_per_lan"] = bool(
            pre["http000"]["lan1"] >= 10 and pre["http000"]["lan2"] >= 10)
        if logs_absent:
            not_evaluable["check_violation_ge_1"] = logs_reason
        else:
            gates["check_violation_ge_1"] = run["check_violations"]["total"] >= 1
        gates["admit_to_first_in_8_12"] = bool(
            run["adm_flow"]["admit_to_first_median_s"] is not None
            and 8.0 <= run["adm_flow"]["admit_to_first_median_s"] <= 12.0)
    else:
        gates["run_wide_000_le_1_and_le_0.2pct"] = bool(
            run["run_wide"]["http000"] <= 1
            and run["run_wide"]["http000_share"] is not None
            and run["run_wide"]["http000_share"] <= 0.002)
        gates["zero_pre_bind_successes"] = run["pre_bind_successes"] == 0
        if logs_absent:
            not_evaluable["zero_check_violations"] = logs_reason
        else:
            gates["zero_check_violations"] = run["check_violations"]["total"] == 0
        gates["claim_to_first_in_10_22"] = bool(
            run["adm_flow"]["claim_to_first_median_s"] is not None
            and 10.0 <= run["adm_flow"]["claim_to_first_median_s"] <= 22.0)
    failed = [name for name, ok in gates.items() if not ok]
    # The C admit->first band is descriptive (plan §7.3 R2(d) sanity): it is
    # reported and never gates.
    descriptive = {}
    if arm == "reconcile":
        descriptive["admit_to_first_in_0_2"] = bool(
            run["adm_flow"]["admit_to_first_median_s"] is not None
            and 0.0 <= run["adm_flow"]["admit_to_first_median_s"] <= 2.0)
    return {
        "run": run["run"], "arm": arm, "block": _completion_ordinal(run["ordinal"]),
        "seed": run["seed"], "gates": gates, "failed": failed,
        "descriptive": descriptive, "not_evaluable": not_evaluable,
        "not_evaluable_logs_absent": logs_absent,
        "validity_failed": [name for name, ok in run["validity"].items() if not ok],
        "pass": not failed,
    }


def _completion_delta_stats(deltas: list[float]) -> dict[str, Any]:
    hits = sum(1 for value in deltas if value > 0.0)
    median = statistics.median(deltas) if deltas else None
    return {
        "n": len(deltas), "positive_blocks": hits,
        "median_pp": round(median * 100.0, 2) if median is not None else None,
        "mwu_p": (exact_mwu(deltas, [0.0] * len(deltas)) if deltas else None),
        "cliffs_delta": (cliffs(deltas, [0.0] * len(deltas)) if deltas else None),
    }


def completion_premature_command(args: argparse.Namespace) -> int:
    """E-P1..E-P3: the completed premature 10 s-lead cells, seven same-seed
    pairs (recorded blocks 1-3 + amendment blocks 4-7). E-P4 is retired: with
    matched partners at every extension seed there is no unpaired/descriptive
    damage path (2026-10-11 (b) amendment)."""
    premature, none, problems = _completion_premature_runs(args)
    if problems:
        raise ValueError("; ".join(problems))
    if not premature:
        raise ValueError("completion-premature needs the rq3snd_premature10_* runs")

    records: dict[tuple[int, str], dict[str, Any]] = {}
    for key, path in premature.items():
        records[key] = soundness_run_metrics(path)
    none_records: dict[tuple[int, str], dict[str, Any]] = {}
    for key, path in none.items():
        none_records[key] = soundness_run_metrics(path)

    # E-P1 -- per-run validity + attestation. The 12 new runs (blocks 4-7) are
    # evaluated fresh; the recorded blocks 1-3 keep their recorded
    # soundness-campaign attestations but still count toward assessability
    # only when their evaluated checks pass (presence is not credit).
    e_p1: dict[str, Any] = {"run_checks": {}, "new_run_checks": {},
                            "recorded_run_checks": {}, "cells": {}}
    for (block, arm), run in sorted(records.items()):
        entry = _completion_premature_run_check(run)
        entry["recorded"] = block in COMPLETION_PREMATURE_RECORDED_BLOCKS
        name = f"b{block}_{arm}"
        e_p1["run_checks"][name] = entry
        if block >= COMPLETION_PREMATURE_NEW_MIN_BLOCK:
            e_p1["new_run_checks"][name] = entry
        else:
            e_p1["recorded_run_checks"][name] = entry
    cell_arms = ["event_only", "hybrid", "reconcile"]
    if any(arm == "wake_verify" for (_, arm) in records):
        cell_arms.append("wake_verify")
    e_p1["wake_verify_present"] = "wake_verify" in cell_arms
    for arm in cell_arms:
        passing = sum(
            1 for block in COMPLETION_BLOCKS
            if e_p1["run_checks"].get(f"b{block}_{arm}", {}).get("pass"))
        recorded_passing = sum(
            1 for block in COMPLETION_PREMATURE_RECORDED_BLOCKS
            if e_p1["run_checks"].get(f"b{block}_{arm}", {}).get("pass"))
        new_passing = sum(
            1 for block in COMPLETION_BLOCKS
            if block >= COMPLETION_PREMATURE_NEW_MIN_BLOCK
            and e_p1["run_checks"].get(f"b{block}_{arm}", {}).get("pass"))
        e_p1["cells"][arm] = {
            "recorded_blocks_passing": recorded_passing,
            "new_runs_passing": new_passing,
            "passing_blocks": passing,
            "assessable_blocks": passing,
            "assessable": bool(passing >= COMPLETION_ASSESSABLE_MIN_BLOCKS),
        }
    e_p1["pass"] = all(e_p1["cells"][arm]["assessable"] for arm in cell_arms)
    e_p1["not_evaluable_runs"] = sorted(
        name for name, entry in e_p1["run_checks"].items()
        if entry.get("not_evaluable"))

    # E-P2 -- rule separation on all 7 blocks: per-block delta_s = U(x) - U(y)
    # (F2 window at the run's own N = 10). 2026-10-13 (m): wake_verify adds
    # the gated "wake vs A" damage contrast (U(A) - U(D)) and the descriptive
    # wake-vs-periodic read (D vs C, near-tie expected).
    def u(run: dict[str, Any]) -> float | None:
        return run["f2"]["u_by_n"].get(str(run["n"]), {}).get("u_pct")

    contrasts: list[tuple[str, str, str, bool]] = [
        ("A vs C", "event_only", "reconcile", True),
        ("B vs C", "hybrid", "reconcile", True),
    ]
    if "wake_verify" in cell_arms:
        contrasts += [
            ("A vs D (wake vs A damage contrast)", "event_only", "wake_verify",
             True),
            ("D vs C (wake vs periodic, descriptive)", "wake_verify",
             "reconcile", False),
        ]
    e_p2: dict[str, Any] = {}
    for label, arm, reference, gated_flag in contrasts:
        deltas: list[float] = []
        per_block: dict[str, Any] = {}
        for block in COMPLETION_BLOCKS:
            x = records.get((block, arm))
            y = records.get((block, reference))
            if x is None or y is None:
                per_block[str(block)] = {"delta": None, "status": "missing_run"}
                continue
            ux, uy = u(x), u(y)
            delta = (ux - uy) if ux is not None and uy is not None else None
            if delta is not None:
                deltas.append(delta / 100.0)  # u_pct values are percent points
            per_block[str(block)] = {"delta_pp": delta, "status": "ok"
                                     if delta is not None else "missing_u"}
        stats = _completion_delta_stats(deltas)
        stats["per_block"] = per_block
        stats["gated"] = gated_flag
        stats["reference"] = reference
        if gated_flag:
            stats["pass"] = bool(stats["n"] >= COMPLETION_ASSESSABLE_MIN_BLOCKS
                                 and stats["positive_blocks"]
                                 >= COMPLETION_DENOM_MIN_HITS_5
                                 and stats["median_pp"] is not None
                                 and stats["median_pp"] >= CONTRAST_PP * 100.0)
        else:
            stats["pass"] = None
            stats["near_tie"] = bool(
                stats["median_pp"] is not None
                and abs(stats["median_pp"]) <= COMPLETION_WAKE_NEAR_TIE_S)
        e_p2[label] = stats

    # E-P3 -- paired damage on the seven seed-matched pairs (no unpaired path).
    e_p3: dict[str, Any] = {"rules": {}}
    for arm in cell_arms:
        pairs: list[dict[str, Any]] = []
        ds: list[float] = []
        for block in COMPLETION_BLOCKS:
            record = records.get((block, arm))
            partner = none_records.get((block, arm))
            entry: dict[str, Any] = {"block": block,
                                     "seed": COMPLETION_PREMATURE_SEEDS[block]}
            if record is None or partner is None:
                entry["status"] = "missing_partner" if record is not None else "missing_run"
                pairs.append(entry)
                continue
            entry["premature_run"] = record["run"]
            entry["none_run"] = partner["run"]
            if not partner.get("valid", True):
                entry["status"] = "partner_invalid"
                entry["partner_invalidity"] = {
                    name: ok for name, ok in (partner.get("validity") or {}).items()
                    if not ok}
                pairs.append(entry)
                continue
            health = partner.get("none_health")
            if health and health["breach"]:
                entry["status"] = "none_breach"
                entry["none_breach_reason"] = health
                pairs.append(entry)
                continue
            u_cell, u_none = _soundness_pair_u(record, partner, "premature10")
            if u_cell is None or u_none is None:
                entry["status"] = "missing_u"
                pairs.append(entry)
                continue
            entry.update({"u_cell": u_cell, "u_none": u_none,
                          "d_s": u_cell - u_none, "status": "ok"})
            ds.append(entry["d_s"] / 100.0)
            pairs.append(entry)
        median = statistics.median(ds) if ds else None
        positives = sum(1 for value in ds if value > 0.0)
        # One-sided C criterion (per pair): D_s <= 2 pp counts as passing —
        # the recorded seeds {0.12, -0.99, -4.68} score 3/3 under this rule.
        le_2pp = sum(1 for value in ds if value * 100.0 <= SOUNDNESS_F2_MAX_PP)
        lost_pairs = len(pairs) - len(ds)
        rule: dict[str, Any] = {
            "pairs": pairs, "usable_pairs": len(ds), "lost_pairs": lost_pairs,
            "median_d_pp": round(median * 100.0, 2) if median is not None else None,
            "positive_pairs": positives, "le_2pp_pairs": le_2pp,
            "mwu_p": (exact_mwu(ds, [0.0] * len(ds)) if ds else None),
            "cliffs_delta": (cliffs(ds, [0.0] * len(ds)) if ds else None),
            # Recorded soundness §4.5 rule: two or more lost seeds (a partner
            # breach or a missing/unusable pair counts as lost) make the
            # paired-damage component not assessable.
            "assessable": bool(lost_pairs <= COMPLETION_PREMATURE_MAX_LOST_PAIRS),
        }
        if arm in ("event_only", "hybrid"):
            rule["pass"] = bool(
                rule["assessable"]
                and median is not None and median * 100.0 >= CONTRAST_PP * 100.0
                and positives >= COMPLETION_DENOM_MIN_HITS_5)
        else:
            # C: one-sided per-pair criterion (plan §7.3) — D_s <= 2 pp counts
            # as passing; there is no median bar.
            rule["pass"] = bool(
                rule["assessable"]
                and le_2pp >= COMPLETION_DENOM_MIN_HITS_5)
        e_p3["rules"][arm] = rule
    e_p3["note"] = (
        "E-P4 retired (2026-10-11 (b) amendment): matched no-fault partners "
        "at every seed 6301-6307 give seven same-seed pairs; no unpaired or "
        "descriptive damage path exists. C is one-sided per pair (D_s <= 2 pp "
        "counts as passing); a partner no-fault-floor breach or base-gate "
        "invalidity drops the pair, and two or more lost seeds make a rule's "
        "component not assessable (recorded soundness 4.5 rule). 2026-10-13 "
        "(m): wake_verify (D) is paired the same way and follows the same "
        "one-sided per-pair criterion as C.")

    report = {
        "family": "rq3_admission_modes", "command": "completion-premature",
        "runs": len(records) + len(none_records),
        "premature_runs": len(records), "none_partners": len(none_records),
        "E-P1": e_p1, "E-P2": e_p2, "E-P3": e_p3,
        "wake_verify_present": "wake_verify" in cell_arms,
        "recorded_bands": {
            "a_b_admit_to_first_s": [8, 12], "c_claim_to_first_s": [10, 22],
            "c_descriptive_admit_to_first_s": [0, 2],
        },
        "failures": [],
    }
    if not e_p1["pass"]:
        report["failures"].append("E-P1: fewer than 5/7 assessable blocks for a cell")
    for label, stats in e_p2.items():
        if stats.get("gated", True) and not stats["pass"]:
            report["failures"].append(f"E-P2 {label} contrast not reproduced")
    for arm, rule in e_p3["rules"].items():
        if not rule["pass"]:
            report["failures"].append(f"E-P3 paired damage not reproduced for {arm}")
    report["pass"] = bool(e_p1["pass"]
                          and all(stats["pass"] for stats in e_p2.values()
                                  if stats.get("gated", True))
                          and all(rule["pass"] for rule in e_p3["rules"].values()))
    if any(not rule["assessable"] for rule in e_p3["rules"].values()):
        report["na"] = True
        report["failures"].append(
            "NA: fewer than 5/7 seed-matched pairs usable for a rule")
        report["pass"] = False
    write_json(Path(args.out), report)
    print(json.dumps({"pass": report["pass"],
                      "E-P1_pass": e_p1["pass"],
                      "E-P1_log_gates_not_evaluable": e_p1["not_evaluable_runs"],
                      "E-P2": {key: (stats["pass"] if stats.get("gated", True)
                                     else "descriptive")
                               for key, stats in e_p2.items()},
                      "E-P3": {arm: rule["pass"]
                               for arm, rule in e_p3["rules"].items()},
                      "failures": report["failures"]}, indent=2, default=str))
    return 0 if report["pass"] else 2


def _completion_initial_mix(path: Path,
                            records: list[dict[str, Any]]) -> dict[str, Any]:
    """v2 useful_initial_share read: successes / offered over the union of the
    per-backend [spawn_started, admitted + 30 s] windows, plus the
    unattributed (backend-less) 000 fast-fail count."""
    windows = [(record["spawn_started_ts"],
                record["admitted_ts"] + COMPLETION_INITIAL_WINDOW_S)
               for record in records
               if record["spawn_started_ts"] is not None
               and record["admitted_ts"] is not None]
    offered = successes = unattributed_000 = total_000 = 0
    for row in request_rows(path):
        classification = soundness_class_of(row)
        if classification == "000":
            total_000 += 1
            if str(row.get("backend_id") or "") in ("", "unknown"):
                unattributed_000 += 1
        if classification in ("canceled", "dropped"):
            continue
        if any(lo <= row["_sent"] < hi for lo, hi in windows):
            offered += 1
            if classification in ("good", "slow"):
                successes += 1
    return {
        "offered": offered, "successes": successes,
        "share": (successes / offered) if offered else None,
        "unattributed_000": unattributed_000, "total_000": total_000,
    }


def _completion_support_run_stats(path: Path) -> dict[str, Any]:
    records = completion_backend_records(path)
    rows = request_rows(path)
    bounds = phase_bounds(path)
    decisions = _completion_scale_decisions(path)
    for record in records:
        lan_decisions = decisions.get(int(record.get("lan") or 0), [])
        admitted = record.get("admitted_ts")
        # The recorded anchor attributes the latest decision-log scale_up at or
        # before the backend's admission to that backend (v2 analysis_focus.md
        # column table: scale-decision -> first_success, via backend_id).
        prior = [ts for ts in lan_decisions
                 if admitted is not None and ts <= admitted]
        decision_ts = max(prior) if prior else None
        record["scale_decision_ts"] = decision_ts
        record["scale_to_first_success_s"] = (
            record["first_success_sent_s"] - decision_ts
            if record.get("first_success_sent_s") is not None
            and decision_ts is not None else None)
    total = len(rows)
    canceled = sum(1 for row in rows
                   if str(row.get("status") or "") in ("canceled", "dropped"))
    completed_in = {}
    for name in ("baseline", "compute_spike"):
        window = bounds.get(name)
        if window is None:
            completed_in[name] = 0
            continue
        lo, hi = window
        completed_in[name] = sum(
            1 for row in rows
            if row.get("phase") == name and lo <= row["_sent"] <= hi
            and is_completed(row))
    gap_bad = 0
    for record in records:
        if record["admitted_ts"] is None or record["first_success_sent_s"] is None:
            continue
        lo, hi = record["admitted_ts"], record["first_success_sent_s"]
        for row in rows:
            if lo <= row["_sent"] < hi and soundness_class_of(row) in (
                    "timeout", "000", "fail5xx"):
                gap_bad += 1
    return {
        "run": path.name,
        "backends": [
            {key: record[key] for key in (
                "container", "lan", "admit_source", "bind_s", "stratum",
                "ready_to_admit_s", "spawn_to_first_success_s",
                "admitted_to_first_routed_s", "scale_to_first_success_s")}
            for record in records],
        "window_attested": _completion_attested_window(path),
        "cancel_rate": (canceled / total) if total else None,
        "driver_clean": (canceled / total) < SOUNDNESS_CANCEL_MAX if total else False,
        "completed": completed_in,
        "floors_ok": bool(completed_in["compute_spike"] >= 2000
                          and completed_in["baseline"] >= 100),
        "gap_window_bad": gap_bad,
        "initial_mix": _completion_initial_mix(path, records),
    }


def _completion_lan_pairing(x_records: list[dict[str, Any]],
                            y_records: list[dict[str, Any]],
                            metric: str) -> list[dict[str, Any]]:
    """Per-LAN k-th-vs-k-th admitted-backend pairing (both runs ordered by
    admitted_ts): the plan's "direct earlier than discovery in every backend"
    read for the added supporting replicates, never a cross-set max/min bound.
    Backends without the metric are excluded; unpaired extras are reported by
    the caller via the per-LAN counts."""
    pairs: list[dict[str, Any]] = []
    for lan in (1, 2):
        xs = sorted((record for record in x_records
                     if record.get("lan") == lan and record.get(metric) is not None),
                    key=lambda record: record.get("admitted_ts") or 0.0)
        ys = sorted((record for record in y_records
                     if record.get("lan") == lan and record.get(metric) is not None),
                    key=lambda record: record.get("admitted_ts") or 0.0)
        for x_record, y_record in zip(xs, ys):
            pairs.append({
                "lan": lan,
                "x_container": x_record.get("container"),
                "y_container": y_record.get("container"),
                "x_s": x_record[metric], "y_s": y_record[metric],
                "x_before_y": x_record[metric] < y_record[metric],
            })
    return pairs


def _completion_scale_decisions(run_dir: Path) -> dict[int, list[float]]:
    """decision-log `scale_up` timestamps per LAN (the recorded metric's
    'scale decision' anchor: decision-log `scale_up` -> first 2xx, attributed
    via backend_id; v2 analysis_focus.md column table)."""
    decisions: dict[int, list[float]] = {1: [], 2: []}
    for lan in (1, 2):
        path = run_dir / f"decision_log_lan{lan}.csv"
        if not path.exists():
            continue
        for row in read_csv(path):
            if str(row.get("action_type") or "") != "scale_up":
                continue
            ts = as_float(row.get("ts"))
            if ts is not None:
                decisions[lan].append(ts)
    return {lan: sorted(values) for lan, values in decisions.items()}


def _completion_attested_window(run_dir: Path) -> str | None:
    """INFLIGHT_WINDOW recorded by the supporting launcher, when present."""
    path = run_dir / "rq3adm_profile_attestation.txt"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("inflight_window="):
            return line.split("=", 1)[1].strip()
    return None


def completion_support_command(args: argparse.Namespace) -> int:
    """E-S1..E-S3: the added replicates of the intact-source supporting sets
    (plan §7.4 / §4.2). Each set is read within itself; samples are never
    pooled across sets, and the set-level statistics remain the certified v2
    values (restated as anchors, not recomputed)."""
    runs: dict[tuple[str, str], tuple[int, Path]] = {}
    problems: list[str] = []
    for path_text in args.run_dir:
        path = Path(path_text)
        parts = completion_support_label_parts(path.name)
        if parts is None:
            problems.append(f"cannot parse support label: {path.name}")
            continue
        name, arm, ordinal = parts
        key = (name, arm)
        if key in runs:
            # Duplicate run identities are refused, never last-wins: two runs
            # parsing to the same set/arm (a replicate plus a rerun) would
            # silently poison the per-set read.
            problems.append(
                f"duplicate run identity {name}/{arm}: "
                f"{runs[key][1].name} and {path.name}")
            continue
        runs[key] = (int(ordinal), path)
    if problems:
        raise ValueError("; ".join(problems))
    if not runs:
        raise ValueError("completion-support needs the added replicate run folders")

    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for key, (_, path) in runs.items():
        stats[key] = _completion_support_run_stats(path)

    report: dict[str, Any] = {"family": "rq3_admission_modes",
                              "command": "completion-support",
                              "anchors": COMPLETION_SUPPORT_ANCHORS,
                              "runs": {f"{name}_{arm}": path.name
                                       for (name, arm), (_, path) in runs.items()},
                              "E-S1": None, "E-S2": None, "E-S3": None,
                              "failures": [], "descriptive_findings": [],
                              "exit_codes": {
                                  "0": "E-S1 and E-S2 direction/consistency "
                                       "checks pass",
                                  "2": "a direction/consistency check failed",
                                  "3": "analyzer/argument error (STOP)"}}

    # E-S1 -- per-backend admission campaign, 2026-10-13 (l) re-scope
    # (mirrors the 2026-10-12 (h) amendment; same instrument degeneracy): the
    # registered ready -> admit leg reads exactly 0.000 s for both mechanisms
    # (admission-log `app_ready_ts` == `admitted_ts`), so the gate rides the
    # measurable spawn -> first successful response leg with the same
    # per-backend pairing/direction style; the registered leg is reported as a
    # note and never gates.
    direct = stats.get(("camp", "direct"))
    discovery = stats.get(("camp", "discovery"))
    e_s1: dict[str, Any] = {"present": bool(direct and discovery)}
    if direct and discovery:
        dv = [record["spawn_to_first_success_s"] for record in direct["backends"]
              if record["spawn_to_first_success_s"] is not None]
        qv = [record["spawn_to_first_success_s"] for record in discovery["backends"]
              if record["spawn_to_first_success_s"] is not None]
        ready_dv = [record["ready_to_admit_s"] for record in direct["backends"]
                    if record["ready_to_admit_s"] is not None]
        ready_qv = [record["ready_to_admit_s"] for record in discovery["backends"]
                    if record["ready_to_admit_s"] is not None]
        d_sources = {record["admit_source"] for record in direct["backends"]}
        q_sources = {record["admit_source"] for record in discovery["backends"]}
        ordering_pairs = _completion_lan_pairing(
            direct["backends"], discovery["backends"], "spawn_to_first_success_s")
        ready_unmeasurable = bool(ready_dv and ready_qv) and all(
            value <= COMPLETION_DEGENERATE_LEG_EPS_S
            for value in (*ready_dv, *ready_qv))
        e_s1.update({
            "leg": "spawn -> first successful response (2026-10-13 (l) re-scope)",
            "direct_spawn_to_first_success_s": dv,
            "discovery_spawn_to_first_success_s": qv,
            "direct_median_s": statistics.median(dv) if dv else None,
            "discovery_median_s": statistics.median(qv) if qv else None,
            "direct_sources": sorted(d_sources), "discovery_sources": sorted(q_sources),
            "direct_event_sourced": d_sources == {"event"},
            "discovery_probe_sourced": q_sources == {"probe"},
            "ordering_pairs": ordering_pairs,
            "direct_before_discovery_every_backend": bool(ordering_pairs)
            and all(pair["x_before_y"] for pair in ordering_pairs),
            "direct_initial_mix": direct["initial_mix"],
            "discovery_initial_mix": discovery["initial_mix"],
            "handover_vs_anchor": {
                "anchor_useful_share": COMPLETION_HANDOVER_ANCHORS["useful_share"],
                "anchor_http000_fast_fails":
                    COMPLETION_HANDOVER_ANCHORS["http000_fast_fails"],
            },
            "ready_to_admit_leg": {
                "leg": "app_ready_ts -> admitted_ts",
                "status": ("unmeasurable in this build (instrument degeneracy; "
                           "2026-10-12 (h) cause, reconfirmed for the C3 runs "
                           "2026-10-13 (l))") if ready_unmeasurable else "measured",
                "direct_values_s": ready_dv, "discovery_values_s": ready_qv,
                "direct_le_0_05_every": bool(ready_dv)
                and all(value <= 0.05 for value in ready_dv),
                "discovery_in_0_10_every": bool(ready_qv)
                and all(0.0 < value <= COMPLETION_POLL_PERIOD_S for value in ready_qv),
                "diagnostic": ("admission-log app_ready_ts == admitted_ts for every "
                               "backend of both camp runs (0.000 s); the sources "
                               "(event vs probe) and the handover read correctly, so "
                               "this is instrument degeneracy, not a mechanism "
                               "deviation — reported, never gating"),
            },
            "ready_to_admit_leg_unmeasurable": ready_unmeasurable,
        })
        e_s1["pass"] = bool(
            e_s1["direct_event_sourced"] and e_s1["discovery_probe_sourced"]
            and e_s1["direct_before_discovery_every_backend"])
    else:
        e_s1["pass"] = False

    # E-S2 -- higher-rate arm: scale decision -> usable capacity direction,
    # the recorded gap-window null, driver clean at window 4096.
    c_direct = stats.get(("cell12", "direct"))
    c_discovery = stats.get(("cell12", "discovery"))
    e_s2: dict[str, Any] = {"present": bool(c_direct and c_discovery)}
    if c_direct and c_discovery:
        dv = [record["scale_to_first_success_s"] for record in c_direct["backends"]
              if record["scale_to_first_success_s"] is not None]
        qv = [record["scale_to_first_success_s"] for record in c_discovery["backends"]
              if record["scale_to_first_success_s"] is not None]
        ordering_pairs = _completion_lan_pairing(
            c_direct["backends"], c_discovery["backends"],
            "scale_to_first_success_s")
        e_s2.update({
            "metric": "scale decision (decision-log scale_up) -> first 2xx, "
                      "attributed via backend_id (v2 analysis_focus.md)",
            "direct_scale_to_usable_s": dv, "discovery_scale_to_usable_s": qv,
            "ordering_pairs": ordering_pairs,
            "direct_vs_discovery_every_backend": bool(ordering_pairs)
            and all(pair["x_before_y"] for pair in ordering_pairs),
            "direct_median_s": statistics.median(dv) if dv else None,
            "discovery_median_s": statistics.median(qv) if qv else None,
            "anchor_direct_median_s":
                COMPLETION_SUPPORT_ANCHORS["cell12"]["scale_to_first_success_direct_s"],
            "anchor_discovery_median_s":
                COMPLETION_SUPPORT_ANCHORS["cell12"]["scale_to_first_success_discovery_s"],
            "gap_window_bad": c_direct["gap_window_bad"] + c_discovery["gap_window_bad"],
            "gap_null_ok": (c_direct["gap_window_bad"] + c_discovery["gap_window_bad"]) == 0,
            "driver_clean": c_direct["driver_clean"] and c_discovery["driver_clean"],
            "floors_ok": c_direct["floors_ok"] and c_discovery["floors_ok"],
            "window_expected": COMPLETION_SUPPORT_ANCHORS["cell12"]["window"],
            "window_attested": {"direct": c_direct.get("window_attested"),
                                "discovery": c_discovery.get("window_attested")},
        })
        e_s2["pass"] = bool(
            e_s2["direct_vs_discovery_every_backend"] and e_s2["gap_null_ok"]
            and e_s2["driver_clean"] and e_s2["floors_ok"])
    else:
        e_s2["pass"] = False

    # E-S3 -- poll-interval sensitivity (descriptive): the 15 s poll's
    # quantization scales with the period; the expected band is ~one period
    # (10-20 s). Report-only: the band never gates. 2026-10-13 (l): the
    # registered leg is the same degenerate one as E-S1 — when it reads
    # 0.000 s the band check is not assessable and is reported as a note.
    disc15 = stats.get(("disc15", "discovery"))
    e_s3: dict[str, Any] = {"present": bool(disc15), "descriptive": True,
                            "expected_band_s": [COMPLETION_DISC15_POLL_S - 5.0,
                                                COMPLETION_DISC15_POLL_S + 5.0]}
    if disc15:
        values = [record["ready_to_admit_s"] for record in disc15["backends"]
                  if record["ready_to_admit_s"] is not None]
        median = statistics.median(values) if values else None
        band = e_s3["expected_band_s"]
        degenerate = bool(values) and all(
            value <= COMPLETION_DEGENERATE_LEG_EPS_S for value in values)
        e_s3.update({
            "ready_to_admit_s": values,
            "median_s": median,
            "ready_to_admit_leg_unmeasurable": degenerate,
            "ready_to_admit_leg_note": (
                "registered ready -> admit leg reads 0.000 s for every backend "
                "(instrument degeneracy, 2026-10-12 (h)/(l)); band check not "
                "assessable") if degenerate else None,
            "quantization_within_expected_band": (
                None if degenerate else bool(
                    median is not None and band[0] <= median <= band[1])),
            "quantization_within_period_scale": (
                None if degenerate else bool(
                    median is not None and 0.0 < median
                    <= 2.0 * COMPLETION_DISC15_POLL_S)),
            "sources": sorted({record["admit_source"]
                               for record in disc15["backends"]}),
            "floors_ok": disc15["floors_ok"], "driver_clean": disc15["driver_clean"],
        })
    else:
        e_s3["quantization_within_expected_band"] = None

    report["E-S1"], report["E-S2"], report["E-S3"] = e_s1, e_s2, e_s3
    report["set_runs"] = {f"{name}_{arm}": _completion_support_run_report(stats, name, arm)
                          for (name, arm) in stats}
    for name, section in (("E-S1", e_s1), ("E-S2", e_s2)):
        if section.get("present") and not section["pass"]:
            report["failures"].append(f"{name} direction/consistency check failed")
        elif not section.get("present"):
            report["failures"].append(f"{name} run(s) not provided")
    report["pass"] = bool(e_s1.get("pass") and e_s2.get("pass"))
    if disc15:
        if e_s3.get("quantization_within_expected_band") is False:
            report["descriptive_findings"].append(
                "E-S3 quantization outside the expected ~one-period band "
                "(descriptive only — a finding, not a rerun trigger)")
        elif e_s3.get("ready_to_admit_leg_unmeasurable"):
            report["descriptive_findings"].append(
                "E-S3 registered ready -> admit leg unmeasurable in this build "
                "(instrument degeneracy, 2026-10-12 (h)/(l)); band check not "
                "assessable — descriptive only")
    write_json(Path(args.out), report)
    print(json.dumps({"pass": report["pass"],
                      "E-S1_pass": e_s1.get("pass"), "E-S2_pass": e_s2.get("pass"),
                      "E-S3_descriptive": (
                          {"present": True,
                           "verdict": (
                               "not_assessable_leg_unmeasurable"
                               if e_s3.get("ready_to_admit_leg_unmeasurable")
                               else ("within_band"
                                     if e_s3.get("quantization_within_expected_band")
                                     else "out_of_band")),
                           "within_expected_band":
                               e_s3.get("quantization_within_expected_band"),
                           "median_s": e_s3.get("median_s"),
                           "expected_band_s": e_s3.get("expected_band_s")}
                          if e_s3.get("present") else None),
                      "failures": report["failures"]}, indent=2, default=str))
    return 0 if report["pass"] else 2


def _completion_support_run_report(stats: dict[tuple[str, str], dict[str, Any]],
                                   name: str, arm: str) -> dict[str, Any]:
    entry = stats.get((name, arm))
    if entry is None:
        return {"present": False}
    return {
        "present": True, "run": entry["run"],
        "backends": len(entry["backends"]),
        "floors_ok": entry["floors_ok"], "driver_clean": entry["driver_clean"],
        "cancel_rate": entry["cancel_rate"],
        "gap_window_bad": entry["gap_window_bad"],
        "completed": entry["completed"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--run-dir", nargs="+", required=True)
    classify_parser.add_argument("--out", required=True)
    classify_parser.set_defaults(func=classify_command)
    stage1 = sub.add_parser("stage1")
    stage1.add_argument("--run-dir", nargs="+", required=True)
    stage1.add_argument("--out", required=True)
    stage1.set_defaults(func=stage1_command)
    restart = sub.add_parser("restart-observation")
    restart.add_argument("--run-dir", nargs="+", required=True)
    restart.add_argument("--out", required=True)
    restart.set_defaults(func=restart_command)
    screen = sub.add_parser("qoe-screen")
    screen.add_argument("--stage", required=True, choices=("c1", "c2", "c3"))
    screen.add_argument("--rung", required=True)
    screen.add_argument("--seed", required=True)
    screen.add_argument("--run-dir", nargs="*", default=[])
    screen.add_argument("--c1-json", default=None)
    screen.add_argument("--out", required=True)
    screen.set_defaults(func=qoe_screen_command)
    campaign = sub.add_parser("qoe-campaign")
    campaign.add_argument("--run-dir", nargs="+", required=True)
    campaign.add_argument("--rung", required=True)
    campaign.add_argument("--out", required=True)
    campaign.set_defaults(func=qoe_campaign_command)
    timing_screen = sub.add_parser("timing-screen")
    timing_screen.add_argument("--stage", required=True,
                               choices=("p1", "p2", "lock"))
    timing_screen.add_argument("--rate", required=True)
    timing_screen.add_argument("--seed", required=True)
    timing_screen.add_argument("--run-dir", nargs="*", default=[])
    timing_screen.add_argument("--screen-json", nargs="+", default=[])
    timing_screen.add_argument("--out", required=True)
    timing_screen.add_argument("--lock-out", default=None)
    timing_screen.set_defaults(func=timing_screen_command)
    timing_campaign = sub.add_parser("timing-campaign")
    timing_campaign.add_argument("--run-dir", nargs="+", required=True)
    timing_campaign.add_argument("--rate", required=True)
    timing_campaign.add_argument("--out", required=True)
    timing_campaign.set_defaults(func=timing_campaign_command)
    soundness_damage = sub.add_parser("soundness-damage")
    soundness_damage.add_argument("--run-dir", nargs="+", required=True)
    soundness_damage.add_argument("--out", required=True)
    soundness_damage.set_defaults(func=soundness_damage_command)
    soundness_preflight = sub.add_parser("soundness-preflight")
    soundness_preflight.add_argument("--run-dir", nargs="+", required=True)
    soundness_preflight.add_argument("--out", required=True)
    soundness_preflight.add_argument("--lock-out", required=True)
    soundness_preflight.add_argument(
        "--rerun-attempted", action="store_true",
        help="the pre-registered same-config rerun has already been attempted: "
             "persisting DIAGNOSE reasons (e.g. an R3 coverage shortfall) "
             "become STOP (exit 3) instead of exit 2")
    soundness_preflight.set_defaults(func=soundness_preflight_command)
    soundness_crossover = sub.add_parser("soundness-crossover")
    soundness_crossover.add_argument("--damage-json", required=True)
    soundness_crossover.add_argument("--out", required=True)
    soundness_crossover.set_defaults(func=soundness_crossover_command)
    intact = sub.add_parser("completion-intact")
    intact.add_argument("--run-dir", nargs="+", required=True)
    intact.add_argument("--rate", default="2.0")
    intact.add_argument("--out", required=True)
    intact.set_defaults(func=completion_intact_command)
    lost = sub.add_parser("completion-lost")
    lost.add_argument("--run-dir", nargs="+", required=True)
    lost.add_argument("--rate", default="2.0")
    lost.add_argument("--out", required=True)
    lost.set_defaults(func=completion_lost_command)
    premature = sub.add_parser("completion-premature")
    premature.add_argument("--run-dir", nargs="+", required=True)
    premature.add_argument("--out", required=True)
    premature.set_defaults(func=completion_premature_command)
    support = sub.add_parser("completion-support")
    support.add_argument("--run-dir", nargs="+", required=True)
    support.add_argument("--out", required=True)
    support.set_defaults(func=completion_support_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - analyzer error paths are STOP (3)
        # Exit 2 is reserved for genuine DIAGNOSE verdicts: parse failures and
        # unexpected exceptions are STOP (exit 3) with a reason on stderr.
        print(f"ERROR: analyzer error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
