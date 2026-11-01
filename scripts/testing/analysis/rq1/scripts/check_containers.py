"""Quick check: container events and policy_state for anomaly diagnosis."""
import csv
from pathlib import Path
from collections import Counter

BASE = Path("/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics")

for label, run_name in [
    ("A1 Push OK", "20260721_051833_rq1_v7_gap_push_1"),
    ("A2 Push BAD", "20260721_061925_rq1_v7_gap_push_2"),
    ("A3 Poll BAD", "20260721_064626_rq1_v7_gap_poll30_1"),
    ("A4 Poll OK", "20260721_073750_rq1_v7_gap_poll30_2"),
]:
    ce = BASE / run_name / "container_events.csv"
    added = Counter()
    with open(ce) as f:
        for row in csv.DictReader(f):
            if row.get("event") == "added":
                added[row.get("container", "?")] += 1
    print(f"{label}: added={dict(added)}")

# Check A2 policy_state for full-run coverage
print()
ps = BASE / "20260721_061925_rq1_v7_gap_push_2" / "policy_state.csv"
phases = set()
sc_vals = set()
with open(ps) as f:
    rows = list(csv.DictReader(f))
    for r in rows:
        phases.add(r.get("phase", "?"))
        sc_vals.add(r.get("server_count", "?"))
first_ph = rows[0].get("phase", "?") if rows else "?"
last_ph = rows[-1].get("phase", "?") if rows else "?"
print(f"A2 policy_state: {len(rows)} rows, phases={sorted(phases)}, server_counts={sorted(sc_vals)}")
print(f"A2 policy_state first={first_ph}, last={last_ph}")

# Check A3 container events for LAN2 edge server
print()
ce3 = BASE / "20260721_064626_rq1_v7_gap_poll30_1" / "container_events.csv"
with open(ce3) as f:
    for row in csv.DictReader(f):
        cname = row.get("container", "")
        if "lan2" in cname.lower() or "n2" in cname.lower():
            print(f"A3 LAN2 container: {cname} event={row.get('event')} phase={row.get('phase')} state={row.get('state')}")
