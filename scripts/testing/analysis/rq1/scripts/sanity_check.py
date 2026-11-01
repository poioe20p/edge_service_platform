"""Sanity check Push rerun."""
import csv
from collections import Counter
from pathlib import Path

p = Path("/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics/20260721_114542_rq1_v7_gap_push_2/client_requests.csv")
c = Counter()
total = 0
with open(p) as f:
    for row in csv.DictReader(f):
        total += 1
        c[row.get("http_status", "?")] += 1

pct_ok = c.get("200", 0) / total * 100 if total else 0
print(f"Total: {total:,}")
print(f"Statuses: {dict(c)}")
print(f"Success rate: {pct_ok:.1f}%")

rs = Path("/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics/20260721_114542_rq1_v7_gap_push_2/resource_stats.csv")
max_sc = 0
phases = set()
with open(rs) as f:
    for row in csv.DictReader(f):
        sc = int(float(row.get("server_count", 0)))
        max_sc = max(max_sc, sc)
        phases.add(row.get("phase", "?"))
print(f"Max server_count: {max_sc}")
print(f"Phases: {sorted(phases)}")
