#!/usr/bin/env python3
"""G9–G13 Mechanism Graphs for RQ2 v5.

Generates four mechanism-level graphs that test predictions from rq2_v5.md §5.5
beyond the standard 12-graph campaign set.

Usage (on cloud-vm):
    python3 source/scripts/testing/analysis/rq2/g9_g13_mechanism_graphs.py \
      --run th_1:topology_host:source/scripts/testing/metrics/<folder> \
      ... (9 runs) ...
      --out-dir docs/operation/testing/experiment/rq2_evaluation/v5/graphs

Data sources per graph:
  G9  — rq2_spawn_metrics.csv (spawn_ts) + per_node_stats.csv (cpu_percent,window_end)
  G10 — client_requests.csv (latency_s, phase)
  G12 — rq2_spawn_metrics.csv (spawn_ts, ttft_s, tfr_s)
  G13 — client_requests.csv (phase, count requests)
"""

from __future__ import annotations

import argparse
import csv
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


def load_spawn_csv(run_dir: Path) -> list[dict]:
    p = run_dir / "analysis" / "rq2_spawn_metrics.csv"
    rows = []
    if p.exists():
        with open(p) as f:
            for row in csv.DictReader(f):
                rows.append(row)
    return rows


def load_per_node_csv(run_dir: Path) -> list[dict]:
    p = run_dir / "per_node_stats.csv"
    rows = []
    if p.exists():
        with open(p) as f:
            for row in csv.DictReader(f):
                rows.append(row)
    return rows


def load_client_csv(run_dir: Path) -> list[dict]:
    p = run_dir / "client_requests.csv"
    rows = []
    if p.exists():
        with open(p) as f:
            for row in csv.DictReader(f):
                rows.append(row)
    return rows


def sf(v):
    """Safe float parser."""
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _style_bar(ax, x, labels, ylabel, title):
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=LABEL_SIZE, rotation=0)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)


def _add_scatter(ax, x_positions, per_mode_data):
    """Add scatter dots for per-replicate variance."""
    for i, (x, vals) in enumerate(zip(x_positions, per_mode_data)):
        if vals:
            jitter = np.random.default_rng(42).uniform(-0.15, 0.15, len(vals))
            ax.scatter([x] * len(vals) + jitter, vals, color="black", alpha=0.3, s=15, zorder=3)


def main():
    ap = argparse.ArgumentParser(description="G9–G13 mechanism graphs")
    ap.add_argument("--run", action="append", dest="runs", default=[],
                    help="Format: label:mode:run_dir (repeatable)")
    ap.add_argument("--out-dir", required=True, help="Output directory for PNGs")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Parse --run arguments
    run_specs = []
    for r in args.runs:
        label, mode, d = r.split(":", 2)
        run_specs.append((label, mode, Path(d)))

    print(f"Loaded {len(run_specs)} runs")

    # ═══════════════════════════════════════════════════════════════
    # Phase durations (from phases_rq2.json — hardcoded for simplicity)
    # ═══════════════════════════════════════════════════════════════
    PHASE_DURATIONS = {
        "baseline": 60, "storage_storm": 240, "cooldown_1": 180,
        "compute_spike": 180, "cooldown_2": 180, "storage_storm_2": 240,
        "cooldown_3": 180, "compute_spike_2": 180, "demand_drop": 300,
    }
    PHASE_ORDER = list(PHASE_DURATIONS.keys())

    # ═══════════════════════════════════════════════════════════════
    # Load all data
    # ═══════════════════════════════════════════════════════════════
    all_spawns = []
    all_clients = []
    all_per_node = []

    for label, mode, d in run_specs:
        for row in load_spawn_csv(d):
            row["_mode"] = mode
            row["_label"] = label
            all_spawns.append(row)
        for row in load_client_csv(d):
            row["_mode"] = mode
            row["_label"] = label
            all_clients.append(row)
        for row in load_per_node_csv(d):
            row["_mode"] = mode
            row["_label"] = label
            all_per_node.append(row)

    print(f"  Spawns: {len(all_spawns)}  Clients: {len(all_clients)}  Per-node: {len(all_per_node)}")

    # ═══════════════════════════════════════════════════════════════
    # G9 — CPU Relief After Spawn
    # ═══════════════════════════════════════════════════════════════
    print("Generating G9 — CPU Relief After Spawn ...")
    mode_relief = {m: [] for m in MODE_ORDER}

    for spawn in all_spawns:
        mode = spawn["_mode"]
        spawn_ts = sf(spawn["spawn_ts"])
        container = spawn.get("container", "")
        if spawn_ts is None:
            continue

        # Find per-node windows for compute backends in the same mode/run
        windows = []
        run_label = spawn["_label"]
        for pn in all_per_node:
            if pn["_mode"] != mode or pn["_label"] != run_label:
                continue
            we = sf(pn["window_end"])
            cpu = sf(pn["cpu_percent"])
            role = pn.get("role", "")
            if we is None or cpu is None:
                continue
            if role != "compute":
                continue
            windows.append((we, cpu))

        if not windows:
            continue

        # Find existing backends: those appearing in windows before spawn
        # that are NOT the newly spawned container
        before_cpus = []
        after_cpus = []

        # Group by server_id to find existing backends
        existing_ids = set()
        for we, sid in [(sf(pn["window_end"]), pn.get("server_id", ""))
                        for pn in all_per_node
                        if pn["_mode"] == mode and pn["_label"] == run_label]:
            if we is not None and we < spawn_ts:
                existing_ids.add(sid)

        # Get CPU of existing backends in 3 windows before spawn
        before_windows = sorted([w for w in windows if w[0] < spawn_ts], key=lambda x: x[0])
        before_windows = before_windows[-3:]  # last 3 windows before spawn

        before_avg = [w[1] for w in before_windows]

        # Get CPU in 3 windows after spawn
        after_windows = sorted([w for w in windows if w[0] >= spawn_ts], key=lambda x: x[0])
        after_windows = after_windows[:3]  # first 3 windows after spawn

        after_avg = [w[1] for w in after_windows]

        if before_avg and after_avg:
            relief = np.mean(before_avg) - np.mean(after_avg)
            mode_relief[mode].append(relief)

    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(len(MODE_ORDER))
    means = [np.mean(mode_relief[m]) if mode_relief[m] else 0 for m in MODE_ORDER]
    sems = [np.std(mode_relief[m]) / max(np.sqrt(len(mode_relief[m])), 1) if mode_relief[m] else 0 for m in MODE_ORDER]
    colors = [MODE_COLORS[m] for m in MODE_ORDER]
    bars = ax.bar(x, means, 0.5, yerr=sems, color=colors, capsize=5, edgecolor="black", linewidth=0.5)
    _add_scatter(ax, x, [mode_relief[m] for m in MODE_ORDER])
    _style_bar(ax, x, [MODE_LABEL[m] for m in MODE_ORDER],
               "CPU Relief (pp)", "G9 — CPU Relief After Spawn by Mode\n(mean CPU drop in 3 windows after spawn − before)")
    ax.axhline(y=0, color="black", linewidth=0.5, linestyle="--")
    for bar, s in zip(bars, [len(mode_relief[m]) for m in MODE_ORDER]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1, f"n={s}",
                ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "g9_cpu_relief.png", dpi=150)
    plt.close(fig)
    print("  ✓ G9")

    # ═══════════════════════════════════════════════════════════════
    # G10 — Per-Phase p95 Latency
    # ═══════════════════════════════════════════════════════════════
    print("Generating G10 — Per-Phase p95 Latency ...")
    phase_p95 = {m: {p: [] for p in PHASE_ORDER} for m in MODE_ORDER}

    for row in all_clients:
        if not is_completed(row):
            continue
        mode = row["_mode"]
        phase = row.get("phase", "")
        lat = sf(row.get("latency_s", ""))
        if lat is None or phase not in PHASE_ORDER:
            continue
        phase_p95[mode][phase].append(lat)

    fig, ax = plt.subplots(figsize=(14, 5))
    n_phases = len(PHASE_ORDER)
    n_modes = len(MODE_ORDER)
    bar_width = 0.22
    x = np.arange(n_phases)

    for i, mode in enumerate(MODE_ORDER):
        p95s = [np.percentile(phase_p95[mode][p], 95) if phase_p95[mode][p] else 0 for p in PHASE_ORDER]
        offset = (i - 1) * bar_width
        ax.bar(x + offset, p95s, bar_width, color=MODE_COLORS[mode], label=MODE_LABEL[mode],
               edgecolor="black", linewidth=0.5)

    ax.set_xticks(x)
    ax.set_xticklabels([p.replace("_", "\n") for p in PHASE_ORDER], fontsize=7, rotation=0)
    ax.set_ylabel("p95 Latency (s)", fontsize=LABEL_SIZE)
    ax.set_title("G10 — Per-Phase p95 Latency by Mode", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "g10_per_phase_p95.png", dpi=150)
    plt.close(fig)
    print("  ✓ G10")

    # ═══════════════════════════════════════════════════════════════
    # G12 — TTFT/TFR by Spawn Order
    # ═══════════════════════════════════════════════════════════════
    print("Generating G12 — TTFT/TFR by Spawn Order ...")

    # Within each run, sort spawns by spawn_ts to get ordinal
    run_spawns = defaultdict(list)
    for spawn in all_spawns:
        run_spawns[spawn["_label"]].append(spawn)
    for label in run_spawns:
        run_spawns[label].sort(key=lambda s: sf(s["spawn_ts"]) or 0)

    # Collect: (ordinal, mode, ttft_s, tfr_s)
    ordinal_data = []
    for label, spawns in run_spawns.items():
        for i, s in enumerate(spawns):
            ttft = sf(s.get("ttft_s", ""))
            tfr = sf(s.get("tfr_s", ""))
            if ttft is not None:
                ordinal_data.append((i + 1, s["_mode"], ttft, tfr))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    for mode in MODE_ORDER:
        mode_points = [(o, ttft) for o, m, ttft, _ in ordinal_data if m == mode and ttft is not None]
        if mode_points:
            xs, ys = zip(*mode_points)
            ax1.scatter(xs, ys, color=MODE_COLORS[mode], alpha=0.6, s=30, label=MODE_LABEL[mode])
        mode_tfr = [(o, tfr) for o, m, _, tfr in ordinal_data if m == mode and tfr is not None]
        if mode_tfr:
            xs, ys = zip(*mode_tfr)
            ax2.scatter(xs, ys, color=MODE_COLORS[mode], alpha=0.6, s=30, label=MODE_LABEL[mode])

    for ax, ylabel, title in [(ax1, "TTFT (s)", "G12a — TTFT by Spawn Order"),
                                (ax2, "TFR (s)", "G12b — TFR by Spawn Order")]:
        ax.set_xlabel("Spawn Ordinal (1st, 2nd, …)", fontsize=LABEL_SIZE)
        ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
        ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_dir / "g12_spawn_order.png", dpi=150)
    plt.close(fig)
    print("  ✓ G12")

    # ═══════════════════════════════════════════════════════════════
    # G13 — Throughput by Phase
    # ═══════════════════════════════════════════════════════════════
    print("Generating G13 — Throughput by Phase ...")
    phase_counts = {m: {p: 0 for p in PHASE_ORDER} for m in MODE_ORDER}
    run_phase_counts = defaultdict(lambda: defaultdict(int))

    for row in all_clients:
        mode = row["_mode"]
        phase = row.get("phase", "")
        label = row["_label"]
        if phase not in PHASE_ORDER:
            continue
        phase_counts[mode][phase] += 1
        run_phase_counts[(mode, label)][phase] += 1

    # Compute per-replicate throughput (req/s)
    n_reps = defaultdict(int)
    for (mode, label) in run_phase_counts:
        n_reps[mode] += 1

    fig, ax = plt.subplots(figsize=(14, 5))
    x = np.arange(n_phases)

    for i, mode in enumerate(MODE_ORDER):
        throughputs = []
        for p in PHASE_ORDER:
            dur = PHASE_DURATIONS.get(p, 60)
            tput = phase_counts[mode][p] / max(n_reps[mode], 1) / dur
            throughputs.append(tput)
        offset = (i - 1) * bar_width
        ax.bar(x + offset, throughputs, bar_width, color=MODE_COLORS[mode], label=MODE_LABEL[mode],
               edgecolor="black", linewidth=0.5)

    ax.set_xticks(x)
    ax.set_xticklabels([p.replace("_", "\n") for p in PHASE_ORDER], fontsize=7, rotation=0)
    ax.set_ylabel("Requests / second", fontsize=LABEL_SIZE)
    ax.set_title("G13 — Throughput by Phase and Mode\n(mean req/s per replicate run)", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "g13_throughput.png", dpi=150)
    plt.close(fig)
    print("  ✓ G13")

    print("\n=== G9–G13 Complete ===")


if __name__ == "__main__":
    main()
