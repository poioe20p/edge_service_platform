#!/usr/bin/env python3
"""Regenerate all RQ2 campaign graphs matching RQ1 comparison-graph style.

Style constants mirror source/scripts/testing/analysis/rq1/scripts/generate_comparison_graphs.py:
  - FIG_SINGLE = (10, 6), DPI = 150
  - TITLE_SIZE=13 bold, LABEL_SIZE=12, TICK_SIZE=11, ANNO_SIZE=11
  - bar: edgecolor="black", alpha=0.75
  - grid: alpha=0.25, linestyle="--", axis="y"
  - spines: top & right hidden
  - bar labels: fontweight="bold", centered above bars
  - tight_layout() before save
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ── Paths ────────────────────────────────────────────────────────
METRICS = Path("/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics")
OUT_DIR = Path("/home/testop/efficient-storage-in-edge-scenarios/docs/operation/testing/experiment/rq2_evaluation/graphs/20260707_campaign_analysis")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Run definitions ──────────────────────────────────────────────
RUNS = [
    ("20260706_171835_rq2_th_1", "topology_host"),
    ("20260706_181823_rq2_th_2", "topology_host"),
    ("20260706_190002_rq2_th_3", "topology_host"),
    ("20260706_194238_rq2_ss_1", "topology_slowstart"),
    ("20260706_202401_rq2_ss_2", "topology_slowstart"),
    ("20260706_210552_rq2_ss_3", "topology_slowstart"),
    ("20260706_214625_rq2_tl_1", "topology_lifecycle"),
    ("20260706_222823_rq2_tl_2", "topology_lifecycle"),
    ("20260706_231045_rq2_tl_3", "topology_lifecycle"),
]

# ── Style constants (from RQ1 comparison-graph style) ────────────
MODE_ORDER = ["topology_host", "topology_slowstart", "topology_lifecycle"]
MODE_COLORS = ["#F44336", "#FF9800", "#2196F3"]  # Red→Orange→Blue (host→slowstart→lifecycle)
MODE_LABELS = ["Host\n(no ramp)", "Slowstart\n(discovery delay)", "Lifecycle\n(warm lease)"]

FIG_SINGLE = (10, 6)
TITLE_SIZE = 13
LABEL_SIZE = 12
TICK_SIZE = 11
ANNO_SIZE = 11
BAR_ALPHA = 0.75
GRID_ALPHA = 0.25
DPI = 150

def _style_bar_ax(ax, x, labels, ylabel, title):
    """Apply consistent RQ1-style bar-chart axis formatting."""
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=TICK_SIZE)
    ax.set_ylabel(ylabel, fontsize=LABEL_SIZE)
    ax.set_title(title, fontsize=TITLE_SIZE, fontweight="bold")
    ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

def _add_bar_labels(ax, x, vals, fmt, offset_pct=0.02):
    """Add value labels above each bar (offset_pct of max value)."""
    max_v = max(vals) if vals else 1
    for i, v in enumerate(vals):
        offset = max_v * offset_pct + 1
        ax.text(x[i], v + offset, fmt.format(v), ha="center",
                fontweight="bold", fontsize=ANNO_SIZE)

# ═══════════════════════════════════════════════════════════════════
# DATA COLLECTION
# ═══════════════════════════════════════════════════════════════════

def collect_per_mode():
    """Collect per-mode latency and failure data from client_requests.csv.

    Returns:
        mode_lat: {mode: [latency_ms, ...]}  — all successful request latencies
        mode_fail_count: {mode: int}          — count of non-200, non-0 statuses
        mode_stress_lat: {mode: [ms, ...]}    — stress-only latencies
        mode_nonstress_lat: {mode: [ms, ...]} — non-stress latencies
        mode_replicate_lat: {mode: [[ms,...], [ms,...], [ms,...]]} — per-replicate
    """
    mode_lat = defaultdict(list)
    mode_fail_count = defaultdict(int)
    mode_stress_lat = defaultdict(list)
    mode_nonstress_lat = defaultdict(list)
    mode_replicate_lat = defaultdict(list)

    for run_dir, mode in RUNS:
        path = METRICS / run_dir / "client_requests.csv"
        if not path.exists():
            continue
        run_lats = []
        with open(path) as f:
            r = csv.DictReader(f)
            for row in r:
                status = row.get("http_status", "")
                lat_s = row.get("latency_s", "")
                phase = row.get("phase", "")
                if status == "200" and lat_s:
                    lat_ms = float(lat_s) * 1000
                    mode_lat[mode].append(lat_ms)
                    run_lats.append(lat_ms)
                    if "storm" in phase or "spike" in phase:
                        mode_stress_lat[mode].append(lat_ms)
                    else:
                        mode_nonstress_lat[mode].append(lat_ms)
                elif status not in ("200", "0", ""):
                    mode_fail_count[mode] += 1
        if run_lats:
            mode_replicate_lat[mode].append(run_lats)

    return mode_lat, mode_fail_count, mode_stress_lat, mode_nonstress_lat, mode_replicate_lat


def collect_initial_shares():
    """Collect initial load share from rq2_redistribution_profile.csv per run."""
    mode_shares = defaultdict(list)
    for run_dir, mode in RUNS:
        path = METRICS / run_dir / "analysis" / "rq2_redistribution_profile.csv"
        if path.exists():
            with open(path) as f:
                for row in csv.DictReader(f):
                    if float(row.get("time_since_spawn_s", -1)) == 0:
                        mode_shares[mode].append(float(row["mean_share"]))
    return mode_shares


def collect_cumulative_loads():
    """Collect cumulative load from rq2_cumulative_load.csv per run."""
    mode_loads = defaultdict(list)
    for run_dir, mode in RUNS:
        path = METRICS / run_dir / "analysis" / "rq2_cumulative_load.csv"
        if path.exists():
            with open(path) as f:
                for row in csv.DictReader(f):
                    if float(row.get("time_since_spawn_s", -1)) == 0:
                        mode_loads[mode].append(float(row["mean_cumulative_load"]))
    return mode_loads


# ═══════════════════════════════════════════════════════════════════
# GRAPH GENERATION
# ═══════════════════════════════════════════════════════════════════

print("Collecting data...")
mode_lat, mode_fail, mode_stress, mode_nonstress, mode_repl_lat = collect_per_mode()
mode_shares = collect_initial_shares()
mode_loads = collect_cumulative_loads()

x = np.arange(len(MODE_ORDER))

# ── Compute aggregate stats per mode ─────────────────────────────
mode_stats = {}
for mode in MODE_ORDER:
    lats = mode_lat[mode]
    if not lats:
        mode_stats[mode] = {"p50": 0, "p95": 0, "p99": 0, "n": 0, "fail": 0}
        continue
    mode_stats[mode] = {
        "p50": np.percentile(lats, 50),
        "p95": np.percentile(lats, 95),
        "p99": np.percentile(lats, 99),
        "n": len(lats),
        "fail": mode_fail[mode],
    }

# ═══════════════════════════════════════════════════════════════════
# GRAPH 1: Load Share vs Time Since Spawn (initial window only — bar chart)
# ═══════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=FIG_SINGLE)
shares_mean = [np.mean(mode_shares[m]) if mode_shares[m] else 0 for m in MODE_ORDER]
_style_bar_ax(ax, x, MODE_LABELS, "Initial Load Share",
              "RQ2 — Backend Load Share at Spawn (compute tier)")
bars = ax.bar(x, shares_mean, color=MODE_COLORS, edgecolor="black", alpha=BAR_ALPHA, width=0.55)

# Add per-replicate scatter dots
for i, mode in enumerate(MODE_ORDER):
    vals = mode_shares.get(mode, [])
    if len(vals) > 1:
        jitter = np.random.default_rng(42).uniform(-0.1, 0.1, len(vals))
        ax.scatter(np.full(len(vals), x[i]) + jitter, vals,
                   color="black", s=50, zorder=5,
                   edgecolors="white", linewidth=1)
    # Value label
    if vals:
        ax.text(x[i], np.mean(vals) + 0.02, f"{np.mean(vals):.1%}",
                ha="center", fontweight="bold", fontsize=ANNO_SIZE, color="#333333")

ax.set_ylim(0, 1.05)
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f'{y:.0%}'))
plt.tight_layout()
fig.savefig(OUT_DIR / "graph1_redistribution_profile.png", dpi=DPI)
plt.close(fig)
print("✓ Graph 1 (RQ1-style load share)")

# ═══════════════════════════════════════════════════════════════════
# GRAPH 2: Redistribution Time — replaced by Initial Share comparison
# (since 0 events reached equilibrium, Graph 2 is the initial share bar chart)
# BUT the plan requires Graph 2 exists. We output a note graph.
# ═══════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=FIG_SINGLE)
ax.text(0.5, 0.55, "Redistribution Time — NOT MEASURABLE",
        ha="center", va="center", fontsize=16, fontweight="bold",
        transform=ax.transAxes, color="#333333")
ax.text(0.5, 0.40, "0 of 47 scale-up events reached equilibrium",
        ha="center", va="center", fontsize=13,
        transform=ax.transAxes, color="#666666")
ax.text(0.5, 0.28, "(\u00b110% load share sustained \u22653 telemetry windows)",
        ha="center", va="center", fontsize=11,
        transform=ax.transAxes, color="#999999", style="italic")
ax.text(0.5, 0.15, "See Graph 1 for initial load-share comparison (closest proxy).",
        ha="center", va="center", fontsize=11,
        transform=ax.transAxes, color="#555555")
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")
plt.tight_layout()
fig.savefig(OUT_DIR / "graph2_redistribution_summary.png", dpi=DPI)
plt.close(fig)
print("✓ Graph 2 (not-measurable note)")

# ═══════════════════════════════════════════════════════════════════
# GRAPH 3: Transition Quality — p50/p95/p99 Latency per Mode
# ═══════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=FIG_SINGLE)
width = 0.22
p50s = [mode_stats[m]["p50"] for m in MODE_ORDER]
p95s = [mode_stats[m]["p95"] for m in MODE_ORDER]
p99s = [mode_stats[m]["p99"] for m in MODE_ORDER]

ax.bar(x - width, p50s, width, label="p50", color="#90CAF9", edgecolor="black", alpha=BAR_ALPHA)
ax.bar(x, p95s, width, label="p95", color="#F44336", edgecolor="black", alpha=BAR_ALPHA)
ax.bar(x + width, p99s, width, label="p99", color="#424242", edgecolor="black", alpha=BAR_ALPHA)

# Value labels
for i, mode in enumerate(MODE_ORDER):
    for j, (pos, val, color) in enumerate([
        (-width, p50s[i], "#1565C0"),
        (0, p95s[i], "#B71C1C"),
        (+width, p99s[i], "#212121"),
    ]):
        ax.text(x[i] + pos, val + max(p99s)*0.015, f"{val:.0f}",
                ha="center", fontsize=ANNO_SIZE-2, fontweight="bold", color=color)

_style_bar_ax(ax, x, MODE_LABELS, "Latency (ms)",
              "RQ2 — Per-Mode Latency Percentiles (all phases, compute tier)")
ax.legend(fontsize=TICK_SIZE-1, framealpha=0.8, loc="upper left")
plt.tight_layout()
fig.savefig(OUT_DIR / "graph3_transition_quality.png", dpi=DPI)
plt.close(fig)
print("✓ Graph 3 (latency percentiles)")

# ═══════════════════════════════════════════════════════════════════
# GRAPH 3b: Stress vs Non-Stress latency (p95 only) — supplemental
# ═══════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=FIG_SINGLE)
stress_p95 = [np.percentile(mode_stress[m], 95) if mode_stress[m] else 0 for m in MODE_ORDER]
nonstress_p95 = [np.percentile(mode_nonstress[m], 95) if mode_nonstress[m] else 0 for m in MODE_ORDER]

ax.bar(x - width/2, stress_p95, width, label="Stress phases (storm+spike)",
       color="#FF7043", edgecolor="black", alpha=BAR_ALPHA)
ax.bar(x + width/2, nonstress_p95, width, label="Non-stress phases",
       color="#81C784", edgecolor="black", alpha=BAR_ALPHA)

for i in range(len(MODE_ORDER)):
    ax.text(x[i] - width/2, stress_p95[i] + 30, f"{stress_p95[i]:.0f}",
            ha="center", fontsize=ANNO_SIZE-2, fontweight="bold", color="#BF360C")
    ax.text(x[i] + width/2, nonstress_p95[i] + 5, f"{nonstress_p95[i]:.0f}",
            ha="center", fontsize=ANNO_SIZE-2, fontweight="bold", color="#2E7D32")

_style_bar_ax(ax, x, MODE_LABELS, "p95 Latency (ms)",
              "RQ2 — p95 Latency: Stress vs Non-Stress Phases (compute tier)")
ax.legend(fontsize=TICK_SIZE-1, framealpha=0.8)
plt.tight_layout()
fig.savefig(OUT_DIR / "graph3b_latency_percentiles.png", dpi=DPI)
plt.close(fig)
print("✓ Graph 3b (stress vs non-stress p95)")

# ═══════════════════════════════════════════════════════════════════
# GRAPH 4: Cumulative Load at Spawn per Mode
# ═══════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=FIG_SINGLE)
load_means = [np.mean(mode_loads[m]) if mode_loads[m] else 0 for m in MODE_ORDER]
_style_bar_ax(ax, x, MODE_LABELS, "Mean Cumulative Requests Served",
              "RQ2 — Cumulative Load at Spawn (compute tier)")
ax.bar(x, load_means, color=MODE_COLORS, edgecolor="black", alpha=BAR_ALPHA, width=0.55)

for i, mode in enumerate(MODE_ORDER):
    vals = mode_loads.get(mode, [])
    if len(vals) > 1:
        jitter = np.random.default_rng(42).uniform(-0.1, 0.1, len(vals))
        ax.scatter(np.full(len(vals), x[i]) + jitter, vals,
                   color="black", s=50, zorder=5,
                   edgecolors="white", linewidth=1)
    if vals:
        ax.text(x[i], np.mean(vals) + 50, f"{np.mean(vals):.0f}",
                ha="center", fontweight="bold", fontsize=ANNO_SIZE)

plt.tight_layout()
fig.savefig(OUT_DIR / "graph4_cumulative_load.png", dpi=DPI)
plt.close(fig)
print("✓ Graph 4 (cumulative load)")

# ═══════════════════════════════════════════════════════════════════
# GRAPH 5: Coordination Gap — slowstart − lifecycle initial share gap
# (proxy since redistribution time is not measurable)
# ═══════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=FIG_SINGLE)
ss_share = np.mean(mode_shares.get("topology_slowstart", [0]))
tl_share = np.mean(mode_shares.get("topology_lifecycle", [0]))
host_share = np.mean(mode_shares.get("topology_host", [0]))

gap_labels = ["Slowstart\nvs Lifecycle", "Lifecycle\nvs Host", "Slowstart\nvs Host"]
gap_vals = [ss_share - tl_share, tl_share - host_share, ss_share - host_share]
gap_colors = ["#FF9800" if v < 0 else "#F44336" for v in gap_vals]

x_gap = np.arange(len(gap_labels))
ax.bar(x_gap, gap_vals, color=gap_colors, edgecolor="black", alpha=BAR_ALPHA, width=0.5)
ax.axhline(y=0, color="black", linewidth=1.5)

for i, (label, val) in enumerate(zip(gap_labels, gap_vals)):
    y_pos = val + 0.01 if val >= 0 else val - 0.04
    ax.text(i, y_pos, f"{val:+.1%}", ha="center", fontweight="bold",
            fontsize=ANNO_SIZE, color="#333333")

ax.set_xticks(x_gap)
ax.set_xticklabels(gap_labels, fontsize=TICK_SIZE-1)
ax.set_ylabel("\u0394 Initial Load Share", fontsize=LABEL_SIZE)
ax.set_title("RQ2 — Coordination Gap: Initial Load-Share Difference (compute tier)",
             fontsize=TITLE_SIZE, fontweight="bold")
ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f'{y:+.0%}'))
plt.tight_layout()
fig.savefig(OUT_DIR / "graph5_coordination_gap.png", dpi=DPI)
plt.close(fig)
print("✓ Graph 5 (coordination gap via initial share)")

# ═══════════════════════════════════════════════════════════════════
# SUMMARY TABLE (printed to stdout + CSV)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*85)
print("RQ2 CAMPAIGN SUMMARY — RQ1-Style Graph Set")
print("="*85)
header = f"{'Mode':<25} {'Req':>9} {'p50(ms)':>10} {'p95(ms)':>10} {'p99(ms)':>10} {'Fail%':>8} {'InitShare':>10}"
print(header)
print("-"*85)
for mode in MODE_ORDER:
    s = mode_stats[mode]
    share = np.mean(mode_shares.get(mode, [0]))
    fr = s["fail"] / max(s["n"] + s["fail"], 1) * 100
    print(f"{mode:<25} {s['n']:>9} {s['p50']:>10.1f} {s['p95']:>10.1f} {s['p99']:>10.1f} {fr:>7.2f}% {share:>9.1%}")

print(f"\nAll 6 graphs saved to: {OUT_DIR}")
