"""
Deep-dive: per-request latency analysis across RQ2 modes.
Breaks down latency by mode, phase, and LAN to identify
whether the host latency penalty is real and where it comes from.
"""
import csv, os
from collections import defaultdict

base = r"c:\Users\themo\Documents\Trabalhos Academicos\Mestrado - Tese\efficient-storage-in-edge-scenarios\source\scripts\testing\metrics"
modes_map = {"th": "host", "ss": "slowstart", "tl": "lifecycle"}

# Collect: mode -> phase -> LAN -> [latencies]
data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

for folder in sorted(os.listdir(base)):
    if "_rq2_" not in folder:
        continue
    mk = folder.split("_rq2_")[1].split("_")[0]
    mode = modes_map.get(mk, mk)
    fpath = os.path.join(base, folder, "client_requests.csv")
    if not os.path.exists(fpath):
        continue

    with open(fpath) as f:
        for row in csv.DictReader(f):
            phase = row.get("phase", "unknown")
            lan = row.get("target_region", row.get("client_lan", "unknown"))
            lat_s = row.get("latency_s", "")
            if not lat_s or not lat_s.strip():
                continue
            lat_ms = float(lat_s) * 1000
            data[mode][phase][lan].append(lat_ms)

# ── Summary per mode ──
print("=" * 85)
print(f"{'Mode':<12} {'Phase':<16} {'LAN':<6} {'n':>7} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8}")
print("-" * 85)

for mode in ["host", "slowstart", "lifecycle"]:
    for phase in sorted(data[mode].keys()):
        for lan in sorted(data[mode][phase].keys()):
            vals = sorted(data[mode][phase][lan])
            if not vals:
                continue
            n = len(vals)
            p50 = vals[n // 2]
            p95 = vals[int(n * 0.95)]
            p99 = vals[int(n * 0.99)]
            mean = sum(vals) / n
            print(f"{mode:<12} {phase:<16} {lan:<6} {n:>7} {p50:>7.0f}ms {p95:>7.0f}ms {p99:>7.0f}ms {mean:>7.0f}ms")

# ── Aggregate (all phases, all LANs) ──
print("\n--- Aggregate (all phases, all LANs) ---")
for mode in ["host", "slowstart", "lifecycle"]:
    all_vals = []
    for phase in data[mode]:
        for lan in data[mode][phase]:
            all_vals.extend(data[mode][phase][lan])
    if all_vals:
        all_vals.sort()
        n = len(all_vals)
        p50 = all_vals[n // 2]
        p95 = all_vals[int(n * 0.95)]
        p99 = all_vals[int(n * 0.99)]
        mean = sum(all_vals) / n
        print(f"  {mode:<12} n={n:>7}  p50={p50:>7.0f}ms  p95={p95:>7.0f}ms  p99={p99:>7.0f}ms  mean={mean:>7.0f}ms")

# ── Stress vs non-stress aggregate ──
print("\n--- Stress vs Non-Stress (all modes, all LANs) ---")
stress_phases = {"storage_storm", "compute_spike"}
for mode in ["host", "slowstart", "lifecycle"]:
    stress_vals = []
    nonstress_vals = []
    for phase in data[mode]:
        for lan in data[mode][phase]:
            if phase in stress_phases:
                stress_vals.extend(data[mode][phase][lan])
            else:
                nonstress_vals.extend(data[mode][phase][lan])
    
    for label, vals in [("stress", stress_vals), ("non-stress", nonstress_vals)]:
        if vals:
            vals.sort()
            n = len(vals)
            p50 = vals[n // 2]
            p95 = vals[int(n * 0.95)]
            mean = sum(vals) / n
            print(f"  {mode:<12} {label:<12} n={n:>7}  p50={p50:>7.0f}ms  p95={p95:>7.0f}ms  mean={mean:>7.0f}ms")
