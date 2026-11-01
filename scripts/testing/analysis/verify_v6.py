#!/usr/bin/env python3
"""Verify results_v6.md numbers against actual run artifacts."""
import csv, os, json

M = '/home/testop/efficient-storage-in-edge-scenarios/source/scripts/testing/metrics'

# ── Exact folder mapping ───────────────────────────────────────
TIER1 = {
    'T1': '20260629_170032_v6_t1_wan200_on',
    'T2': '20260629_173330_v6_t1_wan200_off',
    'T3': '20260629_192337_v6_t1_wan230_on',
    'T4': '20260629_195420_v6_t1_wan230_off',
    'T5': '20260629_202435_v6_t1_wan260_on',
    'T6': '20260629_214307_v6_t1_wan260_off',
    'T7': '20260629_222532_v6_t1_wan300_on',
    'T8': '20260629_225558_v6_t1_wan300_off',
    'T9': '20260629_235752_v6_t1_wan260_on_vip60',
    'T10':'20260630_002831_v6_t1_wan260_off_vip60',
}

STORAGE = {
    'S1-ON':  '20260630_010421_v6_st_cal_s1_cpu012',
    'S1-OFF': '20260630_075327_v6_st_cal_s1_off',
    'S2-ON':  '20260630_013709_v6_st_cal_s2_cpu010',
    'S2-OFF': '20260630_082421_v6_st_cal_s2_off',
    'S3-ON':  '20260630_072202_v6_st_cal_s3_cpu012',
    'S3-OFF': '20260630_085437_v6_st_cal_s3_off',
}

def latency_stats(rows, phase_filter=None):
    """Return (count, fail_pct, median_ms, p95_ms) from client_requests rows."""
    if phase_filter:
        rows = [r for r in rows if r.get('phase') == phase_filter]
    total = len(rows)
    lats = sorted([float(r['latency_s'])*1000 for r in rows if r.get('latency_s')])
    fails = sum(1 for r in rows if r.get('http_status','200') not in ('200','201'))
    n = len(lats)
    return (
        total,
        round(100.0*fails/total, 1) if total else 0,
        round(lats[n//2]) if n else 0,
        round(lats[int(n*0.95)]) if n > 1 else 0,
    )

print('='*80)
print('VERIFICATION: Tier 1 Runs')
print('='*80)

# First, check phase names
print('\n>>> Phase names (from T1 phases_snapshot.json) <<<')
ps_path = os.path.join(M, TIER1['T1'], 'phases_snapshot.json')
if os.path.exists(ps_path):
    ps = json.load(open(ps_path))
    phases_in_config = [p['name'] for p in ps.get('phases', [])]
    print('Config phases:', phases_in_config)

# Check actual phases in CSV
cr_path = os.path.join(M, TIER1['T1'], 'client_requests.csv')
rows = list(csv.DictReader(open(cr_path)))
actual_phases = sorted(set(r['phase'] for r in rows if r.get('phase')))
print('CSV phases:  ', actual_phases)
print()

print('>>> Tier 1 Run Timeline Verification <<<')
print(f'{"Run":5s} {"Req":>7s} {"Fail%":>7s} {"OverallMed":>10s} {"TH_Med":>9s} {"TH_Fail%":>9s}')
print('-' * 60)
for label, folder in TIER1.items():
    cr = os.path.join(M, folder, 'client_requests.csv')
    if not os.path.exists(cr):
        print(f'{label:5s} MISSING')
        continue
    rows = list(csv.DictReader(open(cr)))
    total, fp, med, p95 = latency_stats(rows)
    # tier1_hotspot phase
    _, th_fp, th_med, th_p95 = latency_stats(rows, 'tier1_hotspot')
    print(f'{label:5s} {total:>7d} {fp:>6.1f}% {med:>9d}ms {th_med:>8d}ms {th_fp:>8.1f}%')

print()
print('>>> T9 vs T10 Per-Phase Breakdown <<<')
for label in ['T9', 'T10']:
    cr = os.path.join(M, TIER1[label], 'client_requests.csv')
    rows = list(csv.DictReader(open(cr)))
    phases = sorted(set(r['phase'] for r in rows if r.get('phase')))
    print(f'\n{label}:')
    print(f'  {"Phase":25s} {"Req":>7s} {"Fail%":>7s} {"Med":>8s} {"p95":>8s}')
    for ph in phases:
        total, fp, med, p95 = latency_stats(rows, ph)
        print(f'  {ph:25s} {total:>7d} {fp:>6.1f}% {med:>7d}ms {p95:>7d}ms')

print()
print('>>> Resource Stats: Mechanism Exercise <<<')
print(f'{"Run":5s} {"StorRange":>12s} {"T1-Active":>12s} {"ServerRange":>12s}')
print('-' * 50)
for label, folder in {**TIER1, **STORAGE}.items():
    rs = os.path.join(M, folder, 'resource_stats.csv')
    if not os.path.exists(rs):
        print(f'{label:5s} NO resource_stats.csv')
        continue
    rows = list(csv.DictReader(open(rs)))
    sc = [int(r['storage_count']) for r in rows if r.get('storage_count')]
    t1 = [int(r.get('tier1_lifecycle_active_count', 0) or 0) for r in rows]
    sv = [int(r['server_count']) for r in rows if r.get('server_count')]
    print(f'{label:5s} {min(sc) if sc else 0:>3d}-{max(sc) if sc else 0:<3d} stor  '
          f'{max(t1) if t1 else 0:>3d} t1-act  '
          f'{min(sv) if sv else 0:>3d}-{max(sv) if sv else 0:<3d} svr')

print()
print('='*80)
print('VERIFICATION: Storage Runs — Per-Phase CPU')
print('='*80)

print(f'\n{"Run":7s} {"baseline":>8s} {"storage_storm":>14s} {"tier1_hotspot":>14s} {"compute_spike":>14s} {"RUN_AVG":>8s} {"Nodes":>8s}')
print('-' * 90)
for label, folder in STORAGE.items():
    rs = os.path.join(M, folder, 'resource_stats.csv')
    if not os.path.exists(rs):
        continue
    rows = list(csv.DictReader(open(rs)))
    sc = [int(r['storage_count']) for r in rows if r.get('storage_count')]
    
    parts = []
    all_cpu = []
    for ph in ['baseline', 'storage_storm', 'tier1_hotspot', 'inter_hotspot_cooldown', 'compute_spike', 'cooldown']:
        ph_rows = [r for r in rows if r.get('phase') == ph]
        cpu_vals = [float(r['avg_storage_cpu_percent']) for r in ph_rows if r.get('avg_storage_cpu_percent')]
        if cpu_vals:
            avg = sum(cpu_vals)/len(cpu_vals)
            parts.append(f'{avg:>5.1f}%')
            all_cpu.extend(cpu_vals)
        else:
            parts.append('    N/A')
    
    run_avg = sum(all_cpu)/len(all_cpu) if all_cpu else 0
    parts.append(f'{run_avg:>5.1f}%')
    parts.append(f'{min(sc) if sc else 0}-{max(sc) if sc else 0}')
    
    print(f'{label:7s} ' + '  '.join(f'{p:>12s}' if i < 4 else f'{p:>8s}' if i < 5 else f'{p:>8s}' for i, p in enumerate(parts)))

print()
print('>>> Storage Verification Summary <<<')
print(f'{"Config":20s} {"ON_CPU":>8s} {"OFF_CPU":>8s} {"Penalty":>8s}')
pairs = [('0.12/6K', 'S1-ON', 'S1-OFF'), ('0.10/6K', 'S2-ON', 'S2-OFF'), ('0.12/12K', 'S3-ON', 'S3-OFF')]
for cfg, on, off in pairs:
    on_cpu = off_cpu = 0
    for lbl, folder in [(on, STORAGE[on]), (off, STORAGE[off])]:
        rs = os.path.join(M, folder, 'resource_stats.csv')
        rows = list(csv.DictReader(open(rs)))
        cpu = [float(r['avg_storage_cpu_percent']) for r in rows if r.get('avg_storage_cpu_percent')]
        avg = sum(cpu)/len(cpu) if cpu else 0
        if lbl == on:
            on_cpu = avg
        else:
            off_cpu = avg
    penalty = f'+{(off_cpu/on_cpu - 1)*100:.0f}%' if on_cpu else 'N/A'
    print(f'{cfg:20s} {on_cpu:>7.1f}% {off_cpu:>7.1f}% {penalty:>8s}')
