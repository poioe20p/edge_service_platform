"""time_to_capacity — M3: Time from phase start to sufficient capacity.

Produces <run_dir>/analysis/rq1/:
  rq1_time_to_capacity.csv  — per-phase time-to-capacity metrics

Capacity point: first 10s bucket where:
  - p95 local latency < 0.5s
  - server_count >= 2

Local requests: client_lan == target_region, exclude feed_ranking in phases
with cross_region_ratio > 0.

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.time_to_capacity --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from ...loader import load_run
from ...client_status import is_completed


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def compute_time_to_capacity(run_dir: Path) -> list[dict]:
    """For each high-load phase, compute time from start to capacity."""
    run = load_run(run_dir)

    # Phase boundaries
    phase_starts: dict[str, float] = {}
    phase_ends: dict[str, float] = {}
    phase_cross_region: dict[str, float] = {}
    cumulative = 0.0
    for p in run.phases:
        phase_starts[p.name] = run.t0 + cumulative
        cumulative += p.duration_s
        phase_ends[p.name] = run.t0 + cumulative
        phase_cross_region[p.name] = p.cross_region_ratio

    if not run.phases or not run.all_client_rows:
        print("[time_to_capacity] Missing phases or client data")
        return []

    # Bucket client requests by (phase, 10s bucket)
    # bucket_key = int((sent_at - phase_start) / 10) * 10
    local_latencies: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in run.all_client_rows:
        phase = row.get("phase", "unknown")
        if phase not in phase_starts:
            continue
        sent_at = _safe_float(row.get("sent_at"))
        client_lan = row.get("client_lan", "")
        target_region = row.get("target_region", "")
        endpoint = row.get("endpoint", "")
        lat = _safe_float(row.get("latency_s"))

        # Local requests only
        if client_lan != target_region:
            continue

        # Exclude feed_ranking in cross-region phases
        crr = phase_cross_region.get(phase, 0)
        if endpoint == "feed_ranking" and crr > 0:
            continue

        if not is_completed(row):
            continue

        phase_start = phase_starts[phase]
        bucket_offset = int((sent_at - phase_start) / 10) * 10
        if bucket_offset < 0:
            continue
        local_latencies[(phase, bucket_offset)].append(lat)

    # Server counts per window
    srv_by_ts: dict[float, float] = {}
    for row in run.domain_rows:
        ts = _safe_float(row.get("window_end"))
        srv = _safe_float(row.get("server_count"))
        if ts > 0:
            srv_by_ts[ts] = srv

    high_load_phases = {"storage_storm", "tier1_hotspot", "reverse_hotspot", "compute_spike"}
    results = []

    for p in run.phases:
        if p.name not in high_load_phases:
            continue

        phase_start = phase_starts[p.name]
        phase_end = phase_ends[p.name]
        duration = p.duration_s

        time_to_capacity = None
        capacity_bucket = None

        for bucket_offset in range(0, int(duration), 10):
            key = (p.name, bucket_offset)
            lats = local_latencies.get(key, [])
            if len(lats) < 10:
                continue

            p95_local = _percentile(lats, 0.95)

            # Find server_count at this bucket
            bucket_ts = phase_start + bucket_offset
            srv = 0.0
            for ts, s in sorted(srv_by_ts.items()):
                if abs(ts - bucket_ts) <= 15:  # within 15s window
                    srv = s
                    break

            if p95_local < 0.5 and srv >= 2:
                time_to_capacity = bucket_offset
                capacity_bucket = bucket_offset
                break

        results.append({
            "phase": p.name,
            "duration_s": duration,
            "time_to_capacity_s": time_to_capacity if time_to_capacity is not None else "not_achieved",
            "capacity_bucket_offset": capacity_bucket if capacity_bucket is not None else "",
            "note": "",
        })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="M3: Time-to-capacity analysis")
    parser.add_argument("--run-dir", required=True, type=Path, help="Path to run directory")
    args = parser.parse_args()

    run_dir: Path = args.run_dir.resolve()
    print(f"[time_to_capacity] run_dir={run_dir}")

    rows = compute_time_to_capacity(run_dir)
    if not rows:
        print("[time_to_capacity] No data")
        return

    out_dir = run_dir / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "rq1_time_to_capacity.csv"

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"[time_to_capacity] wrote {out_path} ({len(rows)} phases)")
    for r in rows:
        ttc = r["time_to_capacity_s"]
        status = f"{ttc}s" if isinstance(ttc, (int, float)) else ttc
        print(f"  {r['phase']}: time_to_capacity={status}")


if __name__ == "__main__":
    main()
