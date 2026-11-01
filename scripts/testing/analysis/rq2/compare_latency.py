"""Quick latency comparison across RQ2 modes."""
import csv, os, glob
from collections import defaultdict
import statistics

base = r"c:\Users\themo\Documents\Trabalhos Academicos\Mestrado - Tese\efficient-storage-in-edge-scenarios\source\scripts\testing\metrics"

modes = {"th": "host", "ss": "slowstart", "tl": "lifecycle"}
results = defaultdict(list)

for folder in sorted(os.listdir(base)):
    if "_rq2_" not in folder:
        continue
    mode_key = folder.split("_rq2_")[1].split("_")[0]
    mode = modes.get(mode_key, mode_key)
    fpath = os.path.join(base, folder, "per_node_stats.csv")
    if not os.path.exists(fpath):
        continue
    with open(fpath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["role"] != "compute":
                continue
            proc_s = row.get("avg_time_proc_ms", "")
            db_s = row.get("avg_time_db_ms", "")
            if not proc_s or not proc_s.strip():
                continue
            total = float(proc_s) + (float(db_s) if db_s and db_s.strip() else 0)
            results[mode].append(total)

print("=" * 65)
print(f"{'Mode':<22} {'n':>6} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8}")
print("-" * 65)
for mode in ["host", "slowstart", "lifecycle"]:
    vals = sorted(results[mode])
    if not vals:
        print(f"{mode:<22} {'--':>6}")
        continue
    n = len(vals)
    p50 = vals[n // 2]
    p95 = vals[int(n * 0.95)]
    p99 = vals[int(n * 0.99)]
    mean = sum(vals) / n
    print(f"{mode:<22} {n:>6} {p50:>7.1f}ms {p95:>7.1f}ms {p99:>7.1f}ms {mean:>7.1f}ms")

# Also compute by run
print("\n--- Per-run breakdown ---")
for folder in sorted(os.listdir(base)):
    if "_rq2_" not in folder:
        continue
    mode_key = folder.split("_rq2_")[1].split("_")[0]
    mode = modes.get(mode_key, mode_key)
    fpath = os.path.join(base, folder, "per_node_stats.csv")
    if not os.path.exists(fpath):
        continue
    run_vals = []
    with open(fpath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["role"] != "compute":
                continue
            proc_s = row.get("avg_time_proc_ms", "")
            db_s = row.get("avg_time_db_ms", "")
            if not proc_s or not proc_s.strip():
                continue
            total = float(proc_s) + (float(db_s) if db_s and db_s.strip() else 0)
            run_vals.append(total)
    if run_vals:
        run_vals.sort()
        n = len(run_vals)
        p50 = run_vals[n // 2]
        mean = sum(run_vals) / n
        run_name = folder.split("_rq2_")[1]
        print(f"  {run_name:<20} n={n:>4}  p50={p50:>6.1f}ms  mean={mean:>6.1f}ms")
