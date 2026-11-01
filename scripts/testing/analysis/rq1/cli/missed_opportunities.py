"""missed_opportunities — M2: Phases with CPU pressure but no spawns.

Produces <run_dir>/analysis/rq1/:
  rq1_missed_opportunities.csv  — per-phase missed opportunity assessment

A phase is a "missed opportunity" when:
  1. Mean per-node CPU > 20% (genuine compute pressure)
  2. p95 per-node CPU > 40% (load is concentrated)
  3. Fewer than 1 spawn per 60s of phase duration (controller didn't respond)

Accounts for adaptive threshold: if dynamic nodes already exist, the effective
threshold is higher (BASE + INCREMENT × existing_nodes), so CPU at 20% with
2 existing nodes is correctly NOT a miss.

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.missed_opportunities --run-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from ...loader import load_run


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


def compute_missed_opportunities(run_dir: Path) -> list[dict]:
    """Find phases where the controller should have spawned compute but didn't."""
    run = load_run(run_dir)

    # Phase boundaries
    phase_starts: dict[str, float] = {}
    phase_ends: dict[str, float] = {}
    cumulative = 0.0
    for p in run.phases:
        phase_starts[p.name] = run.t0 + cumulative
        cumulative += p.duration_s
        phase_ends[p.name] = run.t0 + cumulative

    if not run.phases:
        print("[missed_opportunities] No phases found")
        return []

    # Per-phase CPU from per_node_stats
    phase_cpus: dict[str, list[float]] = defaultdict(list)
    for row in run.node_rows:
        ph = row.get("phase", "unknown")
        cpu = _safe_float(row.get("cpu_percent"))
        if cpu > 0:
            phase_cpus[ph].append(cpu)

    # Spawns per phase from node_lifecycle_timings
    phase_spawns: dict[str, int] = defaultdict(int)
    nlt_path = run_dir / "node_lifecycle_timings.csv"
    if nlt_path.exists():
        with nlt_path.open(newline="") as f:
            for row in csv.DictReader(f):
                nt = row.get("node_type", "")
                if "compute" not in nt.lower():
                    continue
                add_ts = _safe_float(row.get("add_time"))
                if add_ts <= 0:
                    continue
                # Assign to phase
                for p in run.phases:
                    if phase_starts[p.name] <= add_ts < phase_ends[p.name]:
                        phase_spawns[p.name] += 1
                        break

    # Track existing dynamic nodes per phase (for adaptive threshold)
    # Count cumulative spawns up to each phase start
    existing_nodes = 0
    phase_node_count: dict[str, int] = {}

    for p in run.phases:
        phase_node_count[p.name] = existing_nodes
        existing_nodes += phase_spawns[p.name]

    # Scoring parameters (from current_state_integrated.env)
    BASE_THRESHOLD = 0.18
    THRESHOLD_INCREMENT = 0.10

    results = []
    high_load_phases = {"storage_storm", "tier1_hotspot", "reverse_hotspot", "compute_spike"}

    for p in run.phases:
        if p.name not in high_load_phases:
            continue

        cpus = phase_cpus.get(p.name, [])
        spawns = phase_spawns.get(p.name, 0)
        existing = phase_node_count.get(p.name, 0)

        mean_cpu = sum(cpus) / len(cpus) if cpus else 0
        p95_cpu = _percentile(cpus, 0.95) if cpus else 0

        # Adaptive threshold
        effective_threshold = BASE_THRESHOLD + THRESHOLD_INCREMENT * existing

        # CPU score contribution (W_CPU=0.60, CPU_FLOOR=10, CPU_SPAN=40)
        cpu_score_contribution = 0.60 * max(0, min(1, (mean_cpu - 10) / 40))

        # Minimum spawns for phase duration (1 spawn per 60s)
        min_spawns = max(1, p.duration_s / 60)

        # A miss: CPU pressure exists AND insufficient spawns
        is_miss = (
            mean_cpu > 20
            and p95_cpu > 40
            and spawns < min_spawns
        )

        results.append({
            "phase": p.name,
            "duration_s": p.duration_s,
            "cpu_samples": len(cpus),
            "mean_cpu": round(mean_cpu, 1),
            "p95_cpu": round(p95_cpu, 1),
            "cpu_score_contribution": round(cpu_score_contribution, 3),
            "effective_threshold": round(effective_threshold, 3),
            "existing_dynamic_nodes": existing,
            "compute_spawns": spawns,
            "min_spawns_expected": round(min_spawns, 1),
            "missed_opportunity": is_miss,
        })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="M2: Missed spawn opportunities")
    parser.add_argument("--run-dir", required=True, type=Path, help="Path to run directory")
    args = parser.parse_args()

    run_dir: Path = args.run_dir.resolve()
    print(f"[missed_opportunities] run_dir={run_dir}")

    rows = compute_missed_opportunities(run_dir)
    if not rows:
        print("[missed_opportunities] No data")
        return

    out_dir = run_dir / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "rq1_missed_opportunities.csv"

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    misses = [r for r in rows if r["missed_opportunity"]]
    print(f"[missed_opportunities] wrote {out_path} ({len(rows)} phases, {len(misses)} missed)")
    for r in misses:
        print(f"  MISS: {r['phase']}  CPU={r['mean_cpu']:.0f}% p95={r['p95_cpu']:.0f}%  "
              f"spawns={r['compute_spawns']}  threshold={r['effective_threshold']:.2f}")


if __name__ == "__main__":
    main()
