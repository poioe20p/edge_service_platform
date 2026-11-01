"""overhead_compare — Cross-run CPU/RAM overhead comparison for RQ1.

Produces a grouped bar chart showing per-phase per-controller mean CPU %
across all four telemetry delivery modes (Push, Poll-5s, Poll-12s, Poll-30s).

Usage:
    python -m source.scripts.testing.analysis.rq1.cli.overhead_compare \
        --run-dirs <push_dir> <poll5_dir> <poll12_dir> <poll30_dir> \
        --output-dir <dir>
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np


def _read_overhead_csv(run_dir: str) -> dict[str, dict[str, float]]:
    """Return {phase_controller: {mean_cpu, p95_cpu, mean_mem, p95_mem}}."""
    path = os.path.join(run_dir, "analysis", "rq1", "rq1_overhead.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Overhead CSV not found: {path}")
    out: dict[str, dict[str, float]] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = row["phase"]
            out[key] = {
                "mean_cpu": float(row["mean_cpu"]),
                "p95_cpu": float(row["p95_cpu"]) if row["p95_cpu"] else 0.0,
                "mean_mem": float(row["mean_mem_mb"]),
                "p95_mem": float(row["p95_mem_mb"]) if row["p95_mem_mb"] else 0.0,
            }
    return out


def _build_comparison(runs: dict[str, str]) -> dict[str, dict[str, dict[str, float]]]:
    """Return {phase: {label: {mean_cpu, ...}}} across all runs."""
    phases_order = [
        "baseline_osken", "baseline_osken_2",
        "local_moderate_osken", "local_moderate_osken_2",
        "storage_stress_osken", "storage_stress_osken_2",
        "cross_region_hotspot_osken", "cross_region_hotspot_osken_2",
        "inter_hotspot_cooldown_osken", "inter_hotspot_cooldown_osken_2",
        "reverse_hotspot_osken", "reverse_hotspot_osken_2",
        "compute_ramp_osken", "compute_ramp_osken_2",
        "compute_spike_osken", "compute_spike_osken_2",
        "sustained_plateau_osken", "sustained_plateau_osken_2",
        "demand_drop_osken", "demand_drop_osken_2",
    ]
    all_data: dict[str, dict[str, dict[str, float]]] = {}
    for label, run_dir in runs.items():
        run_data = _read_overhead_csv(run_dir)
        for phase, metrics in run_data.items():
            all_data.setdefault(phase, {})[label] = metrics

    # Only keep phases present in at least one run
    return {p: all_data[p] for p in phases_order if p in all_data}


def _plot_overhead_comparison(
    comparison: dict[str, dict[str, dict[str, float]]],
    labels: list[str],
    out_path: str,
) -> None:
    """Two-panel grouped bar chart: mean CPU % (top) and mean RAM MB (bottom)
    per phase per configuration across controllers."""
    phases = list(comparison.keys())
    n_phases = len(phases)
    n_configs = len(labels)

    fig, (ax_cpu, ax_ram) = plt.subplots(2, 1, figsize=(max(18, n_phases * 1.3), 12),
                                          sharex=True)

    x = np.arange(n_phases)
    width = 0.8 / n_configs
    colors = ["#2196F3", "#4CAF50", "#FF9800", "#F44336"]

    for i, label in enumerate(labels):
        cpu_means = [comparison[p].get(label, {}).get("mean_cpu", 0) for p in phases]
        ram_means = [comparison[p].get(label, {}).get("mean_mem", 0) for p in phases]
        ax_cpu.bar(x + i * width, cpu_means, width, label=label, color=colors[i % len(colors)],
                   edgecolor="black", linewidth=0.8)
        ax_ram.bar(x + i * width, ram_means, width, label=label, color=colors[i % len(colors)],
                   edgecolor="black", linewidth=0.8)

    # Phase labels — strip _osken / _osken_2 suffix for readability
    short_phases = [p.replace("_osken_2", " (C2)").replace("_osken", " (C1)") for p in phases]
    ax_ram.set_xticks(x + width * (n_configs - 1) / 2)
    ax_ram.set_xticklabels(short_phases, rotation=45, ha="right", fontsize=8)

    ax_cpu.set_ylabel("Mean CPU %")
    ax_cpu.set_title("RQ1 v2 — Controller Overhead Comparison")
    ax_cpu.legend(fontsize=9)
    ax_cpu.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f%%"))
    ax_cpu.grid(axis="y", alpha=0.3)

    ax_ram.set_ylabel("Mean RAM (MB)")
    ax_ram.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.0f"))
    ax_ram.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[overhead_compare] wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="RQ1 cross-run CPU overhead comparison")
    parser.add_argument("--run-dir", action="append", dest="run_dirs", required=True,
                        help="Run directory (repeat for each config)")
    parser.add_argument("--labels", nargs="*", default=None,
                        help="Labels for each run (default: derived from folder name)")
    parser.add_argument("--output-dir", required=True, help="Output directory for PNG")
    args = parser.parse_args()

    if len(args.run_dirs) < 2:
        parser.error("Need at least 2 --run-dir arguments")

    # Auto-labels from folder name if not provided
    if args.labels and len(args.labels) == len(args.run_dirs):
        labels = args.labels
    else:
        labels = []
        for d in args.run_dirs:
            name = os.path.basename(os.path.normpath(d))
            # Extract mode from folder name: rq1_eval_push -> Push, rq1_eval_poll5 -> Poll-5s, etc.
            if "push" in name:
                labels.append("Push")
            elif "poll5" in name:
                labels.append("Poll-5s")
            elif "poll12" in name:
                labels.append("Poll-12s")
            elif "poll30" in name:
                labels.append("Poll-30s")
            else:
                labels.append(name[-20:])  # fallback

    runs = dict(zip(labels, args.run_dirs))
    print(f"[overhead_compare] comparing {len(runs)} runs: {list(runs.keys())}")

    comparison = _build_comparison(runs)
    print(f"[overhead_compare] {len(comparison)} phases with overhead data")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _plot_overhead_comparison(comparison, labels, str(out_dir / "rq1_overhead_compare.png"))


if __name__ == "__main__":
    main()
