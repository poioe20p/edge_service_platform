"""blind_spot_windows — M6: Telemetry windows breached but not consumed.

Produces <run_dir>/analysis/rq1/:
  rq1_blind_spot_windows.csv  — per-window blind spot analysis

A blind spot window = breach window that the controller never consumed.
- Breach: degradation_score >= threshold (using controller's scoring formula)
- Consumed: window_end timestamp appears in controller's telemetry consumption log
  (from resource_stats_debug.csv consumed_at field)

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.blind_spot_windows --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from pathlib import Path

from ...loader import load_run


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _load_scoring_params(run_dir: Path) -> dict:
    """Load scoring parameters from controller_env_snapshot.env."""
    env_path = run_dir / "controller_env_snapshot.env"
    params = {
        "W_CPU": 0.60, "CPU_FLOOR": 10, "CPU_SPAN": 40,
        "W_T_PROC": 0.40, "T_PROC_FLOOR": 25, "T_PROC_SPAN": 500,
        "BASE_THRESHOLD": 0.18, "THRESHOLD_INCREMENT": 0.10,
    }
    if not env_path.exists():
        return params

    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key in params:
                try:
                    params[key] = float(val)
                except ValueError:
                    pass
    return params


def compute_blind_spots(run_dir: Path) -> list[dict]:
    """Compute blind spot windows for the run."""
    run = load_run(run_dir)
    params = _load_scoring_params(run_dir)

    W_CPU = params["W_CPU"]
    CPU_FLOOR = params["CPU_FLOOR"]
    CPU_SPAN = params["CPU_SPAN"]
    W_T_PROC = params["W_T_PROC"]
    T_PROC_FLOOR = params["T_PROC_FLOOR"]
    T_PROC_SPAN = params["T_PROC_SPAN"]
    BASE_THRESHOLD = params["BASE_THRESHOLD"]
    THRESHOLD_INCREMENT = params["THRESHOLD_INCREMENT"]

    # Build per-window CPU and T_proc from per_node_stats
    # window_end -> {cpu_list, t_proc_list}
    window_stats: dict[float, dict] = defaultdict(lambda: {"cpus": [], "t_procs": []})
    for row in run.node_rows:
        ts = _safe_float(row.get("window_end"))
        cpu = _safe_float(row.get("cpu_percent"))
        t_proc = _safe_float(row.get("median_time_proc_ms"))
        if ts > 0:
            if cpu > 0:
                window_stats[ts]["cpus"].append(cpu)
            if t_proc > 0:
                window_stats[ts]["t_procs"].append(t_proc)

    # Build consumed window set from debug_rows
    consumed_windows: set[float] = set()
    for row in run.debug_rows:
        window_end = _safe_float(row.get("window_end"))
        consumed_at = _safe_float(row.get("consumed_at"))
        if window_end > 0 and consumed_at > 0:
            consumed_windows.add(round(window_end, 3))

    # Also try to get consumed windows from resource_stats_debug.csv directly
    debug_path = run_dir / "resource_stats_debug.csv"
    if debug_path.exists():
        with debug_path.open(newline="") as f:
            for row in csv.DictReader(f):
                we = _safe_float(row.get("window_end"))
                ca = _safe_float(row.get("consumed_at"))
                if we > 0 and ca > 0:
                    consumed_windows.add(round(we, 3))

    if not window_stats:
        print("[blind_spot_windows] No per-node stats available")
        return []

    # Track dynamic node count for adaptive threshold
    # Use node_lifecycle_timings to count active nodes per window
    spawn_times: list[tuple[float, int]] = []  # (timestamp, +1 for add, -1 for remove)
    nlt_path = run_dir / "node_lifecycle_timings.csv"
    if nlt_path.exists():
        with nlt_path.open(newline="") as f:
            for row in csv.DictReader(f):
                nt = row.get("node_type", "")
                if "compute" not in nt.lower():
                    continue
                add_ts = _safe_float(row.get("add_time"))
                remove_ts = _safe_float(row.get("remove_time", "0"))
                if add_ts > 0:
                    spawn_times.append((add_ts, 1))
                if remove_ts > 0:
                    spawn_times.append((remove_ts, -1))
    spawn_times.sort()

    # Also check container_events
    for row in run.container_event_rows:
        nt = row.get("node_type", "")
        if "compute" not in nt.lower():
            continue
        add_ts = _safe_float(row.get("online_time"))
        if add_ts > 0:
            spawn_times.append((add_ts, 1))

    # Compute degradation score and breach status for each window
    results = []
    sorted_windows = sorted(window_stats.keys())

    total_windows = 0
    breached_windows = 0
    blind_spot_windows = 0

    for ts in sorted_windows:
        stats = window_stats[ts]
        cpus = stats["cpus"]
        t_procs = stats["t_procs"]

        # Mean CPU and T_proc for this window
        mean_cpu = sum(cpus) / len(cpus) if cpus else 0
        mean_t_proc = sum(t_procs) / len(t_procs) if t_procs else 0

        # Degradation score (same formula as controller)
        cpu_score = _clamp((mean_cpu - CPU_FLOOR) / CPU_SPAN) if CPU_SPAN > 0 else 0
        t_proc_score = _clamp((mean_t_proc - T_PROC_FLOOR) / T_PROC_SPAN) if T_PROC_SPAN > 0 else 0
        degradation_score = W_CPU * cpu_score + W_T_PROC * t_proc_score

        # Count active dynamic nodes at this window
        active_nodes = 0
        for st, delta in spawn_times:
            if st <= ts:
                active_nodes += delta
            else:
                break
        active_nodes = max(0, active_nodes)

        # Adaptive threshold
        threshold = BASE_THRESHOLD + THRESHOLD_INCREMENT * active_nodes

        # Breach?
        breached = degradation_score >= threshold

        # Consumed?
        consumed = round(ts, 3) in consumed_windows

        # Blind spot?
        blind_spot = breached and not consumed

        # Requests in shadow: count requests in the following 10s window
        shadow_requests = 0
        for row in run.all_client_rows:
            sent_at = _safe_float(row.get("sent_at"))
            if ts <= sent_at < ts + 10:
                shadow_requests += 1

        total_windows += 1
        if breached:
            breached_windows += 1
        if blind_spot:
            blind_spot_windows += 1

        results.append({
            "window_end": round(ts, 3),
            "mean_cpu": round(mean_cpu, 1),
            "mean_t_proc": round(mean_t_proc, 1),
            "degradation_score": round(degradation_score, 4),
            "threshold": round(threshold, 3),
            "active_dynamic_nodes": active_nodes,
            "breached": breached,
            "consumed": consumed,
            "blind_spot": blind_spot,
            "shadow_requests": shadow_requests,
        })

    # Summary
    blind_spot_rate = (blind_spot_windows / breached_windows * 100) if breached_windows > 0 else 0
    print(f"[blind_spot_windows] total_windows={total_windows}")
    print(f"[blind_spot_windows] breached={breached_windows}")
    print(f"[blind_spot_windows] consumed={breached_windows - blind_spot_windows}")
    print(f"[blind_spot_windows] blind_spots={blind_spot_windows}")
    print(f"[blind_spot_windows] blind_spot_rate={blind_spot_rate:.1f}%")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="M6: Blind spot window analysis")
    parser.add_argument("--run-dir", required=True, type=Path, help="Path to run directory")
    args = parser.parse_args()

    run_dir: Path = args.run_dir.resolve()
    print(f"[blind_spot_windows] run_dir={run_dir}")

    rows = compute_blind_spots(run_dir)
    if not rows:
        print("[blind_spot_windows] No data")
        return

    out_dir = run_dir / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "rq1_blind_spot_windows.csv"

    fieldnames = [
        "window_end", "mean_cpu", "mean_t_proc", "degradation_score",
        "threshold", "active_dynamic_nodes", "breached", "consumed",
        "blind_spot", "shadow_requests",
    ]
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[blind_spot_windows] wrote {out_path} ({len(rows)} windows)")


if __name__ == "__main__":
    main()
