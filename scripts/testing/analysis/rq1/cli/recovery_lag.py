"""recovery_lag — M9: Time from demand_drop to baseline server_count.

Produces <run_dir>/analysis/rq1/:
  rq1_recovery_lag.csv  — recovery lag, peak server_count, node-seconds wasted

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.recovery_lag --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from ...loader import load_run


def compute_recovery_lag(run_dir: Path) -> dict | None:
    """Compute recovery lag after demand_drop phase.

    Returns a dict with recovery metrics, or None if demand_drop not found.
    """
    run = load_run(run_dir)

    # Find demand_drop phase
    dd_phase = None
    cumulative = 0.0
    for p in run.phases:
        if p.name == "demand_drop":
            dd_phase = p
            dd_start_offset = cumulative
            break
        cumulative += p.duration_s

    if dd_phase is None:
        print("[recovery_lag] No demand_drop phase found")
        return None

    dd_start_ts = run.t0 + dd_start_offset
    dd_end_ts = dd_start_ts + dd_phase.duration_s

    # Get server_count from resource_stats during and after demand_drop
    server_counts: list[tuple[float, float]] = []  # (timestamp, server_count)
    baseline_server = 2  # The baseline level before spawning

    for row in run.domain_rows:
        ts = float(row.get("window_end", 0))
        if ts <= 0:
            continue
        srv = float(row.get("server_count", 0))
        # Include demand_drop and beyond
        if ts >= dd_start_ts:
            server_counts.append((ts, srv))

    if not server_counts:
        print("[recovery_lag] No resource_stats data in demand_drop window")
        return None

    # Find peak server_count during demand_drop
    peak_srv = 0.0
    for ts, srv in server_counts:
        if ts <= dd_end_ts and srv > peak_srv:
            peak_srv = srv

    # Find recovery: first window where server_count <= 2 AND stays <= 2
    # for 7 consecutive windows (matching 7/12 scale-down sliding window)
    recovery_ts = None
    stable_count = 0
    for ts, srv in sorted(server_counts):
        if srv <= baseline_server:
            stable_count += 1
            if stable_count >= 7 and recovery_ts is None:
                recovery_ts = ts
        else:
            stable_count = 0

    recovery_lag = None
    if recovery_ts is not None:
        recovery_lag = recovery_ts - dd_start_ts

    # Compute node-seconds wasted: area under server_count curve above baseline
    # during demand_drop (trapezoidal integration at each 10s window)
    node_seconds_wasted = 0.0
    dd_windows = [(ts, srv) for ts, srv in server_counts if dd_start_ts <= ts <= dd_end_ts]
    for i in range(1, len(dd_windows)):
        t0, s0 = dd_windows[i - 1]
        t1, s1 = dd_windows[i]
        dt = t1 - t0
        if dt <= 0 or dt > 30:  # Sanity: skip gaps > 30s
            continue
        excess0 = max(0, s0 - baseline_server)
        excess1 = max(0, s1 - baseline_server)
        avg_excess = (excess0 + excess1) / 2
        node_seconds_wasted += avg_excess * dt

    return {
        "dd_start_offset": round(dd_start_offset, 1),
        "dd_duration_s": dd_phase.duration_s,
        "peak_server_count": int(peak_srv),
        "recovery_lag_s": round(recovery_lag, 1) if recovery_lag is not None else "not_achieved",
        "node_seconds_wasted": round(node_seconds_wasted, 1),
        "stable_consecutive_windows": 7,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="M9: Recovery lag analysis")
    parser.add_argument("--run-dir", required=True, type=Path, help="Path to run directory")
    args = parser.parse_args()

    run_dir: Path = args.run_dir.resolve()
    print(f"[recovery_lag] run_dir={run_dir}")

    result = compute_recovery_lag(run_dir)
    if result is None:
        print("[recovery_lag] Could not compute recovery lag")
        return

    out_dir = run_dir / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "rq1_recovery_lag.csv"

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(result.keys()))
        writer.writeheader()
        writer.writerow(result)

    print(f"[recovery_lag] wrote {out_path}")
    print(f"  peak_server_count={result['peak_server_count']}")
    print(f"  recovery_lag={result['recovery_lag_s']}s")
    print(f"  node_seconds_wasted={result['node_seconds_wasted']}")


if __name__ == "__main__":
    main()
