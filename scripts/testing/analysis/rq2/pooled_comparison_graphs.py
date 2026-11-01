#!/usr/bin/env python3
"""Generate pooled cross-mode comparison graphs: throughput, p50, p95.

Usage (on cloud-vm):
    python3 source/scripts/testing/analysis/rq2/pooled_comparison_graphs.py \
      --run ... (9 runs) ... --out-dir <dir>
"""

from __future__ import annotations

import argparse, csv
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:  # package-module invocation (python -m ...)
    from ..client_status import is_completed
except ImportError:  # plain-script invocation (python3 path/to/file.py)
    def is_completed(row, header=None):  # type: ignore
        if "status" not in row:
            return True
        return str(row.get("status", "") or "").strip().lower() == "completed"

MODE_ORDER = ["topology_host", "topology_slowstart", "topology_lifecycle"]
MODE_LABEL = {"topology_host": "Host", "topology_slowstart": "Slowstart", "topology_lifecycle": "Lifecycle"}
MODE_COLORS = {"topology_host": "#2196F3", "topology_slowstart": "#FF9800", "topology_lifecycle": "#4CAF50"}
TITLE_SIZE = 11
LABEL_SIZE = 9
BAR_WIDTH = 0.5


def sf(v):
    try: return float(v)
    except: return None


def _add_scatter(ax, x_positions, per_mode_data):
    rng = np.random.default_rng(42)
    for x, vals in zip(x_positions, per_mode_data):
        if vals:
            jitter = rng.uniform(-0.12, 0.12, len(vals))
            ax.scatter([x]*len(vals) + jitter, vals, color="black", alpha=0.4, s=20, zorder=3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", dest="runs", default=[])
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Parse
    run_specs = []
    for r in args.runs:
        label, mode, d = r.split(":", 2)
        run_specs.append((label, mode, Path(d)))

    # Phase durations
    PHASE_DUR = {"baseline": 60, "storage_storm": 240, "cooldown_1": 180,
                 "compute_spike": 180, "cooldown_2": 180, "storage_storm_2": 240,
                 "cooldown_3": 180, "compute_spike_2": 180, "demand_drop": 300}

    # ── Load ──
    per_run_lat = defaultdict(list)          # (mode,label) -> [latency_s...]
    per_run_reqs = defaultdict(int)          # (mode,label) -> total requests
    per_run_duration = defaultdict(float)    # (mode,label) -> total duration

    for label, mode, d in run_specs:
        cr = d / "client_requests.csv"
        if cr.exists():
            with open(cr) as f:
                for row in csv.DictReader(f):
                    lat = sf(row.get("latency_s", ""))
                    phase = row.get("phase", "")
                    if lat is not None and is_completed(row):
                        per_run_lat[(mode, label)].append(lat)
                    per_run_reqs[(mode, label)] += 1
                    per_run_duration[(mode, label)] += PHASE_DUR.get(phase, 0)

    # ── Aggregate per mode ──
    mode_p50 = {m: [] for m in MODE_ORDER}
    mode_p95 = {m: [] for m in MODE_ORDER}
    mode_tput = {m: [] for m in MODE_ORDER}

    for (mode, label) in per_run_lat:
        lats = per_run_lat[(mode, label)]
        if lats:
            mode_p50[mode].append(np.percentile(lats, 50) * 1000)  # ms
            mode_p95[mode].append(np.percentile(lats, 95) * 1000)
        dur = per_run_duration.get((mode, label), 1)
        reqs = per_run_reqs.get((mode, label), 0)
        if dur > 0:
            mode_tput[mode].append(reqs / dur)

    # ═══════════════════════════════════════
    # G14 — Pooled Throughput by Mode
    # ═══════════════════════════════════════
    fig, ax = plt.subplots(figsize=(6, 4.5))
    x = np.arange(len(MODE_ORDER))
    means = [np.mean(mode_tput[m]) if mode_tput[m] else 0 for m in MODE_ORDER]
    colors = [MODE_COLORS[m] for m in MODE_ORDER]
    bars = ax.bar(x, means, BAR_WIDTH, color=colors, edgecolor="black", linewidth=0.5)
    _add_scatter(ax, x, [mode_tput[m] for m in MODE_ORDER])
    ax.set_xticks(x)
    ax.set_xticklabels([MODE_LABEL[m] for m in MODE_ORDER], fontsize=LABEL_SIZE)
    ax.set_ylabel("Throughput (req/s)", fontsize=LABEL_SIZE)
    ax.set_title("G14 — Pooled Throughput by Mode\n(mean req/s across all phases, n=3 replicates)", fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    for bar, m in zip(bars, MODE_ORDER):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                f"{means[MODE_ORDER.index(m)]:.1f}", ha="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "g14_throughput_pooled.png", dpi=150)
    plt.close(fig)
    print("✓ G14 — Pooled Throughput")

    # ═══════════════════════════════════════
    # G15 — Pooled p50 Latency by Mode
    # ═══════════════════════════════════════
    fig, ax = plt.subplots(figsize=(6, 4.5))
    means = [np.mean(mode_p50[m]) if mode_p50[m] else 0 for m in MODE_ORDER]
    sems = [np.std(mode_p50[m]) / max(np.sqrt(len(mode_p50[m])), 1) if mode_p50[m] else 0 for m in MODE_ORDER]
    bars = ax.bar(x, means, BAR_WIDTH, yerr=sems, color=colors, capsize=5, edgecolor="black", linewidth=0.5)
    _add_scatter(ax, x, [mode_p50[m] for m in MODE_ORDER])
    ax.set_xticks(x)
    ax.set_xticklabels([MODE_LABEL[m] for m in MODE_ORDER], fontsize=LABEL_SIZE)
    ax.set_ylabel("p50 Latency (ms)", fontsize=LABEL_SIZE)
    ax.set_title("G15 — Pooled p50 Latency by Mode\n(all phases, n=3 replicates, error bars = SEM)", fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    for bar, m in zip(bars, MODE_ORDER):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 2,
                f"{means[MODE_ORDER.index(m)]:.1f}", ha="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "g15_p50_pooled.png", dpi=150)
    plt.close(fig)
    print("✓ G15 — Pooled p50 Latency")

    # ═══════════════════════════════════════
    # G16 — Pooled p95 Latency by Mode
    # ═══════════════════════════════════════
    fig, ax = plt.subplots(figsize=(6, 4.5))
    means = [np.mean(mode_p95[m]) if mode_p95[m] else 0 for m in MODE_ORDER]
    sems = [np.std(mode_p95[m]) / max(np.sqrt(len(mode_p95[m])), 1) if mode_p95[m] else 0 for m in MODE_ORDER]
    bars = ax.bar(x, means, BAR_WIDTH, yerr=sems, color=colors, capsize=5, edgecolor="black", linewidth=0.5)
    _add_scatter(ax, x, [mode_p95[m] for m in MODE_ORDER])
    ax.set_xticks(x)
    ax.set_xticklabels([MODE_LABEL[m] for m in MODE_ORDER], fontsize=LABEL_SIZE)
    ax.set_ylabel("p95 Latency (ms)", fontsize=LABEL_SIZE)
    ax.set_title("G16 — Pooled p95 Latency by Mode\n(all phases, n=3 replicates, error bars = SEM)", fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    for bar, m in zip(bars, MODE_ORDER):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                f"{means[MODE_ORDER.index(m)]:.1f}", ha="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "g16_p95_pooled.png", dpi=150)
    plt.close(fig)
    print("✓ G16 — Pooled p95 Latency")


if __name__ == "__main__":
    main()
