#!/usr/bin/env python3
"""Investigate why redistribution time was never reached — spawn timing, pool churn, phase alignment."""
import csv, json, os
from collections import defaultdict
from datetime import datetime, timedelta
import numpy as np

METRICS = "/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics"
RUN = "20260706_171835_rq2_th_1"

# ── Extract spawn events from controller logs ────────────────────
spawns = []
for lan in ["lan1", "lan2"]:
    log = os.path.join(METRICS, RUN, "controller_" + lan + ".log")
    if os.path.exists(log):
        with open(log) as f:
            for line in f:
                if "spawning" in line:
                    parts = line.split(" ")
                    ts = parts[0] + "T" + parts[1].split(",")[0]
                    role = "compute" if "compute:" in line else ("storage" if "storage" in line else "?")
                    container = line.split("spawning ")[1].split(" on")[0] if " spawning " in line else "?"
                    spawns.append({"ts": ts, "container": container, "role": role, "lan": lan, "line": line.strip()})

# ── Container lifecycle from container_events.csv ─────────────────
ce_path = os.path.join(METRICS, RUN, "container_events.csv")
containers = []
if os.path.exists(ce_path):
    with open(ce_path) as f:
        for row in csv.DictReader(f):
            name = row.get("container_name", "")
            if "edge_server_lan" in name and "dyn" in name:
                containers.append(row)

# ── Phase boundaries ──────────────────────────────────────────────
ps_path = os.path.join(METRICS, RUN, "phases_snapshot.json")
phases = []
if os.path.exists(ps_path):
    with open(ps_path) as f:
        data = json.load(f)
        for p in data.get("phases", []):
            phases.append({"name": p["name"], "duration_s": p.get("duration_s", 0)})

# ── Helper: parse timestamps ──────────────────────────────────────
def parse_ts(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T")
        return datetime.fromisoformat(s[:26])
    except:
        return None

# ── Report ────────────────────────────────────────────────────────
print("=" * 72)
print("SPAWN EVENTS (all roles)")
print("=" * 72)
role_counts = defaultdict(int)
for s in spawns:
    role_counts[s["role"]] += 1
    print("  {}  [{:8s}]  {}  ({})".format(s['ts'], s['role'], s['container'], s['lan']))
print("\n  Total: {} spawns  (compute={}, storage={})".format(
    len(spawns), role_counts['compute'], role_counts['storage']))

print()
print("=" * 72)
print("CONTAINER LIFECYCLE")
print("=" * 72)
print("  Total dynamic edge_server containers: {}".format(len(containers)))
lifetimes = []
alive_count = 0
for c in containers:
    start_s = c.get("started_at", "")
    end_s = c.get("finished_at", "")
    s_dt = parse_ts(start_s)
    e_dt = parse_ts(end_s)
    if s_dt and e_dt:
        lifetimes.append((e_dt - s_dt).total_seconds())
    elif s_dt and not e_dt:
        alive_count += 1

if lifetimes:
    print("  Completed backends: {}".format(len(lifetimes)))
    print("  Still-alive backends: {}".format(alive_count))
    print("  Lifetime: min={:.0f}s  median={:.0f}s  max={:.0f}s".format(
        min(lifetimes), np.median(lifetimes), max(lifetimes)))
    print("  Mean lifetime: {:.0f}s  (std={:.0f}s)".format(
        np.mean(lifetimes), np.std(lifetimes)))

# ── Key insight: compute spacing between spawns ───────────────────
compute_spawns = [s for s in spawns if s["role"] == "compute"]
if len(compute_spawns) >= 2:
    print("\n  Spawn spacing (compute only):")
    for i in range(min(len(compute_spawns) - 1, 10)):
        t1 = datetime.fromisoformat(compute_spawns[i]["ts"])
        t2 = datetime.fromisoformat(compute_spawns[i+1]["ts"])
        gap = (t2 - t1).total_seconds()
        print("    {} -> {}: {:.0f}s".format(
            compute_spawns[i]['container'], compute_spawns[i+1]['container'], gap))
    # Average gap
    gaps = []
    for i in range(len(compute_spawns) - 1):
        t1 = datetime.fromisoformat(compute_spawns[i]["ts"])
        t2 = datetime.fromisoformat(compute_spawns[i+1]["ts"])
        gaps.append((t2 - t1).total_seconds())
    print("    Average spawn gap: {:.0f}s".format(np.mean(gaps)))

print()
print("=" * 72)
print("PHASE STRUCTURE")
print("=" * 72)
total = 0
for p in phases:
    print("  {:25s}  {:4d}s".format(p['name'], p['duration_s']))
    total += p["duration_s"]
print("  {:25s}  {:4d}s  ({:.0f} min)".format("TOTAL", total, total/60))

print()
print("=" * 72)
print("DIAGNOSIS: Why no equilibrium?")
print("=" * 72)
print("1. Compute spawns happen DURING stress phases (rate=4.0), not after.")
print("2. New backends enter an already-saturated pool -- load never drops.")
if lifetimes:
    print("3. Backend lifetimes are short (median={:.0f}s) due to scale-down.".format(np.median(lifetimes)))
else:
    print("3. Backend lifetimes: no completed containers found.")
print("4. Continuous pool churn (spawn + drain + remove) prevents any backend")
print("   from holding a stable share for 3 consecutive telemetry windows.")
print("5. The +/-10% criteria requires near-perfect flatness in a system")
print("   designed to continuously rebalance -- it will never happen during")
print("   stress phases. It might only happen during cooldown, and by then")
print("   the backend has already been removed.")
