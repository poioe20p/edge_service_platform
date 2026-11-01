"""overhead — RQ1 controller CPU/RAM overhead plots.

Produces <run_dir>/analysis/rq1/:
  rq1_overhead_cpu.png   — controller CPU% over time
  rq1_overhead_ram.png   — controller RSS (MB) over time
  rq1_overhead.csv       — per-phase mean/p95 CPU% and RSS MB

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.overhead --run-dir <dir>

Note: requires ``controller_stats.csv`` produced by
``sample_controller_stats.py`` (Phase 5).
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

from ...loader import load_run
from ...phase_window import phase_boundaries, phase_for_ts
from ...plots import shade_phases


def _read_controller_stats(path: Path) -> list[dict]:
    """Read controller_stats.csv, parse numeric fields."""
    if not path.exists():
        print(f"[overhead] controller_stats.csv not found: {path}")
        return []
    rows: list[dict] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["_ts"] = _float(row.get("timestamp"))
            row["_cpu"] = _float(row.get("cpu_percent"))
            row["_mem"] = _float(row.get("mem_usage_mb"))
            if row["_ts"] is not None and row["_cpu"] is not None and row["_mem"] is not None:
                rows.append(row)
    return rows


def _float(v) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _compute_overhead(
    rows: list[dict],
    t0: float,
    phases: list,
) -> tuple[list[dict], list[dict]]:
    """Organise overhead data for plotting and per-phase aggregations.

    Returns (plot_points, per_phase_stats).
    """
    # ── Plot points ──
    points: list[dict] = []
    for row in rows:
        points.append({
            "t_s":          row["_ts"] - t0,
            "container":    row.get("container", ""),
            "cpu_percent":  row["_cpu"],
            "mem_usage_mb": row["_mem"],
        })

    # ── Per-phase aggregation ──
    # Group by (phase, container)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        phase = phase_for_ts(row["_ts"], t0, phases)
        key = f"{phase}_{row.get('container', 'unknown')}"
        groups[key].append(row)

    per_phase: list[dict] = []
    for key in sorted(groups):
        grp = groups[key]
        cpus = [r["_cpu"] for r in grp]
        mems = [r["_mem"] for r in grp]
        per_phase.append({
            "phase":        key,
            "count":        len(grp),
            "mean_cpu":     round(statistics.mean(cpus), 2),
            "p95_cpu":      round(sorted(cpus)[int(len(cpus) * 0.95)], 2) if len(cpus) > 1 else "",
            "mean_mem_mb":  round(statistics.mean(mems), 1),
            "p95_mem_mb":   round(sorted(mems)[int(len(mems) * 0.95)], 1) if len(mems) > 1 else "",
        })

    return points, per_phase


def _plot_overhead_cpu(
    points: list[dict],
    boundaries: list[tuple[str, float, float]],
    t0: float,
    out_dir: Path,
) -> None:
    if not points:
        print("[overhead] no overhead data to plot")
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 4))
    fig.suptitle("RQ1 — Controller CPU%", fontsize=12)

    containers = sorted({p["container"] for p in points})
    colors = {"osken": "#1a7abf", "osken_2": "#bf8c1a"}
    default_color = "#666666"

    for container in containers:
        subset = [p for p in points if p["container"] == container]
        x = [p["t_s"] for p in subset]
        y = [p["cpu_percent"] for p in subset]
        color = colors.get(container, default_color)
        ax.plot(x, y, "o-", label=container, color=color, linewidth=1.2,
                markersize=4, alpha=0.8)

    shade_phases(ax, boundaries, t0)
    ax.set_ylabel("CPU %")
    ax.set_xlabel("Experiment time (s)")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    fig.savefig(out_dir / "rq1_overhead_cpu.png", dpi=150)
    plt.close(fig)
    print(f"[overhead] wrote {out_dir / 'rq1_overhead_cpu.png'}")


def _plot_overhead_ram(
    points: list[dict],
    boundaries: list[tuple[str, float, float]],
    t0: float,
    out_dir: Path,
) -> None:
    if not points:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 4))
    fig.suptitle("RQ1 — Controller RSS (MB)", fontsize=12)

    containers = sorted({p["container"] for p in points})
    colors = {"osken": "#1a7abf", "osken_2": "#bf8c1a"}
    default_color = "#666666"

    for container in containers:
        subset = [p for p in points if p["container"] == container]
        x = [p["t_s"] for p in subset]
        y = [p["mem_usage_mb"] for p in subset]
        color = colors.get(container, default_color)
        ax.plot(x, y, "o-", label=container, color=color, linewidth=1.2,
                markersize=4, alpha=0.8)

    shade_phases(ax, boundaries, t0)
    ax.set_ylabel("RSS (MB)")
    ax.set_xlabel("Experiment time (s)")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    fig.savefig(out_dir / "rq1_overhead_ram.png", dpi=150)
    plt.close(fig)
    print(f"[overhead] wrote {out_dir / 'rq1_overhead_ram.png'}")


def _write_overhead_csv(per_phase: list[dict], out_dir: Path) -> None:
    path = out_dir / "rq1_overhead.csv"
    with path.open("w", newline="") as f:
        fieldnames = ["phase", "count", "mean_cpu", "p95_cpu", "mean_mem_mb", "p95_mem_mb"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(per_phase)
    print(f"[overhead] wrote {path}")


def run(run_dir: Path) -> None:
    print(f"[overhead] run_dir={run_dir}")
    r = load_run(run_dir)
    out_dir = Path(run_dir) / "analysis" / "rq1"
    out_dir.mkdir(parents=True, exist_ok=True)

    controller_stats_path = Path(run_dir) / "controller_stats.csv"
    raw = _read_controller_stats(controller_stats_path)
    if not raw:
        print("[overhead] no controller_stats.csv — overhead charts skipped")
        print("  (Requires Phase 5: sample_controller_stats.py)")
        return

    points, per_phase = _compute_overhead(raw, r.t0, r.phases)
    boundaries = phase_boundaries(r.t0, r.phases)

    _plot_overhead_cpu(points, boundaries, r.t0, out_dir)
    _plot_overhead_ram(points, boundaries, r.t0, out_dir)
    _write_overhead_csv(per_phase, out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RQ1 — controller CPU/RAM overhead analysis",
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run(args.run_dir)


if __name__ == "__main__":
    main()
