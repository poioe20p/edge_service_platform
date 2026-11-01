"""Collect per-run metrics for RQ1 experiments.

Computes reaction latency, staleness, failure rate, and mechanism exercise
from run artifacts and prints a summary table.

Usage:
    python -m source.scripts.testing.analysis.rq1.scripts.collect_metrics \
        --run-dir <dir1> [--run-dir <dir2> ...] [--per-phase] [--csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from ...client_status import is_timeout_legacy


def collect_run_metrics(run_dir: Path) -> dict:
    """Collect all metrics from a single run directory."""
    result = {
        "run_dir": run_dir.name,
        "reaction_events": 0,
        "reaction_mean_s": 0.0,
        "reaction_max_s": 0.0,
        "staleness_max_s": 0.0,
        "total_requests": 0,
        "timeouts": 0,
        "timeout_rate_pct": 0.0,
        "max_storage": 0,
        "max_server": 0,
        "final_phase": "unknown",
        "phases": [],
        "per_phase": {},
    }

    # --- Reaction latency ---
    rl_csv = run_dir / "analysis" / "rq1_reaction_latency.csv"
    if rl_csv.exists():
        with open(rl_csv) as f:
            rows = list(csv.DictReader(f))
        if rows:
            lats = [float(r["total_reaction_s"]) for r in rows]
            result["reaction_events"] = len(rows)
            result["reaction_mean_s"] = sum(lats) / len(lats)
            result["reaction_max_s"] = max(lats)

    # --- Staleness ---
    st_csv = run_dir / "analysis" / "rq1_staleness.csv"
    if st_csv.exists():
        with open(st_csv) as f:
            rows = list(csv.DictReader(f))
        if rows:
            result["staleness_max_s"] = max(float(r["staleness_s"]) for r in rows)

    # --- Failure rate ---
    cr_csv = run_dir / "client_requests.csv"
    if cr_csv.exists():
        with open(cr_csv) as f:
            rows = list(csv.DictReader(f))
        result["total_requests"] = len(rows)
        result["timeouts"] = sum(1 for r in rows if is_timeout_legacy(r))
        if result["total_requests"] > 0:
            result["timeout_rate_pct"] = (result["timeouts"] / result["total_requests"]) * 100

        # Per-phase
        phases: dict[str, dict] = {}
        for r in rows:
            ph = r.get("phase", "unknown")
            if ph not in phases:
                phases[ph] = {"total": 0, "timeouts": 0}
            phases[ph]["total"] += 1
            if is_timeout_legacy(r):
                phases[ph]["timeouts"] += 1
        for ph, d in phases.items():
            d["rate_pct"] = (d["timeouts"] / d["total"]) * 100 if d["total"] else 0
        result["per_phase"] = phases

    # --- Mechanism exercise ---
    rs_csv = run_dir / "resource_stats.csv"
    if rs_csv.exists():
        with open(rs_csv) as f:
            rows = list(csv.DictReader(f))
        if rows:
            result["max_storage"] = max(int(r.get("storage_count", 0)) for r in rows)
            result["max_server"] = max(int(r.get("server_count", 0)) for r in rows)

    cp_txt = run_dir / "current_phase.txt"
    if cp_txt.exists():
        result["final_phase"] = cp_txt.read_text().strip()

    ps_json = run_dir / "phases_snapshot.json"
    if ps_json.exists():
        with open(ps_json) as f:
            result["phases"] = [p["name"] for p in json.load(f).get("phases", [])]

    return result


PHASE_ORDER = [
    "baseline", "storage_storm", "tier1_hotspot",
    "inter_hotspot_cooldown", "reverse_hotspot",
    "compute_spike", "demand_drop",
]


def print_summary(metrics: list[dict], per_phase: bool = False) -> None:
    """Print a formatted summary table."""
    print(f"{'Run':<20s} {'Events':>6s} {'MeanLat':>8s} {'MaxLat':>8s} {'Stale':>8s} {'Timeouts':>12s} {'Phases':>8s}")
    print("-" * 78)
    for m in metrics:
        phases_ok = "✓" if m["final_phase"] == "idle" and len(m["phases"]) >= 7 else "✗"
        timeout_str = f"{m['timeout_rate_pct']:.1f}%"
        print(
            f"{m['run_dir']:<20s} "
            f"{m['reaction_events']:>6d} "
            f"{m['reaction_mean_s']:>8.1f}s "
            f"{m['reaction_max_s']:>8.1f}s "
            f"{m['staleness_max_s']:>8.2f}s "
            f"{timeout_str:>12s} "
            f"{phases_ok:>8s}"
        )

    if per_phase:
        print()
        print("Per-phase timeout rates:")
        header = f"{'Phase':<25s}"
        for m in metrics:
            header += f" {m['run_dir'][-12:]:>12s}"
        print(header)
        print("-" * (25 + 14 * len(metrics)))
        for ph in PHASE_ORDER:
            row = f"{ph:<25s}"
            for m in metrics:
                pp = m["per_phase"].get(ph, {})
                rate = pp.get("rate_pct", 0)
                row += f" {rate:>11.1f}%"
            print(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect RQ1 per-run metrics")
    parser.add_argument("--run-dir", action="append", dest="run_dirs", required=True,
                        help="Run directory (repeatable)")
    parser.add_argument("--per-phase", action="store_true",
                        help="Print per-phase timeout breakdown")
    parser.add_argument("--csv", action="store_true",
                        help="Output as CSV instead of table")
    args = parser.parse_args()

    metrics = [collect_run_metrics(Path(d)) for d in args.run_dirs]

    if args.csv:
        import io
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=[
            "run_dir", "reaction_events", "reaction_mean_s", "reaction_max_s",
            "staleness_max_s", "total_requests", "timeouts", "timeout_rate_pct",
            "max_storage", "max_server", "final_phase",
        ])
        writer.writeheader()
        for m in metrics:
            writer.writerow({k: m[k] for k in writer.fieldnames})
        print(out.getvalue())
    else:
        print_summary(metrics, per_phase=args.per_phase)


if __name__ == "__main__":
    main()
