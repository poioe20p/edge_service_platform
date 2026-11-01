#!/usr/bin/env python3
"""Regenerate Graph 3 with real latency data from client_requests.csv."""
import csv, os
from collections import defaultdict
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

METRICS = Path("/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics")
GRAPHS_DIR = Path("/home/testop/efficient-storage-in-edge-scenarios/docs/operation/testing/experiment/rq2_evaluation/graphs/20260706_campaign")
GRAPHS_DIR.mkdir(parents=True, exist_ok=True)

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

MODE_LABELS = {
    "topology_host": "Host\n(no ramp)",
    "topology_slowstart": "Slowstart\n(discovery delay)",
    "topology_lifecycle": "Lifecycle\n(warm lease)",
}
MODE_COLORS = {
    "topology_host": "#e74c3c",
    "topology_slowstart": "#f39c12",
    "topology_lifecycle": "#27ae60",
}
MODE_ORDER = ["topology_host", "topology_slowstart", "topology_lifecycle"]

# Collect per-mode latency
mode_lat = defaultdict(list)
mode_fail = defaultdict(int)
mode_stress_lat = defaultdict(list)
mode_nonstress_lat = defaultdict(list)

for run_dir, mode in RUNS:
    path = METRICS / run_dir / "client_requests.csv"
    if not path.exists():
        continue
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            status = row.get("http_status", "")
            lat_s = row.get("latency_s", "")
            phase = row.get("phase", "")
            if status == "200" and lat_s:
                lat_ms = float(lat_s) * 1000
                mode_lat[mode].append(lat_ms)
                if "storm" in phase or "spike" in phase:
                    mode_stress_lat[mode].append(lat_ms)
                else:
                    mode_nonstress_lat[mode].append(lat_ms)
            elif status not in ("200", "0", ""):
                mode_fail[mode] += 1

# ═════════════════════════════════════════════════════════════════
# GRAPH 3: Latency boxplots per mode (all phases + stress-only)
# ═════════════════════════════════════════════════════════════════
fig, axes = plt.subplots(1, 3, figsize=(16, 6))

for idx, (title, data_dict) in enumerate([
    ("All Phases", mode_lat),
    ("Stress Phases (storm+spike)", mode_stress_lat),
    ("Non-Stress Phases (cooldown+baseline+drop)", mode_nonstress_lat),
]):
    ax = axes[idx]
    box_data = [data_dict[m] for m in MODE_ORDER]
    bp = ax.boxplot(box_data, widths=0.5, patch_artist=True, showfliers=False)
    for i, mode in enumerate(MODE_ORDER):
        bp["boxes"][i].set_facecolor(MODE_COLORS[mode])
        lats = data_dict[mode]
        p50 = np.percentile(lats, 50)
        p95 = np.percentile(lats, 95)
        ax.annotate(f"p50={p50:.0f}ms\np95={p95:.0f}ms\nn={len(lats)}",
                    (i+1, p95), ha="center", va="bottom", fontsize=7,
                    xytext=(0, 5), textcoords="offset points")

    ax.set_xticklabels([MODE_LABELS[m] for m in MODE_ORDER], fontsize=9)
    ax.set_ylabel("Latency (ms)", fontsize=11)
    ax.set_title(title, fontsize=12)
    ax.grid(True, alpha=0.3, axis="y")

fig.suptitle("Graph 3 (corrected): Per-Mode Latency Distribution (compute tier)", fontsize=13, fontweight="bold")
fig.tight_layout()
fig.savefig(GRAPHS_DIR / "graph3_transition_quality.png", dpi=150)
plt.close(fig)
print("Graph 3 (corrected) saved")

# ═════════════════════════════════════════════════════════════════
# BONUS: Latency comparison bar chart
# ═════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 6))
x = np.arange(len(MODE_ORDER))
width = 0.25

p50_vals = [np.percentile(mode_lat[m], 50) for m in MODE_ORDER]
p95_vals = [np.percentile(mode_lat[m], 95) for m in MODE_ORDER]
p99_vals = [np.percentile(mode_lat[m], 99) for m in MODE_ORDER]

ax.bar(x - width, p50_vals, width, label="p50", color="#3498db")
ax.bar(x, p95_vals, width, label="p95", color="#e74c3c")
ax.bar(x + width, p99_vals, width, label="p99", color="#2c3e50")

for i, mode in enumerate(MODE_ORDER):
    ax.text(i - width, p50_vals[i] + 50, f"{p50_vals[i]:.0f}", ha="center", fontsize=7)
    ax.text(i, p95_vals[i] + 50, f"{p95_vals[i]:.0f}", ha="center", fontsize=7)
    ax.text(i + width, p99_vals[i] + 50, f"{p99_vals[i]:.0f}", ha="center", fontsize=7)

ax.set_xticks(x)
ax.set_xticklabels([MODE_LABELS[m] for m in MODE_ORDER], fontsize=10)
ax.set_ylabel("Latency (ms)", fontsize=12)
ax.set_title("Per-Mode Latency Percentiles (all phases, compute tier)", fontsize=13)
ax.legend(fontsize=10)
ax.grid(True, alpha=0.3, axis="y")
fig.tight_layout()
fig.savefig(GRAPHS_DIR / "graph3b_latency_percentiles.png", dpi=150)
plt.close(fig)
print("Graph 3b (latency percentiles) saved")

# Print summary table
print("\n=== FINAL CAMPAIGN LATENCY SUMMARY ===")
print(f"{'Mode':<25} {'Requests':>10} {'p50(ms)':>10} {'p95(ms)':>10} {'p99(ms)':>10} {'Fail%':>8}")
print("-" * 73)
for mode in MODE_ORDER:
    lats = mode_lat[mode]
    fails = mode_fail[mode]
    total = len(lats) + fails
    print(f"{mode:<25} {len(lats):>10} {np.percentile(lats,50):>10.1f} {np.percentile(lats,95):>10.1f} {np.percentile(lats,99):>10.1f} {fails/max(total,1)*100:>7.2f}%")
