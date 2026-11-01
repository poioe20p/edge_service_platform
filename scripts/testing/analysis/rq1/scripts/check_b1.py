import csv
from collections import Counter
p = "/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics/20260721_144147_rq1_v7_slot_push_1/client_requests.csv"
c = Counter()
t = 0
with open(p) as f:
    for r in csv.DictReader(f):
        t += 1
        c[r.get("http_status", "?")] += 1
print(f"B1: {t:,} reqs, statuses={dict(c)}, success={c.get('200',0)/t*100:.1f}%")
