#!/usr/bin/env python3
"""RQ3 v6 extended graphs: G9-G12 from container_events.csv and elasticity_events.csv.

G9  — Cumulative Resource-Time by Mode & Tier
G10 — Dynamic Node Count Over Time (3-panel)
G10b — Peak & Mean Node Count
G11 — Cross-Tier Spawn Contamination
G12 — Node Lifetime Distribution
"""
import csv, os, argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

MODE_ORDER = ["degradation_score", "cpu_only", "latency_only"]
MODE_COLORS = {"degradation_score": "#4CAF50", "cpu_only": "#F44336", "latency_only": "#2196F3"}
MODE_EDGES = {"degradation_score": "#1B5E20", "cpu_only": "#B71C1C", "latency_only": "#0D47A1"}
UTC = timezone.utc

STRESS_PHASES = ["storage_storm", "tier1_hotspot", "reverse_hotspot", "compute_spike"]
ALL_PHASES = ["baseline","storage_storm","tier1_hotspot","inter_hotspot_cooldown",
              "reverse_hotspot","compute_spike","demand_drop"]
TITLE_SZ, LABEL_SZ, TICK_SZ, ANNO_SZ = 13, 12, 10, 9
BAR_ALPHA, GRID_ALPHA, DOT_ALPHA = 0.78, 0.22, 0.55
RNG = np.random.default_rng(42)

def parse_client_dt(ts_str):
    ts_str = ts_str.strip().strip('"')
    for fmt in ["%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"]:
        try: return datetime.strptime(ts_str, fmt).astimezone(UTC)
        except ValueError: continue
    return None

def parse_elast_dt(ts_str):
    ts_str = ts_str.strip().strip('"').replace(",", ".")
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)

def load_runs(base_dir, run_specs):
    runs = []
    for label, mode, folder in run_specs:
        d = Path(base_dir) / folder
        if not d.is_dir(): continue
        run = {"label": label, "mode": mode, "dir": d}
        # Load elasticity
        ep = d / "elasticity_events.csv"
        if ep.exists():
            with open(ep) as f: run["elasticity"] = list(csv.DictReader(f))
        else: run["elasticity"] = []
        # Load container events
        ce = d / "container_events.csv"
        if ce.exists():
            with open(ce) as f: run["containers"] = list(csv.DictReader(f))
        else: run["containers"] = []
        # Load client requests for experiment start
        cp = d / "client_requests.csv"
        run["exp_start"] = None
        if cp.exists():
            with open(cp) as f:
                for row in csv.DictReader(f):
                    dt = parse_client_dt(row.get("sent_at",""))
                    if dt: run["exp_start"] = dt; break
        # Load phases
        pj = d / "phases_snapshot.json"
        run["phases"] = []
        if pj.exists():
            with open(pj) as f: run["phases"] = json.load(f).get("phases",[])
        runs.append(run)
    return runs

def phase_timeline(phases, exp_start):
    """Return list of (phase_name, start_s, end_s)."""
    out = []
    t = exp_start.timestamp() if exp_start else 0
    for ph in phases:
        dur = ph.get("duration_s", 0)
        out.append((ph["name"], t, t + dur))
        t += dur
    return out

def is_spawn(ev):
    et = ev.get("event_type","").lower()
    nt = ev.get("node_type","").lower()
    d = ev.get("detail","").lower()
    if any(x in et or x in nt or x in d for x in ("standby","reserve")): return False
    return "spawning" in et or "spawn" in et


# ─── G9: Cumulative Resource-Time ───
def graph_g9(runs, out_dir):
    """Cumulative node-seconds per mode and tier."""
    mode_tier_time = {m: defaultdict(float) for m in MODE_ORDER}
    for r in runs:
        mode = r["mode"]
        timeline = phase_timeline(r["phases"], r["exp_start"])
        end_ts = max(e for _, _, e in timeline) if timeline else 1440
        for ev in r["elasticity"]:
            et = ev.get("event_type","").lower()
            nt = ev.get("node_type","").lower()
            detail = ev.get("detail","").lower()
            if "standby" in et or "standby" in nt or "standby" in detail: continue
            if "reserve" in et or "reserve" in nt or "reserve" in detail: continue
            if "add_timing" in et or "ready_timing" in et or "spawning" in et or "online" in et:
                ts = parse_elast_dt(ev.get("timestamp","0")).timestamp()
                lifetime = max(0, end_ts - ts)
                tier = nt if nt in ("compute","storage") else "other"
                mode_tier_time[mode][tier] += lifetime

    fig, ax = plt.subplots(figsize=(10, 6))
    tiers = ["compute", "storage"]
    x = np.arange(len(tiers))
    w = 0.25
    for i, mode in enumerate(MODE_ORDER):
        vals = [mode_tier_time[mode].get(t, 0) / 3600 for t in tiers]  # hours
        ax.bar(x + i*w, vals, w, color=MODE_COLORS[mode], alpha=BAR_ALPHA,
               edgecolor="black", linewidth=0.8, label=mode)
    ax.set_xticks(x + w)
    ax.set_xticklabels(tiers, fontsize=TICK_SZ)
    ax.set_ylabel("Cumulative node-hours", fontsize=LABEL_SZ)
    ax.set_title("G9 — Cumulative Resource-Time by Mode & Tier", fontsize=TITLE_SZ, fontweight="bold")
    ax.legend(fontsize=TICK_SZ)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout(); fig.savefig(out_dir / "g9_resource_time.png", dpi=150)
    plt.close(fig)


# ─── G10: Dynamic Node Count ───
def graph_g10(runs, out_dir):
    """3-panel line chart: dynamic node count over time, one panel per mode."""
    fig, axes = plt.subplots(1, 3, figsize=(20, 8), sharey=True)

    for ax, mode in zip(axes, MODE_ORDER):
        mode_runs = [r for r in runs if r["mode"] == mode]
        if not mode_runs: continue
        # Pick median replicate by total spawns
        spawns = []
        for r in mode_runs:
            s = sum(1 for ev in r["elasticity"] if is_spawn(ev))
            spawns.append(s)
        med = mode_runs[np.argsort(spawns)[len(spawns)//2]]

        timeline = phase_timeline(med["phases"], med["exp_start"])
        # Track node count over time: for each add/remove event
        # Simple approach: count active nodes from container_events
        events = []
        for ev in med["elasticity"]:
            et = ev.get("event_type","").lower()
            nt = ev.get("node_type","").lower()
            detail = ev.get("detail","").lower()
            ts = parse_elast_dt(ev.get("timestamp","0")).timestamp()
            if "add_timing" in et or "spawning" in et or "online" in et:
                if "standby" not in detail and "reserve" not in detail:
                    events.append((ts, +1))
            elif "remove_timing" in et or "removing" in et:
                events.append((ts, -1))
        events.sort()

        t0 = med["exp_start"].timestamp() if med["exp_start"] else 0
        times = [t0]
        counts = [0]
        cur = 0
        for ts, delta in events:
            times.append(ts)
            cur += delta
            counts.append(cur)
            times.append(ts)
            counts.append(cur)

        ax.plot(times, counts, color=MODE_COLORS[mode], linewidth=1.5, drawstyle="steps-post")
        ax.set_title(mode, fontsize=TITLE_SZ, fontweight="bold")
        ax.set_xlabel("Epoch time (s)", fontsize=LABEL_SZ)
        if ax == axes[0]: ax.set_ylabel("Dynamic node count", fontsize=LABEL_SZ)
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
        ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
        # Phase shading
        for ph, s, e in timeline:
            if ph in STRESS_PHASES:
                ax.axvspan(s, e, alpha=0.12, color="gray")

    fig.suptitle("G10 — Dynamic Node Count Over Time (median replicate per mode)", fontsize=TITLE_SZ, fontweight="bold")
    fig.tight_layout(); fig.savefig(out_dir / "g10_node_count_timeline.png", dpi=150)
    plt.close(fig)


# ─── G10b: Peak & Mean Node Count ───
def graph_g10b(runs, out_dir):
    """Grouped bar: peak and mean dynamic node count per mode."""
    mode_peak = defaultdict(list)
    mode_mean = defaultdict(list)
    for r in runs:
        events = []
        for ev in r["elasticity"]:
            et = ev.get("event_type","").lower()
            nt = ev.get("node_type","").lower()
            detail = ev.get("detail","").lower()
            ts = parse_elast_dt(ev.get("timestamp","0")).timestamp()
            if "standby" in et or "standby" in nt or "standby" in detail: continue
            if "reserve" in et or "reserve" in nt or "reserve" in detail: continue
            if "add_timing" in et or "spawning" in et or "online" in et:
                events.append((ts, +1))
            elif "remove_timing" in et or "removing" in et:
                events.append((ts, -1))
        if not events: continue
        events.sort()
        cur = 0; counts = []
        for ts, delta in events:
            cur += delta
            counts.append(cur)
        if counts:
            mode_peak[r["mode"]].append(max(counts))
            mode_mean[r["mode"]].append(np.mean(counts))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, metric, title, data in [
        (axes[0], "Peak", "Peak Node Count", mode_peak),
        (axes[1], "Mean", "Mean Node Count", mode_mean)]:
        x = np.arange(len(MODE_ORDER))
        means = [np.mean(data.get(m,[0])) for m in MODE_ORDER]
        sems = [np.std(data.get(m,[0]))/max(np.sqrt(len(data.get(m,[0]))),1) for m in MODE_ORDER]
        ax.bar(x, means, yerr=sems, capsize=5, color=[MODE_COLORS[m] for m in MODE_ORDER],
               alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8)
        for i, m in enumerate(MODE_ORDER):
            vals = data.get(m,[])
            if vals:
                j = RNG.uniform(-0.25,0.25,len(vals))
                ax.scatter(x[i]+j, vals, color=MODE_EDGES[m], s=30, alpha=DOT_ALPHA, zorder=3)
        ax.set_xticks(x); ax.set_xticklabels(MODE_ORDER, fontsize=TICK_SZ, rotation=15)
        ax.set_title(title, fontsize=TITLE_SZ, fontweight="bold")
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
        ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.suptitle("G10b — Peak & Mean Node Count by Mode", fontsize=TITLE_SZ, fontweight="bold")
    fig.tight_layout(); fig.savefig(out_dir / "g10b_peak_mean_nodes.png", dpi=150)
    plt.close(fig)


# ─── G11: Cross-Tier Spawn Contamination ───
def graph_g11(runs, out_dir):
    """Grouped bar: cross-tier spawns (compute spawns during storage phases, and vice versa)."""
    STORAGE_PHASES_LIST = ["storage_storm", "tier1_hotspot", "reverse_hotspot"]
    COMPUTE_PHASES_LIST = ["compute_spike"]
    mode_contam = {m: defaultdict(list) for m in MODE_ORDER}

    for r in runs:
        timeline = phase_timeline(r["phases"], r["exp_start"])
        tl_dict = {ph: (s, e) for ph, s, e in timeline}
        # Count compute spawns during storage phases
        c_in_storage = 0
        for ev in r["elasticity"]:
            if not is_spawn(ev): continue
            nt = ev.get("node_type","").lower()
            if nt != "compute": continue
            ts = parse_elast_dt(ev.get("timestamp","0")).timestamp()
            for ph in STORAGE_PHASES_LIST:
                if ph in tl_dict:
                    s, e = tl_dict[ph]
                    if s <= ts < e: c_in_storage += 1; break
        mode_contam[r["mode"]]["compute_in_storage"].append(c_in_storage)

    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(len(MODE_ORDER))
    means = [np.mean(mode_contam[m].get("compute_in_storage",[0])) for m in MODE_ORDER]
    sems = [np.std(mode_contam[m].get("compute_in_storage",[0]))/max(np.sqrt(len(mode_contam[m].get("compute_in_storage",[0]))),1) for m in MODE_ORDER]
    ax.bar(x, means, yerr=sems, capsize=5, color=[MODE_COLORS[m] for m in MODE_ORDER],
           alpha=BAR_ALPHA, edgecolor="black", linewidth=0.8)
    for i, m in enumerate(MODE_ORDER):
        vals = mode_contam[m].get("compute_in_storage",[])
        if vals:
            j = RNG.uniform(-0.25,0.25,len(vals))
            ax.scatter(x[i]+j, vals, color=MODE_EDGES[m], s=30, alpha=DOT_ALPHA, zorder=3)
    ax.set_xticks(x); ax.set_xticklabels(MODE_ORDER, fontsize=TICK_SZ, rotation=15)
    ax.set_ylabel("Compute spawns during storage phases", fontsize=LABEL_SZ)
    ax.set_title("G11 — Cross-Tier Spawn Contamination", fontsize=TITLE_SZ, fontweight="bold")
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout(); fig.savefig(out_dir / "g11_cross_tier.png", dpi=150)
    plt.close(fig)


# ─── G12: Node Lifetime Distribution ───
def graph_g12(runs, out_dir):
    """Box plot: node lifetime per mode."""
    mode_lifetimes = defaultdict(list)
    for r in runs:
        timeline = phase_timeline(r["phases"], r["exp_start"])
        tl_dict = {ph: (s, e) for ph, s, e in timeline}
        end_ts = tl_dict.get("demand_drop", (0, 1440))[1] if "demand_drop" in tl_dict else 1440
        nodes = {}  # mac -> (add_ts, tier)
        for ev in r["elasticity"]:
            et = ev.get("event_type","").lower()
            nt = ev.get("node_type","").lower()
            detail = ev.get("detail","").lower()
            mac = ev.get("mac","") or ev.get("container","")
            if "standby" in et or "standby" in nt or "standby" in detail: continue
            if "reserve" in et or "reserve" in nt or "reserve" in detail: continue
            ts = parse_elast_dt(ev.get("timestamp","0")).timestamp()
            if "add_timing" in et or "spawning" in et or "online" in et:
                if mac not in nodes:
                    nodes[mac] = (ts, nt)
            elif "remove_timing" in et or "removing" in et:
                if mac in nodes:
                    add_ts, tier = nodes.pop(mac)
                    lifetime = ts - add_ts
                    if lifetime > 0:
                        mode_lifetimes[r["mode"]].append(lifetime)
        # Remaining nodes (still alive at end)
        for mac, (add_ts, tier) in nodes.items():
            lifetime = end_ts - add_ts
            if lifetime > 0:
                mode_lifetimes[r["mode"]].append(lifetime)

    fig, ax = plt.subplots(figsize=(8, 5))
    data = [mode_lifetimes.get(m, [0]) for m in MODE_ORDER]
    bp = ax.boxplot(data, patch_artist=True, medianprops={"color":"black"})
    for patch, m in zip(bp["boxes"], MODE_ORDER):
        patch.set_facecolor(MODE_COLORS[m]); patch.set_alpha(0.7)
    for i, vals in enumerate(data):
        valid = [v for v in vals if v > 0]
        if valid:
            j = RNG.uniform(-0.15, 0.15, len(valid))
            ax.scatter([i+1]*len(valid)+j, valid, color="black", s=15, alpha=DOT_ALPHA, zorder=3)
    ax.set_xticklabels(MODE_ORDER, fontsize=TICK_SZ, rotation=15)
    ax.set_ylabel("Node lifetime (s)", fontsize=LABEL_SZ)
    ax.set_title("G12 — Node Lifetime Distribution by Mode", fontsize=TITLE_SZ, fontweight="bold")
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, alpha=GRID_ALPHA, linestyle="--")
    fig.tight_layout(); fig.savefig(out_dir / "g12_node_lifetimes.png", dpi=150)
    plt.close(fig)


# ─── Main ───
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--base-dir", default="docs/operation/testing/experiment/rq3_evaluation/v6/metrics")
    args = parser.parse_args()

    specs = []
    for s in args.run:
        parts = s.split(":", 2)
        if len(parts) == 3: specs.append((parts[0], parts[1], parts[2]))

    runs = load_runs(args.base_dir, specs)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Loaded {len(runs)} runs. Generating G9-G12...")
    graph_g9(runs, out); print("  G9 done")
    graph_g10(runs, out); print("  G10 done")
    graph_g10b(runs, out); print("  G10b done")
    graph_g11(runs, out); print("  G11 done")
    graph_g12(runs, out); print("  G12 done")
    print(f"Done. {len(list(out.glob('*.png')))} graphs in {out}")

if __name__ == "__main__":
    main()
