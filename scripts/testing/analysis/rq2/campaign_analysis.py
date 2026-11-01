#!/usr/bin/env python3
"""RQ2 v3 campaign aggregation and thesis graph generation.

Aggregates per-run rq2_spawn_metrics.csv and client_requests.csv across all
runs, then generates 11 thesis graphs matching the RQ1 v8 visual style.

Usage:
    python -m source.scripts.testing.analysis.rq2.campaign_analysis \
        --run label:mode:path/to/run_folder ... \
        --out-dir path/to/graphs
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from ..client_status import is_completed

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

MODE_LABELS = ["Host", "Slowstart", "Lifecycle"]
MODE_ORDER = ["topology_host", "topology_slowstart", "topology_lifecycle"]
MODE_COLORS = ["#F44336", "#FF9800", "#4CAF50"]  # red, orange, green
MODE_MARKERS = ["^", "s", "o"]  # triangle, square, circle — B&W-distinguishable
MODE_EDGECOLORS = ["#B71C1C", "#E65100", "#1B5E20"]
FIG_SINGLE = (10, 6)
FIG_WIDE = (14, 6)
TITLE_SIZE = 13
LABEL_SIZE = 12
TICK_SIZE = 11
ANNO_SIZE = 10
DOT_SIZE = 55
BAR_ALPHA = 0.78
GRID_ALPHA = 0.22
RNG = np.random.default_rng(42)

NON_STRESS_PHASES = ["baseline", "cooldown_1", "cooldown_2", "cooldown_3", "demand_drop"]
STORAGE_PHASES = ["storage_storm", "storage_storm_2"]
COMPUTE_PHASES = ["compute_spike", "compute_spike_2"]
ALL_PHASES = ["baseline", "storage_storm", "cooldown_1", "compute_spike",
              "cooldown_2", "storage_storm_2", "cooldown_3", "compute_spike_2", "demand_drop"]


def load_spawn_metrics(run_dir: Path, mode: str) -> list[dict]:
    p = run_dir / "analysis" / "rq2_spawn_metrics.csv"
    if not p.exists():
        return []
    rows = []
    with open(p) as f:
        for row in csv.DictReader(f):
            row["run"] = run_dir.name
            row["mode"] = mode
            rows.append(row)
    return rows


def load_client_requests(run_dir: Path, mode: str) -> list[dict]:
    p = run_dir / "client_requests.csv"
    if not p.exists():
        return []
    rows = []
    with open(p) as f:
        for row in csv.DictReader(f):
            row["run"] = run_dir.name
            row["mode"] = mode
            rows.append(row)
    return rows


def _style_bar_ax(ax, x, labels, ylabel, title):
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=TICK_SIZE)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _add_scatter_dots(ax, x, per_mode_vals):
    """Add per-replicate jittered scatter dots over bars."""
    for i, vals in enumerate(per_mode_vals):
        if vals and len(vals) > 0:
            jitter = RNG.uniform(-0.13, 0.13, len(vals))
            ax.scatter(
                np.full(len(vals), x[i]) + jitter, vals,
                color="black", s=DOT_SIZE, zorder=5,
                edgecolors="white", linewidth=1,
            )


def _boxplot_v8(ax, data_per_mode, ylabel, title):
    """Draw box plot with scatter dots in v8 style."""
    modes_present = [m for m in MODE_ORDER if m in data_per_mode and data_per_mode[m]]
    if not modes_present:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, fontsize=14)
        ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
        return
    positions = list(range(len(modes_present)))
    bp = ax.boxplot(
        [data_per_mode[m] for m in modes_present],
        positions=positions, widths=0.45, patch_artist=True,
        medianprops={"color": "black", "linewidth": 2},
        flierprops={"marker": "o", "markersize": 4, "alpha": 0.5},
    )
    for i, mode in enumerate(modes_present):
        bp["boxes"][i].set_facecolor(MODE_COLORS[MODE_ORDER.index(mode)])
        bp["boxes"][i].set_alpha(BAR_ALPHA)
        vals = data_per_mode[mode]
        jitter = RNG.uniform(-0.10, 0.10, len(vals))
        ax.scatter(
            np.full(len(vals), positions[i]) + jitter, vals,
            color="black", s=DOT_SIZE * 0.6, zorder=5, alpha=0.55,
            edgecolors="white", linewidth=0.5,
        )
        ax.annotate(f"n={len(vals)}", (positions[i], ax.get_ylim()[1] * 0.95 if ax.get_ylim()[1] > 0 else 0),
                    ha="center", va="top", fontsize=8, color="#555555")
    ax.set_xticks(positions)
    ax.set_xticklabels([MODE_LABELS[MODE_ORDER.index(m)] for m in modes_present], fontsize=TICK_SIZE)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def main():
    parser = argparse.ArgumentParser(description="RQ2 v3 campaign analysis")
    parser.add_argument("--run", action="append", dest="runs", default=[])
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    if not args.runs:
        print("ERROR: at least one --run required")
        return 1

    run_specs = []
    for spec in args.runs:
        parts = spec.split(":", 2)
        if len(parts) != 3:
            print(f"ERROR: invalid run spec '{spec}'")
            return 1
        run_specs.append((parts[0], parts[1], Path(parts[2])))

    args.out_dir.mkdir(parents=True, exist_ok=True)

    all_spawns: list[dict] = []
    run_spawns: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for label, mode, path in run_specs:
        rows = load_spawn_metrics(path, mode)
        print(f"{label} ({mode}): {len(rows)} spawn events")
        all_spawns.extend(rows)
        run_spawns[mode][label] = rows

    all_requests: list[dict] = []
    for label, mode, path in run_specs:
        rows = load_client_requests(path, mode)
        all_requests.extend(rows)

    if not all_spawns:
        print("ERROR: no spawn metrics")
        return 1

    def mode_vals(key):
        d: dict[str, list[float]] = defaultdict(list)
        for s in all_spawns:
            if s.get(key):
                d[s["mode"]].append(float(s[key]))
        return d

    ttft_data = mode_vals("ttft_s")
    tfr_data = mode_vals("tfr_s")
    init_data = mode_vals("init_time_s")
    share_data = mode_vals("initial_share")

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _boxplot_v8(ax, ttft_data, "TTFT (s)", "G1 — TTFT Distribution by Mode")
    fig.tight_layout()
    fig.savefig(args.out_dir / "g1_ttft.png", dpi=150)
    plt.close(fig)
    print("✓ G1")

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _boxplot_v8(ax, tfr_data, "TFR (s)", "G2 — TFR Distribution by Mode")
    fig.tight_layout()
    fig.savefig(args.out_dir / "g2_tfr.png", dpi=150)
    plt.close(fig)
    print("✓ G2")

    fig, ax = plt.subplots(figsize=(9, 7))
    for mode in MODE_ORDER:
        xs = [float(s["ttft_s"]) for s in all_spawns if s["mode"] == mode and s.get("ttft_s") and s.get("tfr_s")]
        ys = [float(s["tfr_s"]) for s in all_spawns if s["mode"] == mode and s.get("ttft_s") and s.get("tfr_s")]
        if xs:
            idx = MODE_ORDER.index(mode)
            ax.scatter(xs, ys, color=MODE_COLORS[idx], marker=MODE_MARKERS[idx], alpha=0.65, s=DOT_SIZE * 1.3,
                       label=MODE_LABELS[idx], edgecolors=MODE_EDGECOLORS[idx], linewidth=1.2)
    lims = [0, max(ax.get_xlim()[1], ax.get_ylim()[1])]
    ax.plot(lims, lims, "k--", alpha=0.25, linewidth=1, label="TFR = TTFT")
    ax.set_xlabel("TTFT (s)", fontsize=LABEL_SIZE)
    ax.set_ylabel("TFR (s)", fontsize=LABEL_SIZE)
    ax.set_title("G2b — TTFT vs TFR by Mode\n(diagonal = backend ready when traffic arrived)", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g2b_ttft_vs_tfr.png", dpi=150)
    plt.close(fig)
    print("✓ G2b")

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _boxplot_v8(ax, init_data, "Init Time (s)", "G3 — Backend Initialisation Time (TFR − TTFT) by Mode")
    fig.tight_layout()
    fig.savefig(args.out_dir / "g3_init_time.png", dpi=150)
    plt.close(fig)
    print("✓ G3")

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    _boxplot_v8(ax, share_data, "Initial Load Share",
                "G4 — Initial Load Share Distribution by Mode\n(fraction of VIP traffic in first ~10 s telemetry window)")
    # Annotate median pool size per mode (align with _boxplot_v8 positions)
    modes_present = [m for m in MODE_ORDER if m in share_data and share_data[m]]
    for i, mode in enumerate(modes_present):
        psz = [int(s["pool_size"]) for s in all_spawns
               if s["mode"] == mode and s.get("pool_size") and s.get("initial_share")]
        if psz:
            med_ps = np.median(psz)
            ax.annotate(f"pool≈{med_ps:.0f}", (i, 0.02), ha="center", va="bottom",
                       fontsize=8, color="#333333", style="italic")
    fig.tight_layout()
    fig.savefig(args.out_dir / "g4_initial_share.png", dpi=150)
    plt.close(fig)
    print("✓ G4")

    fig, ax = plt.subplots(figsize=(9, 7))
    for mode in MODE_ORDER:
        spawns_mode = [s for s in all_spawns if s["mode"] == mode and s.get("ttft_s") and s.get("initial_share")]
        xs = [float(s["ttft_s"]) for s in spawns_mode]
        ys = [float(s["initial_share"]) for s in spawns_mode]
        psz = [int(s["pool_size"]) if s.get("pool_size") else 0 for s in spawns_mode]
        if xs:
            idx = MODE_ORDER.index(mode)
            # Scale pool_size to dot area: 2 nodes → 40, 20 nodes → 350
            sizes = [max(35, min(350, 20 + (p - 1) * 18)) if p > 0 else 60 for p in psz]
            ax.scatter(xs, ys, color=MODE_COLORS[idx], marker=MODE_MARKERS[idx], alpha=0.65, s=sizes,
                       label=MODE_LABELS[idx], edgecolors=MODE_EDGECOLORS[idx], linewidth=1.2)
    ax.set_xlabel("TTFT (s)", fontsize=LABEL_SIZE)
    ax.set_ylabel("Initial Load Share", fontsize=LABEL_SIZE)
    ax.set_title("G4b — TTFT vs Initial Load Share by Mode\n(dot size = pool size: more backends → larger dot → share is diluted)",
                 fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g4b_ttft_vs_share.png", dpi=150)
    plt.close(fig)
    print("✓ G4b")

    # per_run_lat[mode][run][phase] = list of latencies (ms)
    per_run_lat: dict[str, dict[str, dict[str, list[float]]]] = \
        defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in all_requests:
        if not is_completed(r):
            continue
        mode = r.get("mode", "")
        run_label = r.get("run", "")
        phase = r.get("phase", "")
        lat = float(r.get("latency_s", 0)) * 1000
        if mode and run_label and phase and lat >= 0:
            per_run_lat[mode][run_label][phase].append(lat)

    # Build per-mode per-phase lists of per-run p50 values (for grouped bars with scatter)
    def per_run_p50(mode, phase):
        result = []
        for run_label in per_run_lat.get(mode, {}):
            vals = per_run_lat[mode][run_label].get(phase, [])
            if vals:
                result.append(np.percentile(vals, 50))
        return result

    def per_run_p95(mode, phase):
        result = []
        for run_label in per_run_lat.get(mode, {}):
            vals = per_run_lat[mode][run_label].get(phase, [])
            if vals:
                result.append(np.percentile(vals, 95))
        return result

    g5_vals = [per_run_p50(m, "baseline") for m in MODE_ORDER]
    g5_means = [np.mean(v) if v else 0 for v in g5_vals]
    fig, ax = plt.subplots(figsize=(7, 6))
    x = np.arange(len(MODE_LABELS))
    _style_bar_ax(ax, x, MODE_LABELS, "p50 Latency (ms)", "G5 — Baseline p50 Latency by Mode")
    ax.bar(x, g5_means, color=MODE_COLORS, edgecolor="black", alpha=BAR_ALPHA, linewidth=1.2)
    _add_scatter_dots(ax, x, g5_vals)
    for i, (v, vals) in enumerate(zip(g5_means, g5_vals)):
        ax.text(i, v + max(1, v * 0.05), f"{v:.1f}", ha="center", fontweight="bold", fontsize=ANNO_SIZE)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g5_baseline_p50.png", dpi=150)
    plt.close(fig)
    print("✓ G5")

    fig, ax = plt.subplots(figsize=FIG_WIDE)
    x = np.arange(len(NON_STRESS_PHASES))
    n_modes = len(MODE_LABELS)
    width = 0.7 / n_modes
    for j, mode in enumerate(MODE_ORDER):
        means = []
        vals_per_phase = []
        for ph in NON_STRESS_PHASES:
            p50s = per_run_p50(mode, ph)
            vals_per_phase.append(p50s)
            means.append(np.mean(p50s) if p50s else 0)
        offset = (j - (n_modes - 1) / 2) * width
        ax.bar(x + offset, means, width, label=MODE_LABELS[j], color=MODE_COLORS[j],
               edgecolor="black", alpha=BAR_ALPHA, linewidth=0.8)
        for gi, (ph, p50s) in enumerate(zip(NON_STRESS_PHASES, vals_per_phase)):
            if p50s:
                jitter = RNG.uniform(-width * 0.3, width * 0.3, len(p50s))
                ax.scatter(np.full(len(p50s), x[gi] + offset) + jitter, p50s,
                           color="black", s=DOT_SIZE * 0.7, zorder=5, alpha=0.55,
                           edgecolors="white", linewidth=0.5)
    ax.set_xticks(x)
    ax.set_xticklabels([p.replace("_", "\n") for p in NON_STRESS_PHASES], fontsize=TICK_SIZE - 1)
    ax.set_ylabel("p50 Latency (ms)", fontsize=LABEL_SIZE)
    ax.set_title("G5b — Non-Stress p50 Latency by Mode", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g5b_nonstress_p50.png", dpi=150)
    plt.close(fig)
    print("✓ G5b")

    fig, ax = plt.subplots(figsize=(18, 7))
    x = np.arange(len(ALL_PHASES))
    width = 0.7 / n_modes
    for j, mode in enumerate(MODE_ORDER):
        means = []
        vals_per_phase = []
        for ph in ALL_PHASES:
            p50s = per_run_p50(mode, ph)
            vals_per_phase.append(p50s)
            means.append(np.mean(p50s) if p50s else 0)
        offset = (j - (n_modes - 1) / 2) * width
        ax.bar(x + offset, means, width, label=MODE_LABELS[j], color=MODE_COLORS[j],
               edgecolor="black", alpha=BAR_ALPHA, linewidth=0.8)
        for gi, (ph, p50s) in enumerate(zip(ALL_PHASES, vals_per_phase)):
            if p50s:
                jitter = RNG.uniform(-width * 0.3, width * 0.3, len(p50s))
                ax.scatter(np.full(len(p50s), x[gi] + offset) + jitter, p50s,
                           color="black", s=DOT_SIZE * 0.5, zorder=5, alpha=0.5,
                           edgecolors="white", linewidth=0.5)
    ax.set_xticks(x)
    ax.set_xticklabels([p.replace("_", "\n") for p in ALL_PHASES], fontsize=7, rotation=0)
    ax.set_ylabel("p50 Latency (ms)", fontsize=LABEL_SIZE)
    ax.set_title("G6 — Per-Phase p50 Latency by Mode", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g6_per_phase_p50.png", dpi=150)
    plt.close(fig)
    print("✓ G6")

    pct_labels = ["p50", "p95", "p99"]
    g7_vals: list[list[list[float]]] = []  # [mode][pct_idx][run_vals]
    for mode in MODE_ORDER:
        mode_pcts = []
        for pct in [50, 95, 99]:
            run_vals = []
            for run_label in per_run_lat.get(mode, {}):
                all_lats = []
                for ph in per_run_lat[mode][run_label]:
                    all_lats.extend(per_run_lat[mode][run_label][ph])
                if all_lats:
                    run_vals.append(np.percentile(all_lats, pct))
            mode_pcts.append(run_vals)
        g7_vals.append(mode_pcts)

    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(pct_labels))
    for j, mode in enumerate(MODE_ORDER):
        means = [np.mean(g7_vals[MODE_ORDER.index(mode)][i]) if g7_vals[MODE_ORDER.index(mode)][i] else 0 for i in range(3)]
        offset = (j - (n_modes - 1) / 2) * width
        ax.bar(x + offset, means, width, label=MODE_LABELS[j], color=MODE_COLORS[j],
               edgecolor="black", alpha=BAR_ALPHA, linewidth=1)
        for gi in range(3):
            vals = g7_vals[MODE_ORDER.index(mode)][gi]
            if vals:
                jitter = RNG.uniform(-width * 0.3, width * 0.3, len(vals))
                ax.scatter(np.full(len(vals), x[gi] + offset) + jitter, vals,
                           color="black", s=DOT_SIZE * 0.7, zorder=5, alpha=0.55,
                           edgecolors="white", linewidth=0.5)
    ax.set_xticks(x)
    ax.set_xticklabels(pct_labels, fontsize=TICK_SIZE)
    ax.set_ylabel("Latency (ms)", fontsize=LABEL_SIZE)
    ax.set_title("G7 — Per-Mode Latency Percentiles", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=9)
    ax.set_yscale("log")
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g7_percentiles.png", dpi=150)
    plt.close(fig)
    print("✓ G7")

    type_groups = {
        "Baseline": ["baseline"],
        "Post-stress": ["cooldown_1", "cooldown_2", "cooldown_3", "demand_drop"],
        "Storage": STORAGE_PHASES,
        "Compute": COMPUTE_PHASES,
    }
    type_order = ["Baseline", "Post-stress", "Storage", "Compute"]
    g8_vals: list[list[list[float]]] = []  # [mode][type_idx][run_p95s]
    for mode in MODE_ORDER:
        mode_type_vals = []
        for tname in type_order:
            phases = type_groups[tname]
            run_p95s = []
            for run_label in per_run_lat.get(mode, {}):
                all_lats = []
                for ph in phases:
                    all_lats.extend(per_run_lat[mode][run_label].get(ph, []))
                if all_lats:
                    run_p95s.append(np.percentile(all_lats, 95))
            mode_type_vals.append(run_p95s)
        g8_vals.append(mode_type_vals)

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(len(type_order))
    for j, mode in enumerate(MODE_ORDER):
        means = [np.mean(g8_vals[MODE_ORDER.index(mode)][i]) if g8_vals[MODE_ORDER.index(mode)][i] else 0 for i in range(len(type_order))]
        offset = (j - (n_modes - 1) / 2) * width
        ax.bar(x + offset, means, width, label=MODE_LABELS[j], color=MODE_COLORS[j],
               edgecolor="black", alpha=BAR_ALPHA, linewidth=1.2)
        for gi in range(len(type_order)):
            vals = g8_vals[MODE_ORDER.index(mode)][gi]
            if vals:
                jitter = RNG.uniform(-width * 0.3, width * 0.3, len(vals))
                ax.scatter(np.full(len(vals), x[gi] + offset) + jitter, vals,
                           color="black", s=DOT_SIZE * 0.8, zorder=5, alpha=0.55,
                           edgecolors="white", linewidth=0.5)
    ax.set_xticks(x)
    ax.set_xticklabels(type_order, fontsize=TICK_SIZE)
    ax.set_ylabel("p95 Latency (ms)", fontsize=LABEL_SIZE)
    ax.set_title("G8 — Latency by Phase Type (p95)", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g8_phase_type_p95.png", dpi=150)
    plt.close(fig)
    print("✓ G8")

    g8b_vals: list[list[list[float]]] = []  # [mode][type_idx][run_p50s]
    for mode in MODE_ORDER:
        mode_type_vals = []
        for tname in type_order:
            phases = type_groups[tname]
            run_p50s = []
            for run_label in per_run_lat.get(mode, {}):
                all_lats = []
                for ph in phases:
                    all_lats.extend(per_run_lat[mode][run_label].get(ph, []))
                if all_lats:
                    run_p50s.append(np.percentile(all_lats, 50))
            mode_type_vals.append(run_p50s)
        g8b_vals.append(mode_type_vals)

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(len(type_order))
    for j, mode in enumerate(MODE_ORDER):
        means = [np.mean(g8b_vals[MODE_ORDER.index(mode)][i]) if g8b_vals[MODE_ORDER.index(mode)][i] else 0 for i in range(len(type_order))]
        offset = (j - (n_modes - 1) / 2) * width
        ax.bar(x + offset, means, width, label=MODE_LABELS[j], color=MODE_COLORS[j],
               edgecolor="black", alpha=BAR_ALPHA, linewidth=1.2)
        for gi in range(len(type_order)):
            vals = g8b_vals[MODE_ORDER.index(mode)][gi]
            if vals:
                jitter = RNG.uniform(-width * 0.3, width * 0.3, len(vals))
                ax.scatter(np.full(len(vals), x[gi] + offset) + jitter, vals,
                           color="black", s=DOT_SIZE * 0.8, zorder=5, alpha=0.55,
                           edgecolors="white", linewidth=0.5)
    ax.set_xticks(x)
    ax.set_xticklabels(type_order, fontsize=TICK_SIZE)
    ax.set_ylabel("p50 Latency (ms)", fontsize=LABEL_SIZE)
    ax.set_title("G8b — Latency by Phase Type (p50)", fontsize=TITLE_SIZE, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(args.out_dir / "g8b_phase_type_p50.png", dpi=150)
    plt.close(fig)
    print("✓ G8b")

    print("\n=== Campaign Summary ===")
    for mode in MODE_ORDER:
        for metric, key in [("TTFT", "ttft_s"), ("TFR", "tfr_s"),
                              ("Init", "init_time_s"), ("Share", "initial_share")]:
            vals = [float(s[key]) for s in all_spawns if s["mode"] == mode and s.get(key)]
            if vals:
                print(f"  {mode} {metric}: n={len(vals)} med={np.median(vals):.1f} mean={np.mean(vals):.1f} p95={np.percentile(vals, 95):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
