#!/usr/bin/env python3
"""Tier 1 WAN curve latency & reliability analysis."""
import csv, os, statistics, json

METRICS = '/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics'

# Explicit mapping: keyword → folder name (handles duplicates by picking the re-run)
_FOLDER_MAP = {
    'wan200_on':       '20260629_170032_v6_t1_wan200_on',
    'wan200_off':      '20260629_173330_v6_t1_wan200_off',
    'wan230_on':       '20260629_192337_v6_t1_wan230_on',   # re-run (first was anomalous)
    'wan230_off':      '20260629_195420_v6_t1_wan230_off',
    'wan260_on':       '20260629_202435_v6_t1_wan260_on',
    'wan260_off':      '20260629_214307_v6_t1_wan260_off',  # re-run (first was anomalous)
    'wan300_on':       '20260629_222532_v6_t1_wan300_on',
    'wan300_off':      '20260629_225558_v6_t1_wan300_off',
    'wan260_on_vip60': '20260629_235752_v6_t1_wan260_on_vip60',
    'wan260_off_vip60':'20260630_002831_v6_t1_wan260_off_vip60',
}

def find_folder(keyword):
    """Find the metrics folder using explicit mapping (handles duplicates)."""
    if keyword in _FOLDER_MAP:
        path = os.path.join(METRICS, _FOLDER_MAP[keyword])
        if os.path.isdir(path):
            return path
    # Fallback: search
    matches = [os.path.join(METRICS, d) for d in os.listdir(METRICS) if keyword in d]
    if not matches:
        return None
    if '_vip' not in keyword:
        best = [m for m in matches if '_vip' not in m]
    else:
        best = [m for m in matches if '_vip' in m]
    return best[0] if best else matches[0]


# ── Run inventory ──────────────────────────────────────────────
pairs = [
    ('200ms ON  (30s)', 'wan200_on'),
    ('200ms OFF (30s)', 'wan200_off'),
    ('230ms ON  (30s)', 'wan230_on'),
    ('230ms OFF (30s)', 'wan230_off'),
    ('260ms ON  (30s)', 'wan260_on'),
    ('260ms OFF (30s)', 'wan260_off'),
    ('300ms ON  (30s)', 'wan300_on'),
    ('300ms OFF (30s)', 'wan300_off'),
    ('260ms ON  (60s)', 'wan260_on_vip60'),
    ('260ms OFF (60s)', 'wan260_off_vip60'),
]

# ── Overall latency & reliability ──────────────────────────────
print('=== Tier 1 WAN Curve — Latency & Reliability ===')
print()
header = '%-22s  %7s  %7s  %7s  %7s  %7s  %6s  %7s'
print(header % ('Run', 'Median', 'Mean', 'p75', 'p90', 'p95', 'Fail%', 'Count'))
print('-' * 85)

for label, kw in pairs:
    folder = find_folder(kw)
    if not folder:
        print('%-22s  NOT FOUND' % label)
        continue
    cr = os.path.join(folder, 'client_requests.csv')
    if not os.path.exists(cr):
        continue
    rows = list(csv.DictReader(open(cr)))
    lats = sorted([float(r['latency_s']) * 1000 for r in rows if r.get('latency_s')])
    n = len(lats)
    fails = sum(1 for r in rows if r.get('http_status', '200') not in ('200', '201'))
    if n == 0:
        continue
    print('%-22s  %6.0fms  %6.0fms  %6.0fms  %6.0fms  %6.0fms  %5.1f%%  %6d' % (
        label,
        lats[n // 2],
        sum(lats) / n,
        lats[int(n * 0.75)],
        lats[int(n * 0.90)],
        lats[int(n * 0.95)],
        100.0 * fails / len(rows),
        n,
    ))

# ── Per-phase for key phases ───────────────────────────────────
print()
print('=== Per-Phase Comparison ===')
print()

for phase_name in ['storage_storm', 'tier1_hotspot', 'compute_spike']:
    print('--- %s ---' % phase_name)
    print('%-22s  %7s  %7s  %7s  %6s' % ('Run', 'Median', 'Mean', 'p95', 'Fail%'))
    print('-' * 55)
    for label, kw in pairs:
        folder = find_folder(kw)
        if not folder:
            continue
        cr = os.path.join(folder, 'client_requests.csv')
        rows = list(csv.DictReader(open(cr)))
        ph_rows = [r for r in rows if r.get('phase') == phase_name]
        lats = sorted([float(r['latency_s']) * 1000 for r in ph_rows if r.get('latency_s')])
        n = len(lats)
        fails = sum(1 for r in ph_rows if r.get('http_status', '200') not in ('200', '201'))
        if n == 0:
            continue
        print('%-22s  %6.0fms  %6.0fms  %6.0fms  %5.1f%%' % (
            label,
            lats[n // 2],
            sum(lats) / n,
            lats[int(n * 0.95)] if n > 1 else 0,
            100.0 * fails / len(ph_rows),
        ))
    print()


# ── ON/OFF delta table ─────────────────────────────────────────
print()
print('=== Tier 1 Benefit: ON vs OFF Delta ===')
print()
print('%-20s  %8s  %8s  %8s' % ('WAN Level', 'Med-ON', 'Med-OFF', 'Reduction'))
print('-' * 50)

for wan_label, kw_on, kw_off in [
    ('200ms (30s)', 'wan200_on', 'wan200_off'),
    ('260ms (30s)', 'wan260_on', 'wan260_off'),
    ('300ms (30s)', 'wan300_on', 'wan300_off'),
    ('260ms (60s)', 'wan260_on_vip60', 'wan260_off_vip60'),
]:
    f_on = find_folder(kw_on)
    f_off = find_folder(kw_off)
    if not f_on or not f_off:
        continue
    med_on = med_off = 0
    for label, f in [('ON', f_on), ('OFF', f_off)]:
        cr = os.path.join(f, 'client_requests.csv')
        rows = list(csv.DictReader(open(cr)))
        lats = sorted([float(r['latency_s']) * 1000 for r in rows if r.get('latency_s')])
        if lats:
            if label == 'ON':
                med_on = lats[len(lats) // 2]
            else:
                med_off = lats[len(lats) // 2]
    if med_off > 0:
        reduction = (1 - med_on / med_off) * 100
        print('%-20s  %7.0fms  %7.0fms  %6.1f%%' % (wan_label, med_on, med_off, reduction))


# ── Resource stats: Tier 1 lifecycle & storage count ───────────
print()
print('=== Resource Stats: Mechanism Exercise ===')
print()
print('%-22s  %12s  %10s  %10s' % ('Run', 'Stor(Range)', 'T1-Active', 'Server(Range)'))
print('-' * 60)

for label, kw in pairs:
    folder = find_folder(kw)
    if not folder:
        continue
    rs = os.path.join(folder, 'resource_stats.csv')
    if not os.path.exists(rs):
        continue
    rows = list(csv.DictReader(open(rs)))
    sc_vals = [int(r['storage_count']) for r in rows if r.get('storage_count')]
    t1_vals = [int(r.get('tier1_lifecycle_active_count', 0) or 0) for r in rows if r.get('tier1_lifecycle_active_count') is not None]
    sv_vals = [int(r['server_count']) for r in rows if r.get('server_count')]
    print('%-22s  %2d..%2d stor  %4d..%d act  %2d..%2d svr' % (
        label,
        min(sc_vals) if sc_vals else 0,
        max(sc_vals) if sc_vals else 0,
        max(t1_vals) if t1_vals else 0,
        max(t1_vals) if t1_vals else 0,
        min(sv_vals) if sv_vals else 0,
        max(sv_vals) if sv_vals else 0,
    ))
