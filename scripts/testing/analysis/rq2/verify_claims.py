#!/usr/bin/env python3
"""Verify all RQ2 analysis claims against raw run data on cloud VM."""
import csv, os, json
from collections import defaultdict
import numpy as np

METRICS = "/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics"

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

# ═══════════════════════════════════════════════════════════════════
# 1. VERIFY INITIAL LOAD SHARE
# ═══════════════════════════════════════════════════════════════════
print("=" * 80)
print("1. INITIAL LOAD SHARE (from rq2_redistribution_profile.csv)")
print("=" * 80)
for run_dir, mode in RUNS:
    path = os.path.join(METRICS, run_dir, "analysis", "rq2_redistribution_profile.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                t = float(row.get("time_since_spawn_s", -1))
                share = float(row.get("mean_share", -1))
                n = row.get("n_events", "?")
                if t == 0:
                    print(f"  {run_dir} [{mode}]  time_since_spawn={t}  mean_share={share:.4f}  n={n}")
    else:
        print(f"  {run_dir} [{mode}]  MISSING profile CSV")

# Compute per-mode means
mode_shares = defaultdict(list)
for run_dir, mode in RUNS:
    path = os.path.join(METRICS, run_dir, "analysis", "rq2_redistribution_profile.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                if float(row.get("time_since_spawn_s", -1)) == 0:
                    mode_shares[mode].append(float(row["mean_share"]))
print()
for mode in ["topology_host", "topology_slowstart", "topology_lifecycle"]:
    vals = mode_shares[mode]
    print(f"  {mode}: values={[f'{v:.4f}' for v in vals]}  mean={np.mean(vals):.4f}  std={np.std(vals):.4f}")

# ═══════════════════════════════════════════════════════════════════
# 2. VERIFY LATENCY PER RUN
# ═══════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("2. LATENCY PER RUN (from client_requests.csv)")
print("=" * 80)
mode_lat = defaultdict(list)
for run_dir, mode in RUNS:
    path = os.path.join(METRICS, run_dir, "client_requests.csv")
    if not os.path.exists(path):
        print(f"  {run_dir} MISSING")
        continue
    lats = []
    fails = 0
    total = 0
    with open(path) as f:
        for row in csv.DictReader(f):
            total += 1
            status = row.get("http_status", "")
            lat_s = row.get("latency_s", "")
            if status == "200" and lat_s:
                lats.append(float(lat_s) * 1000)
            elif status not in ("200", "0", ""):
                fails += 1
    if lats:
        p50 = np.percentile(lats, 50)
        p95 = np.percentile(lats, 95)
        p99 = np.percentile(lats, 99)
        fr = fails / max(total, 1) * 100
        mode_lat[mode].extend(lats)
        print(f"  {run_dir} [{mode}]  n={len(lats)}  total={total}  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms  fails={fails} ({fr:.2f}%)")
    else:
        print(f"  {run_dir} [{mode}]  NO DATA")

print()
for mode in ["topology_host", "topology_slowstart", "topology_lifecycle"]:
    lats = mode_lat[mode]
    if lats:
        print(f"  {mode} AGGREGATE: n={len(lats)}  p50={np.percentile(lats,50):.1f}ms  p95={np.percentile(lats,95):.1f}ms  p99={np.percentile(lats,99):.1f}ms")

# ═══════════════════════════════════════════════════════════════════
# 3. VERIFY CONTROLLER LOGS FOR SPAWN EVENTS
# ═══════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("3. SPAWN EVENTS (from controller logs)")
print("=" * 80)
for run_dir, mode in RUNS:
    for lan in ["lan1", "lan2"]:
        log_path = os.path.join(METRICS, run_dir, f"controller_{lan}.log")
        if os.path.exists(log_path):
            count = 0
            with open(log_path) as f:
                for line in f:
                    if "spawning" in line:
                        count += 1
            print(f"  {run_dir}  {lan}: {count} spawn lines")

# ═══════════════════════════════════════════════════════════════════
# 4. VERIFY REDISTRIBUTION SUMMARY (equilibrium events)
# ═══════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("4. REDISTRIBUTION SUMMARY (equilibrium events)")
print("=" * 80)
total_events = 0
equil_events = 0
for run_dir, mode in RUNS:
    path = os.path.join(METRICS, run_dir, "analysis", "rq2_redistribution_summary.csv")
    if os.path.exists(path):
        with open(path) as f:
            rows = list(csv.DictReader(f))
            n = len(rows)
            n_equil = sum(1 for r in rows if r.get("redistribution_s", "").strip())
            total_events += n
            equil_events += n_equil
            print(f"  {run_dir} [{mode}]: {n} events, {n_equil} with equilibrium")

print(f"\n  TOTAL: {total_events} events, {equil_events} reached equilibrium")

# ═══════════════════════════════════════════════════════════════════
# 5. VERIFY FAILURE RATES PER RUN
# ═══════════════════════════════════════════════════════════════════
print()
print("=" * 80)
print("5. FAILURE RATE PER RUN (non-200, non-0 status codes)")
print("=" * 80)
for run_dir, mode in RUNS:
    path = os.path.join(METRICS, run_dir, "client_requests.csv")
    if not os.path.exists(path):
        continue
    total = 0
    fails = 0
    status0 = 0
    status200 = 0
    other = defaultdict(int)
    with open(path) as f:
        for row in csv.DictReader(f):
            total += 1
            s = row.get("http_status", "")
            if s == "200":
                status200 += 1
            elif s == "0":
                status0 += 1
            else:
                fails += 1
                other[s] += 1
    fr = fails / max(total, 1) * 100
    print(f"  {run_dir} [{mode}]: total={total}  200={status200}  0={status0}  FAIL={fails} ({fr:.2f}%)  other_statuses={dict(other)}")

print()
print("=" * 80)
print("VERIFICATION COMPLETE")
print("=" * 80)
