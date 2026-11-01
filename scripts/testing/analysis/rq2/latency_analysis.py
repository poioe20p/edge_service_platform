import csv, os, sys
import numpy as np
from collections import defaultdict

metrics = "/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics"
runs = [
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

mode_lat = defaultdict(list)
mode_fail = defaultdict(int)
mode_phase_lat = defaultdict(lambda: defaultdict(list))

for run_dir, mode in runs:
    path = os.path.join(metrics, run_dir, "client_requests.csv")
    if not os.path.exists(path):
        print("MISSING: " + path)
        continue
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            status = row.get("http_status", "")
            lat_s = row.get("latency_s", "")
            if status == "200" and lat_s:
                lat_ms = float(lat_s) * 1000
                mode_lat[mode].append(lat_ms)
                phase = row.get("phase", "")
                if "storm" in phase or "spike" in phase:
                    mode_phase_lat[mode]["stress"].append(lat_ms)
                else:
                    mode_phase_lat[mode]["non_stress"].append(lat_ms)
            elif status not in ("200", "0", ""):
                mode_fail[mode] += 1

print("=" * 80)
print("RQ2 CAMPAIGN — PER-MODE LATENCY ANALYSIS")
print("=" * 80)
for mode in ["topology_host", "topology_slowstart", "topology_lifecycle"]:
    lats = mode_lat[mode]
    if not lats:
        print(mode + ": NO DATA")
        continue
    fails = mode_fail[mode]
    total = len(lats) + fails
    p50 = np.percentile(lats, 50)
    p95 = np.percentile(lats, 95)
    p99 = np.percentile(lats, 99)
    fr = fails / max(total, 1) * 100
    print("")
    print("--- " + mode + " ---")
    print("  Requests: " + str(len(lats)))
    print("  Failures: " + str(fails) + " (" + str(round(fr, 2)) + "%)")
    print("  p50: " + str(round(p50, 1)) + " ms")
    print("  p95: " + str(round(p95, 1)) + " ms")
    print("  p99: " + str(round(p99, 1)) + " ms")
    for phase_type in ["stress", "non_stress"]:
        pl = mode_phase_lat[mode][phase_type]
        if pl:
            print("  " + phase_type + ": p50=" + str(round(np.percentile(pl, 50), 1)) +
                  "ms, p95=" + str(round(np.percentile(pl, 95), 1)) + "ms, n=" + str(len(pl)))

# Also compute per-phase breakdown
print("")
print("=" * 80)
print("PER-PHASE LATENCY (all modes combined)")
print("=" * 80)
phase_lat = defaultdict(list)
for run_dir, mode in runs:
    path = os.path.join(metrics, run_dir, "client_requests.csv")
    if not os.path.exists(path):
        continue
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            if row.get("http_status") == "200" and row.get("latency_s"):
                phase_lat[row.get("phase", "?")].append(float(row["latency_s"]) * 1000)

for phase in sorted(phase_lat.keys()):
    lats = phase_lat[phase]
    if lats:
        print(phase + ": n=" + str(len(lats)) +
              ", p50=" + str(round(np.percentile(lats, 50), 1)) +
              "ms, p95=" + str(round(np.percentile(lats, 95), 1)) + "ms")
