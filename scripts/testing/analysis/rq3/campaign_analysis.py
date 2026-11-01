#!/usr/bin/env python3
"""RQ3 cross-mode comparison graph generator.

Generates thesis graphs G1-G8 + G1b + G5b + G7b + G9-G12
from a 9-run RQ3 experiment campaign.

Usage:
    python -m source.scripts.testing.analysis.rq3.campaign_analysis \
        --run label:mode:path/to/run ... --out-dir path/to/graphs
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

MODE_ORDER = ["degradation_score", "cpu_only", "latency_only"]
MODE_LABELS = ["degradation_score", "cpu_only", "latency_only"]
MODE_COLORS = {"degradation_score": "#4CAF50", "cpu_only": "#F44336", "latency_only": "#2196F3"}
MODE_EDGES = {"degradation_score": "#1B5E20", "cpu_only": "#B71C1C", "latency_only": "#0D47A1"}

STRESS_PHASES = ["storage_storm", "tier1_hotspot", "reverse_hotspot", "compute_spike"]
ALL_PHASES = ["baseline", "storage_storm", "tier1_hotspot", "inter_hotspot_cooldown",
              "reverse_hotspot", "compute_spike", "demand_drop"]
PHASE_TYPE = {
    "baseline": "Baseline",
    "storage_storm": "Storage stress",
    "tier1_hotspot": "Storage stress",
    "inter_hotspot_cooldown": "Post-stress",
    "reverse_hotspot": "Storage stress",
    "compute_spike": "Compute stress",
    "demand_drop": "Post-stress",
}
PHASE_TYPE_ORDER = ["Baseline", "Storage stress", "Compute stress", "Post-stress"]

FIG_SINGLE = (8, 5)
FIG_BOX = (10, 6)
FIG_MULTI = (14, 6)
FIG_WIDE = (18, 7)
FIG_DIAG = (20, 8)
TITLE_SZ = 13
LABEL_SZ = 12
TICK_SZ = 10
ANNO_SZ = 9
BAR_ALPHA = 0.78
GRID_ALPHA = 0.22
DOT_ALPHA = 0.55
RNG = np.random.default_rng(42)

CURL_MAX_TIME = 30.0  # Hard client timeout


def load_csv(run_dir: Path, name: str) -> list[dict]:
    p = run_dir / name
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def parse_dt(ts_str: str) -> float:
    """Parse datetime string like '2026-07-29 00:48:59,233' or ISO to epoch seconds."""
    from datetime import datetime, timezone
    ts_str = ts_str.strip().strip('"')
    for fmt in ["%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"]:
        try:
            return datetime.strptime(ts_str, fmt).timestamp()
        except ValueError:
            continue
    # Try space-separated with comma decimal (Python log format)
    try:
        ts_str_norm = ts_str.replace(",", ".")
        return datetime.strptime(ts_str_norm, "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except ValueError:
        pass
    return 0.0


def is_reserve_event(ev: dict) -> bool:
    """Check if an elasticity event is a reserve/standby spawn."""
    et = ev.get("event_type", ev.get("event", "")).lower()
    nt = ev.get("node_type", "").lower()
    detail = ev.get("detail", "").lower()
    if "standby" in et or "standby" in nt or "standby" in detail:
        return True
    if "reserve" in et or "reserve" in nt or "reserve" in detail:
        return True
    return False


def is_spawn_event(ev: dict) -> bool:
    """Check if an elasticity event is a spawn (not timing/online/removal)."""
    et = ev.get("event_type", ev.get("event", "")).lower()
    return "spawning" in et or "spawn" in et


def is_node_addition(ev: dict) -> bool:
    """Check if an elasticity event represents a node being added.
    
    Matches node_spawning, node_add_timing, node_ready_timing, and node_online.
    These all represent a node joining the pool. Excludes removals and cleanups.
    """
    et = ev.get("event_type", ev.get("event", "")).lower()
    return any(kw in et for kw in ("spawning", "spawn", "add_timing", "ready_timing", "online"))


def load_phases(run_dir: Path) -> list[dict]:
    p = run_dir / "phases_snapshot.json"
    if p.exists():
        with open(p) as f:
            data = json.load(f)
            return data.get("phases", [])
    return []


def phase_boundaries(phases: list[dict]) -> dict[str, tuple[float, float]]:
    """Return {phase_name: (start_s, end_s)}."""
    bounds = {}
    t = 0.0
    for ph in phases:
        name = ph["name"]
        dur = ph["duration_s"]
        bounds[name] = (t, t + dur)
        t += dur
    return bounds


def graph_g1(runs: list[dict], out_dir: Path):
    """Grouped bar: baseline FP spawns per mode with per-replicate scatter."""
    mode_counts = defaultdict(list)
    for r in runs:
        phase_map = phase_boundaries(r["phases"])
        b_start, b_end = phase_map.get("baseline", (0, 0))
        spawns = 0
        for ev in r["elasticity"]:
            if is_reserve_event(ev):
                continue
            if not is_spawn_event(ev):
                continue
            ts = parse_dt(ev.get("timestamp_s", ev.get("timestamp", "0")))
            if b_start <= ts <= b_end:
                spawns += 1
        mode_counts[r["mode"]].append(spawns)

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    x = np.arange(len(MODE_ORDER))
    means = [np.mean(mode_counts[m]) if mode_counts[m] else 0 for m in MODE_ORDER]
    sems = [np.std(mode_counts[m]) / max(np.sqrt(len(mode_counts[m])), 1) if mode_counts[m] else 0 for m in MODE_ORDER]

    bars = ax.bar(x, means, yerr=sems, capsize=5, color=[MODE_COLORS[m] for m in MODE_ORDER],
                  alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8)

    for i, m in enumerate(MODE_ORDER):
        vals = mode_counts[m]
        if vals:
            jitter = RNG.uniform(-0.25, 0.25, len(vals))
            ax.scatter(x[i] + jitter, vals, color=MODE_EDGES[m], s=30, alpha=DOT_ALPHA, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(MODE_LABELS, fontsize=TICK_SZ, rotation=15)
    ax.set_ylabel("Baseline FP spawns", fontsize=LABEL_SZ)
    ax.set_title("G1 — Baseline False-Positive Spawns by Mode", fontsize=TITLE_SZ, fontweight="bold")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g1_baseline_fp_spawns.png", dpi=150)
    plt.close(fig)
    return mode_counts


def graph_g1b(runs: list[dict], out_dir: Path):
    """2D scatter of CPU vs latency score components for baseline FP spawn events."""
    points = defaultdict(list)
    for r in runs:
        phase_map = phase_boundaries(r["phases"])
        b_start, b_end = phase_map.get("baseline", (0, 0))
        policy_rows = r.get("policy_state", [])
        for ev in r["elasticity"]:
            if is_reserve_event(ev):
                continue
            if not is_spawn_event(ev):
                continue
            ts = parse_dt(ev.get("timestamp_s", ev.get("timestamp", "0")))
            if b_start <= ts <= b_end:
                cpu_c, lat_c = 0.0, 0.0
                best_dt = float("inf")
                for pr in policy_rows:
                    try:
                        pt = float(pr.get("timestamp_s", pr.get("window_end_s", 0)))
                    except (ValueError, TypeError):
                        continue
                    dt = abs(ts - pt)
                    if dt < best_dt:
                        best_dt = dt
                        cpu_c = float(pr.get("compute_score", pr.get("score", 0)))
                        lat_c = float(pr.get("storage_score", 0))
                points[r["mode"]].append((cpu_c, lat_c))

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    for mode in MODE_ORDER:
        pts = points.get(mode, [])
        if pts:
            xs, ys = zip(*pts)
            ax.scatter(xs, ys, c=MODE_COLORS[mode], label=mode, s=28, alpha=0.65, edgecolors="black", linewidth=0.3)

    ax.set_xlabel("CPU score component", fontsize=LABEL_SZ)
    ax.set_ylabel("Latency score component", fontsize=LABEL_SZ)
    ax.set_title("G1b — FP Spawn Score Components at Trigger", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(fontsize=TICK_SZ)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "g1b_fp_score_components.png", dpi=150)
    plt.close(fig)


def graph_g2(runs: list[dict], out_dir: Path):
    """Grouped bar: spawns per stress phase per mode. Uses all node addition events."""
    mode_phase_counts = {m: defaultdict(list) for m in MODE_ORDER}
    for r in runs:
        phase_map = phase_boundaries(r["phases"])
        for ph_name in STRESS_PHASES:
            if ph_name not in phase_map:
                continue
            start, end = phase_map[ph_name]
            spawns = 0
            for ev in r["elasticity"]:
                if is_reserve_event(ev):
                    continue
                if not is_node_addition(ev):
                    continue
                ts = parse_dt(ev.get("timestamp_s", ev.get("timestamp", "0")))
                if start <= ts <= end:
                    spawns += 1
            mode_phase_counts[r["mode"]][ph_name].append(spawns)

    fig, ax = plt.subplots(figsize=FIG_MULTI)
    n_phases = len(STRESS_PHASES)
    n_modes = len(MODE_ORDER)
    total_width = 0.7
    bar_width = total_width / n_modes
    x = np.arange(n_phases)

    for i, mode in enumerate(MODE_ORDER):
        means = [np.mean(mode_phase_counts[mode].get(ph, [0])) for ph in STRESS_PHASES]
        sems = [np.std(mode_phase_counts[mode].get(ph, [0])) / max(np.sqrt(len(mode_phase_counts[mode].get(ph, [0]))), 1) for ph in STRESS_PHASES]
        offset = (i - n_modes / 2 + 0.5) * bar_width
        ax.bar(x + offset, means, bar_width, yerr=sems, capsize=4,
               color=MODE_COLORS[mode], alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8, label=mode)
        for j, ph in enumerate(STRESS_PHASES):
            vals = mode_phase_counts[mode].get(ph, [])
            if vals:
                jitter = RNG.uniform(-bar_width * 0.3, bar_width * 0.3, len(vals))
                ax.scatter(x[j] + offset + jitter, vals, color=MODE_EDGES[mode], s=25, alpha=DOT_ALPHA, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(STRESS_PHASES, fontsize=TICK_SZ, rotation=20)
    ax.set_ylabel("Spawn count", fontsize=LABEL_SZ)
    ax.set_title("G2 — Stress Spawn Count by Mode & Phase", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(fontsize=TICK_SZ)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g2_stress_spawn_count.png", dpi=150)
    plt.close(fig)


def graph_g3(runs: list[dict], out_dir: Path):
    """Box plot: time-to-first-spawn per stress phase per mode."""
    mode_phase_ttfs = {m: defaultdict(list) for m in MODE_ORDER}
    for r in runs:
        phase_map = phase_boundaries(r["phases"])
        for ph_name in STRESS_PHASES:
            if ph_name not in phase_map:
                continue
            start, end = phase_map[ph_name]
            first_ts = None
            for ev in r["elasticity"]:
                if is_reserve_event(ev):
                    continue
                if not is_node_addition(ev):
                    continue
                ts = parse_dt(ev.get("timestamp_s", ev.get("timestamp", "0")))
                if start <= ts <= end:
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
            if first_ts is not None:
                mode_phase_ttfs[r["mode"]][ph_name].append(first_ts - start)

    fig, ax = plt.subplots(figsize=FIG_BOX)
    positions = []
    data_series = []
    colors = []
    for j, ph in enumerate(STRESS_PHASES):
        for i, mode in enumerate(MODE_ORDER):
            vals = mode_phase_ttfs[mode].get(ph, [])
            pos = j * (len(MODE_ORDER) + 0.5) + i
            positions.append(pos)
            data_series.append(vals if vals else [np.nan])
            colors.append(MODE_COLORS[mode])

    bp = ax.boxplot(data_series, positions=positions, widths=0.6, patch_artist=True,
                     medianprops={"color": "black"})
    for patch, c in zip(bp["boxes"], colors):
        patch.set_facecolor(c)
        patch.set_alpha(0.7)
    for pos, vals in zip(positions, data_series):
        valid = [v for v in vals if not np.isnan(v)]
        if valid:
            jitter = RNG.uniform(-0.15, 0.15, len(valid))
            ax.scatter([pos] * len(valid) + jitter, valid, color="black", s=15, alpha=DOT_ALPHA, zorder=3)

    ax.set_xticks([j * (len(MODE_ORDER) + 0.5) + (len(MODE_ORDER) - 1) / 2 for j in range(len(STRESS_PHASES))])
    ax.set_xticklabels(STRESS_PHASES, fontsize=TICK_SZ, rotation=20)
    ax.set_ylabel("TTFS (s)", fontsize=LABEL_SZ)
    ax.set_title("G3 — Time-to-First-Spawn by Mode & Phase", fontsize=TITLE_SZ, fontweight="bold")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    from matplotlib.patches import Patch
    legend_patches = [Patch(facecolor=MODE_COLORS[m], alpha=0.7, label=m) for m in MODE_ORDER]
    ax.legend(handles=legend_patches, fontsize=TICK_SZ)
    fig.tight_layout()
    fig.savefig(out_dir / "g3_ttfs_distribution.png", dpi=150)
    plt.close(fig)


def graph_g4(runs: list[dict], out_dir: Path):
    """Grouped bar: per-phase p50 latency per mode."""
    mode_phase_p50 = {m: defaultdict(list) for m in MODE_ORDER}
    for r in runs:
        phase_lats = defaultdict(list)
        for row in r["client_reqs"]:
            try:
                lat = float(row.get("latency_s", row.get("latency", 0)))
            except (ValueError, TypeError):
                continue
            ph = row.get("phase", "")
            if ph:
                phase_lats[ph].append(lat)
        for ph in ALL_PHASES:
            if phase_lats[ph]:
                mode_phase_p50[r["mode"]][ph].append(np.percentile(phase_lats[ph], 50))

    fig, ax = plt.subplots(figsize=FIG_WIDE)
    n_phases = len(ALL_PHASES)
    n_modes = len(MODE_ORDER)
    bar_w = 0.7 / n_modes
    x = np.arange(n_phases)

    for i, mode in enumerate(MODE_ORDER):
        means = [np.mean(mode_phase_p50[mode].get(ph, [0])) * 1000 for ph in ALL_PHASES]
        sems = [np.std(mode_phase_p50[mode].get(ph, [0])) * 1000 / max(np.sqrt(len(mode_phase_p50[mode].get(ph, [0]))), 1) for ph in ALL_PHASES]
        offset = (i - n_modes / 2 + 0.5) * bar_w
        ax.bar(x + offset, means, bar_w, yerr=sems, capsize=4,
               color=MODE_COLORS[mode], alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8, label=mode)
        for j, ph in enumerate(ALL_PHASES):
            vals = [v * 1000 for v in mode_phase_p50[mode].get(ph, [])]
            if vals:
                jitter = RNG.uniform(-bar_w * 0.3, bar_w * 0.3, len(vals))
                ax.scatter(x[j] + offset + jitter, vals, color=MODE_EDGES[mode], s=25, alpha=DOT_ALPHA, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(ALL_PHASES, fontsize=TICK_SZ, rotation=25, ha="right")
    ax.set_ylabel("p50 latency (ms)", fontsize=LABEL_SZ)
    ax.set_title("G4 — Per-Phase p50 Latency by Mode", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(fontsize=TICK_SZ)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g4_per_phase_p50.png", dpi=150)
    plt.close(fig)


def graph_g5(runs: list[dict], out_dir: Path):
    """Grouped bar: baseline p50 latency only."""
    mode_lats = defaultdict(list)
    for r in runs:
        lats = []
        for row in r["client_reqs"]:
            try:
                lat = float(row.get("latency_s", row.get("latency", 0)))
            except (ValueError, TypeError):
                continue
            if row.get("phase", "") == "baseline":
                lats.append(lat)
        if lats:
            mode_lats[r["mode"]].append(np.percentile(lats, 50) * 1000)

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    x = np.arange(len(MODE_ORDER))
    means = [np.mean(mode_lats[m]) if mode_lats[m] else 0 for m in MODE_ORDER]
    sems = [np.std(mode_lats[m]) / max(np.sqrt(len(mode_lats[m])), 1) if mode_lats[m] else 0 for m in MODE_ORDER]
    ax.bar(x, means, yerr=sems, capsize=5, color=[MODE_COLORS[m] for m in MODE_ORDER],
           alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8)
    for i, m in enumerate(MODE_ORDER):
        vals = mode_lats[m]
        if vals:
            jitter = RNG.uniform(-0.25, 0.25, len(vals))
            ax.scatter(x[i] + jitter, vals, color=MODE_EDGES[m], s=30, alpha=DOT_ALPHA, zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels(MODE_LABELS, fontsize=TICK_SZ, rotation=15)
    ax.set_ylabel("p50 latency (ms)", fontsize=LABEL_SZ)
    ax.set_title("G5 — Baseline p50 Latency by Mode", fontsize=TITLE_SZ, fontweight="bold")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g5_baseline_p50.png", dpi=150)
    plt.close(fig)


def graph_g5b(runs: list[dict], out_dir: Path):
    """Grouped bar: p50 latency aggregated by phase type."""
    mode_type_p50 = {m: defaultdict(list) for m in MODE_ORDER}
    for r in runs:
        type_lats = defaultdict(list)
        for row in r["client_reqs"]:
            try:
                lat = float(row.get("latency_s", row.get("latency", 0)))
            except (ValueError, TypeError):
                continue
            ph = row.get("phase", "")
            if ph:
                ptype = PHASE_TYPE.get(ph, ph)
                type_lats[ptype].append(lat)
        for ptype in PHASE_TYPE_ORDER:
            if type_lats[ptype]:
                mode_type_p50[r["mode"]][ptype].append(np.percentile(type_lats[ptype], 50) * 1000)

    fig, ax = plt.subplots(figsize=FIG_MULTI)
    n_types = len(PHASE_TYPE_ORDER)
    n_modes = len(MODE_ORDER)
    bar_w = 0.7 / n_modes
    x = np.arange(n_types)

    for i, mode in enumerate(MODE_ORDER):
        means = [np.mean(mode_type_p50[mode].get(pt, [0])) for pt in PHASE_TYPE_ORDER]
        sems = [np.std(mode_type_p50[mode].get(pt, [0])) / max(np.sqrt(len(mode_type_p50[mode].get(pt, [0]))), 1) for pt in PHASE_TYPE_ORDER]
        offset = (i - n_modes / 2 + 0.5) * bar_w
        ax.bar(x + offset, means, bar_w, yerr=sems, capsize=4,
               color=MODE_COLORS[mode], alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8, label=mode)
        for j, pt in enumerate(PHASE_TYPE_ORDER):
            vals = mode_type_p50[mode].get(pt, [])
            if vals:
                jitter = RNG.uniform(-bar_w * 0.3, bar_w * 0.3, len(vals))
                ax.scatter(x[j] + offset + jitter, vals, color=MODE_EDGES[mode], s=25, alpha=DOT_ALPHA, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(PHASE_TYPE_ORDER, fontsize=TICK_SZ)
    ax.set_ylabel("p50 latency (ms)", fontsize=LABEL_SZ)
    ax.set_title("G5b — Latency by Phase Type", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(fontsize=TICK_SZ)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g5b_phase_type_p50.png", dpi=150)
    plt.close(fig)


def graph_g6(runs: list[dict], out_dir: Path):
    """Grouped bar: per-phase timeout rate (latency >= 29.9s)."""
    mode_phase_timeout = {m: defaultdict(list) for m in MODE_ORDER}
    for r in runs:
        phase_total = defaultdict(int)
        phase_timeouts = defaultdict(int)
        for row in r["client_reqs"]:
            try:
                lat = float(row.get("latency_s", row.get("latency", 0)))
            except (ValueError, TypeError):
                continue
            ph = row.get("phase", "")
            if ph:
                phase_total[ph] += 1
                if lat >= (CURL_MAX_TIME - 0.1):
                    phase_timeouts[ph] += 1
        for ph in ALL_PHASES:
            if phase_total[ph] > 0:
                mode_phase_timeout[r["mode"]][ph].append(phase_timeouts[ph] / phase_total[ph] * 100)

    fig, ax = plt.subplots(figsize=FIG_MULTI)
    n_phases = len(ALL_PHASES)
    n_modes = len(MODE_ORDER)
    bar_w = 0.7 / n_modes
    x = np.arange(n_phases)

    for i, mode in enumerate(MODE_ORDER):
        means = [np.mean(mode_phase_timeout[mode].get(ph, [0])) for ph in ALL_PHASES]
        sems = [np.std(mode_phase_timeout[mode].get(ph, [0])) / max(np.sqrt(len(mode_phase_timeout[mode].get(ph, [0]))), 1) for ph in ALL_PHASES]
        offset = (i - n_modes / 2 + 0.5) * bar_w
        ax.bar(x + offset, means, bar_w, yerr=sems, capsize=4,
               color=MODE_COLORS[mode], alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8, label=mode)
        for j, ph in enumerate(ALL_PHASES):
            vals = mode_phase_timeout[mode].get(ph, [])
            if vals:
                jitter = RNG.uniform(-bar_w * 0.3, bar_w * 0.3, len(vals))
                ax.scatter(x[j] + offset + jitter, vals, color=MODE_EDGES[mode], s=25, alpha=DOT_ALPHA, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(ALL_PHASES, fontsize=TICK_SZ, rotation=25, ha="right")
    ax.set_ylabel("Timeout rate (%)", fontsize=LABEL_SZ)
    ax.set_title("G6 — Timeout Rate by Mode & Phase", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(fontsize=TICK_SZ)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g6_timeout_rate.png", dpi=150)
    plt.close(fig)


def graph_g7(runs: list[dict], out_dir: Path):
    """Grouped bar: completed requests per stress phase per mode."""
    mode_phase_tp = {m: defaultdict(list) for m in MODE_ORDER}
    for r in runs:
        phase_reqs = defaultdict(int)
        for row in r["client_reqs"]:
            ph = row.get("phase", "")
            if ph:
                phase_reqs[ph] += 1
        for ph in STRESS_PHASES:
            dur = phase_boundaries(r["phases"]).get(ph, (0, 0))[1] - phase_boundaries(r["phases"]).get(ph, (0, 0))[0]
            if phase_reqs[ph] > 0 and dur > 0:
                mode_phase_tp[r["mode"]][ph].append(phase_reqs[ph] / dur)

    fig, ax = plt.subplots(figsize=FIG_MULTI)
    n_phases = len(STRESS_PHASES)
    n_modes = len(MODE_ORDER)
    bar_w = 0.7 / n_modes
    x = np.arange(n_phases)

    for i, mode in enumerate(MODE_ORDER):
        means = [np.mean(mode_phase_tp[mode].get(ph, [0])) for ph in STRESS_PHASES]
        sems = [np.std(mode_phase_tp[mode].get(ph, [0])) / max(np.sqrt(len(mode_phase_tp[mode].get(ph, [0]))), 1) for ph in STRESS_PHASES]
        offset = (i - n_modes / 2 + 0.5) * bar_w
        ax.bar(x + offset, means, bar_w, yerr=sems, capsize=4,
               color=MODE_COLORS[mode], alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8, label=mode)
        for j, ph in enumerate(STRESS_PHASES):
            vals = mode_phase_tp[mode].get(ph, [])
            if vals:
                jitter = RNG.uniform(-bar_w * 0.3, bar_w * 0.3, len(vals))
                ax.scatter(x[j] + offset + jitter, vals, color=MODE_EDGES[mode], s=25, alpha=DOT_ALPHA, zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels(STRESS_PHASES, fontsize=TICK_SZ, rotation=20)
    ax.set_ylabel("Throughput (req/s)", fontsize=LABEL_SZ)
    ax.set_title("G7 — Throughput by Mode & Stress Phase", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(fontsize=TICK_SZ)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g7_throughput.png", dpi=150)
    plt.close(fig)


def graph_g8(runs: list[dict], out_dir: Path):
    """3-panel line chart: CPU + latency score components over time, median replicate per mode."""
    fig, axes = plt.subplots(1, 3, figsize=FIG_DIAG, sharey=True)

    for ax, mode in zip(axes, MODE_ORDER):
        mode_runs = [r for r in runs if r["mode"] == mode]
        if not mode_runs:
            ax.set_title(f"{mode} (no data)")
            continue
        # Pick median replicate by total spawn count
        spawn_totals = []
        for r in mode_runs:
            s = 0
            for ev in r["elasticity"]:
                if not is_reserve_event(ev) and is_spawn_event(ev):
                    s += 1
            spawn_totals.append(s)
        median_idx = np.argsort(spawn_totals)[len(spawn_totals) // 2]
        med_run = mode_runs[median_idx]

        policy_rows = med_run.get("policy_state", [])
        if not policy_rows:
            ax.set_title(f"{mode} (no policy state)")
            continue

        times = []
        cpu_comp = []
        lat_comp = []
        for pr in policy_rows:
            t = parse_dt(pr.get("timestamp_s", pr.get("timestamp", pr.get("window_end_s", "0"))))
            times.append(t)
            cpu_comp.append(float(pr.get("compute_score", pr.get("score", 0))))
            lat_comp.append(float(pr.get("storage_score", 0)))
        
        # Make times relative to first timestamp so phase shading aligns
        if times:
            t0 = times[0]
            times = [t - t0 for t in times]

        ax.plot(times, cpu_comp, color="#F44336", linewidth=2.0, label="CPU component")
        ax.plot(times, lat_comp, color="#2196F3", linewidth=2.0, label="Latency component")
        ax.set_title(mode, fontsize=TITLE_SZ, fontweight="bold")
        ax.set_xlabel("Time (s)", fontsize=LABEL_SZ)
        if ax == axes[0]:
            ax.set_ylabel("Score component", fontsize=LABEL_SZ)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
        phase_map = phase_boundaries(med_run["phases"])
        for ph, (start, end) in phase_map.items():
            if ph in STRESS_PHASES:
                ax.axvspan(start, end, alpha=0.12, color="gray")
        if mode == MODE_ORDER[0]:
            ax.legend(fontsize=ANNO_SZ)

    fig.suptitle("G8 — Score Component Decomposition (median replicate per mode)", fontsize=TITLE_SZ, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_dir / "g8_score_components.png", dpi=150)
    plt.close(fig)


def graph_g7b(runs: list[dict], out_dir: Path):
    """Throughput per resource-time: (total reqs) / (total node-seconds)."""
    mode_tpr = defaultdict(list)
    for r in runs:
        total_reqs = len(r["client_reqs"])
        node_seconds = 0.0
        for ev in r["elasticity"]:
            ts = parse_dt(ev.get("timestamp_s", ev.get("timestamp", "0")))
            if is_reserve_event(ev):
                continue
            if is_node_addition(ev):
                # Rough: assume node lives until end of run
                last_ts = phase_boundaries(r["phases"]).get("demand_drop", (0, 1440))[1]
                node_seconds += max(0, last_ts - ts)
        if node_seconds > 0:
            mode_tpr[r["mode"]].append(total_reqs / node_seconds)

    fig, ax = plt.subplots(figsize=FIG_SINGLE)
    x = np.arange(len(MODE_ORDER))
    means = [np.mean(mode_tpr[m]) if mode_tpr[m] else 0 for m in MODE_ORDER]
    sems = [np.std(mode_tpr[m]) / max(np.sqrt(len(mode_tpr[m])), 1) if mode_tpr[m] else 0 for m in MODE_ORDER]
    ax.bar(x, means, yerr=sems, capsize=5, color=[MODE_COLORS[m] for m in MODE_ORDER],
           alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8)
    for i, m in enumerate(MODE_ORDER):
        vals = mode_tpr[m]
        if vals:
            jitter = RNG.uniform(-0.25, 0.25, len(vals))
            ax.scatter(x[i] + jitter, vals, color=MODE_EDGES[m], s=30, alpha=DOT_ALPHA, zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels(MODE_LABELS, fontsize=TICK_SZ, rotation=15)
    ax.set_ylabel("Throughput per node-second", fontsize=LABEL_SZ)
    ax.set_title("G7b — Throughput-per-Resource by Mode", fontsize=TITLE_SZ, fontweight="bold")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout()
    fig.savefig(out_dir / "g7b_throughput_per_resource.png", dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="RQ3 cross-mode comparison graphs")
    parser.add_argument("--run", action="append", required=True,
                        help="Run spec: label:mode:path (repeat for each run)")
    parser.add_argument("--out-dir", required=True, help="Output directory for graphs")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = []
    for spec in args.run:
        parts = spec.split(":", 2)
        if len(parts) != 3:
            print(f"ERROR: invalid --run spec: {spec}", file=sys.stderr)
            sys.exit(1)
        label, mode, path = parts
        run_dir = Path(path)
        if not run_dir.is_dir():
            print(f"ERROR: run dir not found: {run_dir}", file=sys.stderr)
            sys.exit(1)

        run_data = {
            "label": label,
            "mode": mode,
            "path": run_dir,
            "elasticity": load_csv(run_dir, "elasticity_events.csv"),
            "client_reqs": load_csv(run_dir, "client_requests.csv"),
            "policy_state": load_csv(run_dir, "policy_state.csv"),
            "phases": load_phases(run_dir),
        }
        runs.append(run_data)
        print(f"Loaded {label} ({mode}): {len(run_data['elasticity'])} elasticity events, "
              f"{len(run_data['client_reqs'])} client reqs, "
              f"{len(run_data['policy_state'])} policy state rows")

    if len(runs) != 9:
        print(f"WARNING: expected 9 runs, got {len(runs)}", file=sys.stderr)

    print(f"\nGenerating graphs to {out_dir}...")

    print("  G1 — Baseline FP Spawns...")
    graph_g1(runs, out_dir)
    print("  G1b — FP Score Components...")
    graph_g1b(runs, out_dir)
    print("  G2 — Stress Spawn Count...")
    graph_g2(runs, out_dir)
    print("  G3 — TTFS Distribution...")
    graph_g3(runs, out_dir)

    print("  G4 — Per-Phase p50 Latency...")
    graph_g4(runs, out_dir)
    print("  G5 — Baseline p50 Latency...")
    graph_g5(runs, out_dir)
    print("  G5b — Latency by Phase Type...")
    graph_g5b(runs, out_dir)
    print("  G6 — Timeout Rate...")
    graph_g6(runs, out_dir)
    print("  G7 — Throughput...")
    graph_g7(runs, out_dir)

    print("  G7b — Throughput-per-Resource...")
    graph_g7b(runs, out_dir)

    print("  G8 — Score Component Decomposition...")
    graph_g8(runs, out_dir)

    print(f"\nDone. {len(list(out_dir.glob('*.png')))} graphs written to {out_dir}")


if __name__ == "__main__":
    main()
