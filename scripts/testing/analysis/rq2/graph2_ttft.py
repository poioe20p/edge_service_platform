#!/usr/bin/env python3
"""Generate Graph 2: Time-to-First-Traffic (TTFT) per mode, RQ1 comparison style."""
import csv, os, re
from collections import defaultdict
from datetime import datetime
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

METRICS = "/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics"
OUT_DIR = "/home/testop/efficient-storage-in-edge-scenarios/docs/operation/testing/experiment/rq2_evaluation/graphs/20260707_campaign_analysis"

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

MODE_ORDER = ["topology_host", "topology_slowstart", "topology_lifecycle"]
MODE_COLORS = ["#F44336", "#FF9800", "#2196F3"]
MODE_LABELS = ["Host\n(no ramp)", "Slowstart\n(discovery delay)", "Lifecycle\n(warm lease)"]

FIG_SINGLE = (10, 6)
TITLE_SIZE = 13
LABEL_SIZE = 12
TICK_SIZE = 11
ANNO_SIZE = 11
BAR_ALPHA = 0.75
GRID_ALPHA = 0.25
DPI = 150

def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except:
        return None

def extract_ttft_per_run(run_dir):
    """Return list of TTFT values for a single run."""
    # Spawn events from controller logs
    spawns = {}
    for lan in ["lan1", "lan2"]:
        log = os.path.join(METRICS, run_dir, "controller_" + lan + ".log")
        if not os.path.exists(log):
            continue
        with open(log) as f:
            for line in f:
                if "spawning" in line and "compute:" in line:
                    parts = line.split(" ")
                    iso_ts = parts[0] + "T" + parts[1].split(",")[0]
                    unix_ts = parse_ts(iso_ts)
                    mac_match = re.search(r"mac=([0-9a-f:]+)", line)
                    mac = mac_match.group(1) if mac_match else None
                    if unix_ts and mac and mac not in spawns:
                        spawns[mac] = unix_ts

    # First request per MAC from per_node_stats
    first_req = {}
    pns = os.path.join(METRICS, run_dir, "per_node_stats.csv")
    if os.path.exists(pns):
        with open(pns) as f:
            for row in csv.DictReader(f):
                mac = row.get("server_id", "").strip()
                rc = int(row.get("request_count", 0))
                we = float(row.get("window_end", 0))
                if mac and rc > 0 and mac not in first_req:
                    first_req[mac] = we

    # Compute TTFT per matched spawn
    ttfts = []
    for mac, spawn_ts in spawns.items():
        if mac in first_req:
            ttft = first_req[mac] - spawn_ts
            if 0 <= ttft < 600:
                ttfts.append(ttft)
    return ttfts

# Collect per-mode per-replicate data
mode_repl_ttft = defaultdict(list)
for run_dir, mode in RUNS:
    ttfts = extract_ttft_per_run(run_dir)
    if ttfts:
        mode_repl_ttft[mode].append(ttfts)

# Compute per-mode aggregates
x = np.arange(len(MODE_ORDER))
medians = []
for mode in MODE_ORDER:
    all_vals = [v for repl in mode_repl_ttft[mode] for v in repl]
    medians.append(np.median(all_vals))

# ── Plot ─────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=FIG_SINGLE)

bars = ax.bar(x, medians, color=MODE_COLORS, edgecolor="black", alpha=BAR_ALPHA, width=0.55)

# Per-replicate scatter dots (median per replicate)
for i, mode in enumerate(MODE_ORDER):
    repl_medians = [np.median(repl) for repl in mode_repl_ttft[mode]]
    if len(repl_medians) > 1:
        jitter = np.random.default_rng(42).uniform(-0.1, 0.1, len(repl_medians))
        ax.scatter(np.full(len(repl_medians), x[i]) + jitter, repl_medians,
                   color="black", s=50, zorder=5,
                   edgecolors="white", linewidth=1)
    # Value label
    ax.text(x[i], medians[i] + 3, "{:.0f}s".format(medians[i]),
            ha="center", fontweight="bold", fontsize=ANNO_SIZE, color="#333333")

# Style
ax.set_xticks(x)
ax.set_xticklabels(MODE_LABELS, fontsize=TICK_SIZE)
ax.set_ylabel("Median Time-to-First-Traffic (s)", fontsize=LABEL_SIZE)
ax.set_title("RQ2 -- Time-to-First-Traffic by Routing Mode (compute tier)", fontsize=TITLE_SIZE, fontweight="bold")
ax.grid(axis="y", alpha=GRID_ALPHA, linestyle="--")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)

# Add n=... annotations
for i, mode in enumerate(MODE_ORDER):
    all_vals = [v for repl in mode_repl_ttft[mode] for v in repl]
    ax.annotate("n={}".format(len(all_vals)), (x[i], 0), ha="center", va="bottom",
                fontsize=9, xytext=(0, -18), textcoords="offset points", color="#666666")

plt.tight_layout()
path = os.path.join(OUT_DIR, "graph2_redistribution_summary.png")
fig.savefig(path, dpi=DPI)
plt.close(fig)
print("Graph 2 (TTFT) saved to " + path)

# Print summary
print()
for mode in MODE_ORDER:
    all_vals = [v for repl in mode_repl_ttft[mode] for v in repl]
    repl_meds = [np.median(repl) for repl in mode_repl_ttft[mode]]
    print("{}: n={}  median={:.1f}s  p95={:.1f}s  per-run medians={}".format(
        mode, len(all_vals), np.median(all_vals), np.percentile(all_vals, 95),
        ["{:.0f}s".format(m) for m in repl_meds]))
