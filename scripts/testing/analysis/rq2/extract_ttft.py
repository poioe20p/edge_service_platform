#!/usr/bin/env python3
"""Extract time-to-first-traffic (TTFT) from per_node_stats + controller logs."""
import csv, os, re
from collections import defaultdict
from datetime import datetime
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

def parse_ts(s):
    """Parse ISO timestamp to unix seconds."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except:
        return None

def extract_spawns(run_dir):
    """Extract compute spawn events from controller logs: (unix_ts, mac, container)."""
    spawns = []
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
                    # Extract MAC: mac=00:00:00:00:02:09
                    mac_match = re.search(r"mac=([0-9a-f:]+)", line)
                    mac = mac_match.group(1) if mac_match else None
                    container = line.split("spawning ")[1].split(" on")[0] if " spawning " in line else "?"
                    if unix_ts and mac:
                        spawns.append((unix_ts, mac, container, iso_ts))
    return spawns

def extract_first_requests(run_dir):
    """For each MAC in per_node_stats, find first window with request_count > 0."""
    pns = os.path.join(METRICS, run_dir, "per_node_stats.csv")
    if not os.path.exists(pns):
        return {}
    first_req = {}
    mac_role = {}
    with open(pns) as f:
        for row in csv.DictReader(f):
            mac = row.get("server_id", "").strip()
            role = row.get("role", "").strip()
            rc = int(row.get("request_count", 0))
            we = float(row.get("window_end", 0))
            if mac and rc > 0 and mac not in first_req:
                first_req[mac] = (we, role)
            if mac and role:
                mac_role[mac] = role
    return first_req, mac_role

# ── Compute TTFT per run per mode ────────────────────────────────
mode_ttft = defaultdict(list)
mode_labels = {}

print("=" * 85)
print("TIME-TO-FIRST-TRAFFIC (TTFT) PER SPAWN EVENT")
print("=" * 85)
print("{:35s} {:>20s} {:>10s} {:>10s} {:>10s}".format(
    "Run", "Container", "Spawn TS", "First Req TS", "TTFT(s)"))
print("-" * 85)

for run_dir, mode in RUNS:
    spawns = extract_spawns(run_dir)
    first_req, mac_role = extract_first_requests(run_dir)

    matched = 0
    unmatched = 0
    for unix_ts, mac, container, iso_ts in spawns:
        if mac in first_req:
            fr_ts, role = first_req[mac]
            ttft = fr_ts - unix_ts
            if ttft >= 0 and ttft < 600:  # filter absurd values
                mode_ttft[mode].append(ttft)
                matched += 1
                mode_labels[mode] = mode
                print("{:35s} {:>20s} {:>10.0f} {:>10.0f} {:>10.1f}".format(
                    run_dir, container[:20], unix_ts, fr_ts, ttft))
            else:
                unmatched += 1
        else:
            unmatched += 1

    # Report unmatched
    if unmatched > 0:
        pass  # some spawns may not appear in per_node_stats (storage spawns, etc.)

print()
print("=" * 85)
print("PER-MODE TTFT SUMMARY")
print("=" * 85)
for mode in ["topology_host", "topology_slowstart", "topology_lifecycle"]:
    vals = mode_ttft.get(mode, [])
    if vals:
        print("{}: n={}  mean={:.1f}s  median={:.1f}s  p95={:.1f}s  min={:.1f}s  max={:.1f}s".format(
            mode, len(vals), np.mean(vals), np.median(vals),
            np.percentile(vals, 95), min(vals), max(vals)))
    else:
        print("{}: NO DATA".format(mode))

# Effect size
print()
print("=" * 85)
print("EFFECT SIZE (Cohen's d) for TTFT")
print("=" * 85)
for a_mode, b_mode in [("topology_slowstart", "topology_lifecycle"),
                         ("topology_host", "topology_lifecycle"),
                         ("topology_host", "topology_slowstart")]:
    a = mode_ttft.get(a_mode, [])
    b = mode_ttft.get(b_mode, [])
    if a and b:
        m1, m2 = np.mean(a), np.mean(b)
        s1, s2 = np.std(a, ddof=1), np.std(b, ddof=1)
        pooled = np.sqrt((s1**2 + s2**2) / 2)
        d = (m1 - m2) / pooled if pooled > 0 else 0
        verdict = "LARGE" if abs(d) >= 1.2 else ("MEDIUM" if abs(d) >= 0.5 else "SMALL")
        print("  {} vs {}: d={:.2f} ({})".format(a_mode, b_mode, d, verdict))
