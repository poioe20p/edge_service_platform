"""endpoint_latency — M8: Per-endpoint latency breakdown.

Produces <run_dir>/analysis/rq1/:
  rq1_endpoint_latency.csv  — per-phase, per-endpoint p50/p95/p99 latency

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.endpoint_latency --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from ...loader import load_run
from ...client_status import is_completed


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


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compute_endpoint_latency(run_dir: Path) -> list[dict]:
    """Compute per-phase, per-endpoint latency percentiles."""
    run = load_run(run_dir)
    if not run.all_client_rows:
        print("[endpoint_latency] No client_requests.csv data")
        return []

    # Group latencies by (phase, endpoint)
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in run.all_client_rows:
        if not is_completed(row):
            continue
        phase = row.get("phase", "unknown")
        endpoint = row.get("endpoint", "unknown")
        lat = _safe_float(row.get("latency_s"))
        if lat >= 0:
            buckets[(phase, endpoint)].append(lat)

    results = []
    for (phase, endpoint), lats in sorted(buckets.items()):
        if len(lats) < 5:
            continue
        results.append({
            "phase": phase,
            "endpoint": endpoint,
            "count": len(lats),
            "p50": round(_percentile(lats, 0.50), 6),
            "p95": round(_percentile(lats, 0.95), 6),
            "p99": round(_percentile(lats, 0.99), 6),
            "mean": round(sum(lats) / len(lats), 6),
        })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="M8: Per-endpoint latency breakdown")
    parser.add_argument("--run-dir", required=True, type=Path, help="Path to run directory")
    args = parser.parse_args()

    run_dir: Path = args.run_dir.resolve()
    print(f"[endpoint_latency] run_dir={run_dir}")

    rows = compute_endpoint_latency(run_dir)
    if not rows:
        print("[endpoint_latency] No data to write")
        return

    out_dir = run_dir / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "rq1_endpoint_latency.csv"

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["phase", "endpoint", "count", "p50", "p95", "p99", "mean"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"[endpoint_latency] wrote {out_path} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
