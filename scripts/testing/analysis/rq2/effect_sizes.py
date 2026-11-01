#!/usr/bin/env python3
"""Cohen's d effect size analysis for RQ2 metrics."""
import numpy as np

# Per-run data from verification
host_share  = [0.1214, 0.3805, 0.3868]
ss_share    = [0.5865, 0.5754, 0.4806]
tl_share    = [0.6612, 0.7274, 0.7955]

host_p50    = [282.6, 483.0, 244.8]
ss_p50      = [138.6, 135.0, 148.1]
tl_p50      = [115.2, 163.8, 154.8]

host_p95    = [3159.4, 2170.1, 2322.1]
ss_p95      = [2573.1, 2531.6, 2672.5]
tl_p95      = [2384.7, 2402.0, 3167.5]

def cohens_d(a, b):
    m1, m2 = np.mean(a), np.mean(b)
    s1, s2 = np.std(a, ddof=1), np.std(b, ddof=1)
    pooled = np.sqrt((s1**2 + s2**2) / 2)
    return (m1 - m2) / pooled if pooled > 0 else 0

def verdict(d_abs):
    if d_abs >= 1.2:
        return "LARGE  (d={:.2f}) -- 3 reps SUFFICIENT".format(d_abs)
    elif d_abs >= 0.5:
        return "MEDIUM (d={:.2f}) -- 3 reps MARGINAL".format(d_abs)
    else:
        return "SMALL  (d={:.2f}) -- 3 reps INSUFFICIENT".format(d_abs)

print("=" * 72)
print("EFFECT SIZE ANALYSIS -- Cohen's d (n=3 per group)")
print("=" * 72)

print()
print("--- Initial Load Share ---")
for label, a, b in [
    ("Host vs Slowstart       ", host_share, ss_share),
    ("Slowstart vs Lifecycle  ", ss_share, tl_share),
    ("Host vs Lifecycle       ", host_share, tl_share),
]:
    d = cohens_d(b, a)
    print("  {} {}".format(label, verdict(abs(d))))

print()
print("--- p50 Latency (lower is better) ---")
for label, a, b in [
    ("Host vs Slowstart       ", host_p50, ss_p50),
    ("Slowstart vs Lifecycle  ", ss_p50, tl_p50),
    ("Host vs Lifecycle       ", host_p50, tl_p50),
]:
    d = cohens_d(a, b)
    print("  {} {}".format(label, verdict(d)))

print()
print("--- p95 Latency (lower is better) ---")
for label, a, b in [
    ("Host vs Slowstart       ", host_p95, ss_p95),
    ("Slowstart vs Lifecycle  ", ss_p95, tl_p95),
    ("Host vs Lifecycle       ", host_p95, tl_p95),
]:
    d = cohens_d(a, b)
    print("  {} {}".format(label, verdict(abs(d))))

print()
print("=" * 72)
print("VERDICT")
print("=" * 72)
print("d >= 1.2 : large  effect -- 3 reps sufficient (power > 0.8)")
print("0.5-1.2  : medium effect -- 3 reps marginal  (power ~0.4-0.7)")
print("d < 0.5  : small  effect -- 3 reps insufficient (need >10)")
print()
print("Host within-mode variance is ALWAYS the largest -- host is less predictable.")
