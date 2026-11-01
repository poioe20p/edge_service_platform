"""Investigate A2 and A3 anomalies."""
import csv
from pathlib import Path

BASE = Path("/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics")

# Check container_events format
print("=== container_events.csv structure (A1) ===")
ce = BASE / "20260721_051833_rq1_v7_gap_push_1" / "container_events.csv"
with open(ce) as f:
    reader = csv.DictReader(f)
    print(f"  Columns: {reader.fieldnames}")
    rows = list(reader)
    print(f"  Total rows: {len(rows)}")
    if rows:
        print(f"  Sample: {dict(rows[0])}")
    events = {}
    roles = {}
    for r in rows:
        ev = r.get("event_type", r.get("event", "?"))
        events[ev] = events.get(ev, 0) + 1
        ro = r.get("role", "?")
        roles[ro] = roles.get(ro, 0) + 1
    print(f"  Event types: {events}")
    print(f"  Roles: {roles}")

# Check A2 resource_stats timeline
print()
print("=== A2 resource_stats.csv timeline ===")
rs = BASE / "20260721_061925_rq1_v7_gap_push_2" / "resource_stats.csv"
with open(rs) as f:
    rows = list(csv.DictReader(f))
    print(f"  Total rows: {len(rows)}")
    if rows:
        first_ts = rows[0].get("timestamp", "?")
        first_ph = rows[0].get("phase", "?")
        first_sc = rows[0].get("server_count", "?")
        last_ts = rows[-1].get("timestamp", "?")
        last_ph = rows[-1].get("phase", "?")
        last_sc = rows[-1].get("server_count", "?")
        print(f"  First: ts={first_ts}, phase={first_ph}, servers={first_sc}")
        print(f"  Last:  ts={last_ts}, phase={last_ph}, servers={last_sc}")
    phases_seen = set(r.get("phase", "?") for r in rows)
    print(f"  Phases: {sorted(phases_seen)}")

# Check A3 per-LAN latency
print()
print("=== A3 LAN latency comparison ===")
cr = BASE / "20260721_064626_rq1_v7_gap_poll30_1" / "client_requests.csv"
lan_lat = {"lan1": [], "lan2": []}
lan_status = {"lan1": {}, "lan2": {}}
with open(cr) as f:
    for row in csv.DictReader(f):
        lan = row.get("client_lan", "?")
        if lan in lan_lat:
            lat = float(row.get("latency_s", 0))
            lan_lat[lan].append(lat)
            s = row.get("http_status", "?")
            lan_status[lan][s] = lan_status[lan].get(s, 0) + 1

for lan in ["lan1", "lan2"]:
    lats = lan_lat[lan]
    if lats:
        lats.sort()
        n = len(lats)
        print(f"  {lan}: n={n}, p50={lats[n//2]:.3f}s, p95={lats[int(n*0.95)]:.3f}s, p99={lats[int(n*0.99)]:.3f}s")
        print(f"    http_status: {lan_status[lan]}")

# Check A3 LAN2 success rate
print()
print("=== A3 LAN2 successful requests ===")
lan2_ok = 0
lan2_total = 0
with open(cr) as f:
    for row in csv.DictReader(f):
        if row.get("client_lan") == "lan2":
            lan2_total += 1
            if row.get("http_status") == "200":
                lan2_ok += 1
pct = (lan2_ok / lan2_total * 100) if lan2_total else 0
print(f"  LAN2: {lan2_ok}/{lan2_total} successful ({pct:.1f}%)")

# Check A2 per-LAN breakdown
print()
print("=== A2 per-LAN breakdown ===")
cr2 = BASE / "20260721_061925_rq1_v7_gap_push_2" / "client_requests.csv"
lan_data = {}
with open(cr2) as f:
    for row in csv.DictReader(f):
        lan = row.get("client_lan", "?")
        if lan not in lan_data:
            lan_data[lan] = {"total": 0, "status": {}}
        lan_data[lan]["total"] += 1
        s = row.get("http_status", "?")
        lan_data[lan]["status"][s] = lan_data[lan]["status"].get(s, 0) + 1

for lan in sorted(lan_data.keys()):
    d = lan_data[lan]
    print(f"  {lan}: {d['total']:,} reqs, statuses={d['status']}")

# Check A2: did it have cross-region traffic?
print()
print("=== A2 target_region analysis ===")
cross = 0
local = 0
with open(cr2) as f:
    for row in csv.DictReader(f):
        cl = row.get("client_lan", "")
        tr = row.get("target_region", "")
        if cl and tr:
            if cl == tr:
                local += 1
            else:
                cross += 1
total_reg = cross + local
pct_cross = (cross / total_reg * 100) if total_reg else 0
print(f"  Cross-region: {cross}/{total_reg} ({pct_cross:.1f}%)")
print(f"  Local: {local}/{total_reg} ({100-pct_cross:.1f}%)")

print()
print("DONE")
