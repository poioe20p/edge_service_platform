"""Check LAN distribution asymmetry across RQ2 runs."""
import csv, os
from collections import Counter

base = r"c:\Users\themo\Documents\Trabalhos Academicos\Mestrado - Tese\efficient-storage-in-edge-scenarios\source\scripts\testing\metrics"
modes_map = {"th": "host", "ss": "slowstart", "tl": "lifecycle"}

print(f"{'run':<25} {'mode':<10} {'lan1_req':>8} {'lan1%':>6} {'lan2_req':>8} {'total':>8}")
print("-" * 75)

for folder in sorted(os.listdir(base)):
    if "_rq2_" not in folder:
        continue
    mk = folder.split("_rq2_")[1].split("_")[0]
    mode = modes_map.get(mk, mk)
    run = folder.split("_rq2_")[1]

    lan_counts = Counter()
    fpath = os.path.join(base, folder, "client_requests.csv")
    if not os.path.exists(fpath):
        continue
    with open(fpath) as f:
        for row in csv.DictReader(f):
            lan = row.get("target_region", row.get("client_lan", ""))
            lan_counts[lan] += 1

    total = sum(lan_counts.values())
    if total == 0:
        continue
    lan1 = lan_counts.get("lan1", 0)
    lan2 = lan_counts.get("lan2", 0)
    print(f"{run:<25} {mode:<10} {lan1:>8} {lan1/total*100:>5.0f}% {lan2:>8} {total:>8}")
