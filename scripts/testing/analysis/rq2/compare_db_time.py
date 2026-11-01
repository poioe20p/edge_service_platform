"""Breakdown: processing vs DB time across RQ2 modes."""
import csv, os
from collections import defaultdict

base = r"c:\Users\themo\Documents\Trabalhos Academicos\Mestrado - Tese\efficient-storage-in-edge-scenarios\source\scripts\testing\metrics"
modes = {"th": "host", "ss": "slowstart", "tl": "lifecycle"}
results = defaultdict(lambda: {"proc": [], "db": [], "db_read": [], "db_write": [], "db_cmds": []})

for folder in sorted(os.listdir(base)):
    if "_rq2_" not in folder:
        continue
    mode_key = folder.split("_rq2_")[1].split("_")[0]
    mode = modes.get(mode_key, mode_key)
    fpath = os.path.join(base, folder, "per_node_stats.csv")
    if not os.path.exists(fpath):
        continue
    with open(fpath) as f:
        for row in csv.DictReader(f):
            if row["role"] != "compute":
                continue
            for col, short in [("avg_time_proc_ms", "proc"), ("avg_time_db_ms", "db"), ("avg_time_db_read_ms", "db_read"), ("avg_time_db_write_ms", "db_write"), ("avg_time_db_cmd_count", "db_cmds")]:
                val = row.get(col, "")
                if val and val.strip():
                    results[mode][short].append(float(val))

print("=" * 80)
print(f"{'Metric':<20} {'host':>12} {'slowstart':>12} {'lifecycle':>12}")
print("-" * 80)
for label, key in [("proc (ms)", "proc"), ("db total (ms)", "db"), ("db read (ms)", "db_read"), ("db write (ms)", "db_write"), ("db cmd count", "db_cmds")]:
    vals = []
    for mode in ["host", "slowstart", "lifecycle"]:
        v = results[mode][key]
        vals.append(f"{sum(v)/len(v):.1f}" if v else "--")
    print(f"{label:<20} {vals[0]:>12} {vals[1]:>12} {vals[2]:>12}")

# DB time as % of total
print("\n--- DB time as % of total (proc+db) ---")
for mode in ["host", "slowstart", "lifecycle"]:
    proc_vals = results[mode]["proc"]
    db_vals = results[mode]["db"]
    if proc_vals and db_vals and len(proc_vals) == len(db_vals):
        ratios = [db/(p+db)*100 if (p+db) > 0 else 0 for p, db in zip(proc_vals, db_vals)]
        ratios.sort()
        n = len(ratios)
        print(f"  {mode:<12} median DB share: {ratios[n//2]:.1f}%  (mean: {sum(ratios)/n:.1f}%)")

# Per-run DB share
print("\n--- Per-run DB share ---")
for folder in sorted(os.listdir(base)):
    if "_rq2_" not in folder:
        continue
    mode_key = folder.split("_rq2_")[1].split("_")[0]
    mode = modes.get(mode_key, mode_key)
    fpath = os.path.join(base, folder, "per_node_stats.csv")
    if not os.path.exists(fpath):
        continue
    proc_vals, db_vals = [], []
    with open(fpath) as f:
        for row in csv.DictReader(f):
            if row["role"] != "compute":
                continue
            p = row.get("avg_time_proc_ms", "")
            d = row.get("avg_time_db_ms", "")
            if p and p.strip() and d and d.strip():
                proc_vals.append(float(p))
                db_vals.append(float(d))
    if proc_vals:
        ratios = [db/(p+db)*100 if (p+db) > 0 else 0 for p, db in zip(proc_vals, db_vals)]
        ratios.sort()
        n = len(ratios)
        run = folder.split("_rq2_")[1]
        print(f"  {run:<25} n={n:>4}  median DB% = {ratios[n//2]:.1f}%  mean = {sum(ratios)/n:.1f}%")
