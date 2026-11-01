# RQ1 Evaluation v1 — Concluding Summary

**Date**: 2026-06-25  
**Status**: ⚠️ Rerun required — Tier 1 did not fire in Push baseline

## What the experiment measured

Four runs of the 10-phase integrated workload under identical conditions except
telemetry delivery: Push (ZMQ), Poll-5s, Poll-12s, Poll-30s. All runs completed
all 10 phases without crashes.

## What was confirmed

### 1. The HTTP cache works correctly ✅

Information age (`consumed_at − window_end`) is effectively zero for all
modes — mean staleness 0.002–0.009 s per phase, worst case 0.05 s. The
aggregator's HTTP cache serves the freshest summary at every poll. Push and
poll are indistinguishable by this metric. This validates the delivery
pipeline design and confirms that *stale data* is not the mechanism that
delays the controller — *missed windows* are.

### 2. Blind spots measurably delay reaction ⚠️

The breach-detection segment of reaction latency captures the blind-spot
penalty. Poll-12s produced the worst case: 329.6 s to detect a Tier 1
breach on lan1 vs. Push's 29.9 s worst case. The mechanism is real — the
controller simply does not see windows between polls — but the relationship
is not monotonic across all four conditions. Poll-30s did not show the
expected degradation beyond Poll-12s (worst case 119.8 s vs 329.6 s). This
is likely single-run variance: with only 3–5 reaction-latency events per run,
one outlier dominates the range.

### 3. Overhead is indistinguishable ✅

Push and poll modes show comparable CPU and RAM profiles. Polling at 30 s
adds no measurable control-plane cost — each HTTP GET to the aggregator's
`/latest_summary` endpoint is sub-millisecond on the same Docker host.

### 4. Storage (Tier 2) reserve mechanism has a MAC-recycling bug — predates RQ1

**Corrected 2026-06-25.** The reserve standby nodes are prepared correctly but
activation is blocked by a MAC-recycling collision in `node_registry.py` (root
cause found in golden_config_a controller logs). When `sync()` processes a late
cleanup completion for an already-removed node that happened to share a recycled
MAC with a new reserve node, the new reserve is removed from `_active`, causing
`consume_ready_storage_reserve()` to return `None`. The bug exists since the
golden_config_stability experiment (2026-06-09).

In RQ1 v1, storage scaled 2→4 nodes. Whether these were functional (serving VIP
traffic) or idle standbys cannot be confirmed because the controller logs were
deleted in the post-run workflow. The `DataAlert` fallback path (direct spawn)
may have compensated.

**This bug must be fixed before the RQ1 rerun.** The golden config itself never
had working reserve activation — the `[reserve] activated` log was never emitted
in any experiment because activation always failed.

## What was NOT confirmed

### 5. Tier 1 (selective sync) regressed between golden_config_stability and RQ1 ❌

| Mechanism | Push | Poll-5s | Poll-12s | Poll-30s |
|-----------|------|---------|----------|----------|
| Compute (Tier 3) | ✅ 13 spawns | ✅ | ✅ 6 spawns | ✅ 7 spawns |
| Storage (Tier 2) | ✅ 2→4 nodes | ✅ | ✅ | ✅ |
| Tier 1 (selective sync) | ❌ 0 ACTIVE | ❌ 0 ACTIVE | ✅ 29 ACTIVE rows | ❌ 0 ACTIVE |
| Conntrack | ✅ | ✅ | ✅ | ✅ |

The golden_config_stability experiment (2026-06-09, same `phases.json`, same
`current_state_integrated.env`, same toggles) achieved Tier 1 activation in
both directions: 118 ACTIVE coord rows in Run A, 72 in Run B. RQ1 v1 Push
had 0 ACTIVE rows. This is a regression — Tier 1 stopped working between the
stability experiments and RQ1 despite identical configuration.

**The earlier "Push preempted Tier 1 via faster Tier 2/3 reaction" hypothesis
is withdrawn.** The golden_config_stability experiment proved Tier 1 fires in
Push mode with this exact configuration. The regression cause is unknown and
must be investigated before the rerun.

## Implications for the thesis

The v1 data supports portions of the thesis argument:
> *Missed telemetry windows — not stale data — delay the controller's
> response to overload.*

However, without Tier 1 firing in the Push baseline, the experiment cannot
provide the full multi-tier reaction-latency comparison the thesis requires.

## Rerun prerequisites

Before relaunching, two issues must be resolved:

### 1. Fix the MAC-recycling bug in `node_registry.py` ✅ DONE (2026-06-25)

This bug (found 2026-06-25 in golden_config_a controller logs) prevented reserve
activation in ALL experiments before this date. Both fixes applied and verified:

- **B1** ✅: `sync()` now checks container name before removing from `_active` — prevents stale Tier 1 cleanup completions from clobbering recycled MACs. Triggered once in fix-verification (late Tier 1 cleanup correctly skipped).
- **B2** ✅: `consume_ready_storage_reserve()` constructs `NodeInfo` from slot data when `_active` lookup returns `None`; slot cleared only on success.

**Verification**: 7 `[reserve] activated` events across the fix-verification pair
(`20260625_203912_golden_config_a` + `20260625_212249_golden_config_b`), 1 stale-removal
guard trigger, 0 "consume returned None" warnings. See
[`golden_config_stability/results.md`](../../../docs/operation/testing/experiment/stability/golden_config_stability/results.md) §6–§7.

### 2. Investigate Tier 1 regression

1. **Compare code versions**: The golden_config_stability experiment ran on
   2026-06-09. RQ1 v1 ran on 2026-06-21. Identify all code changes to
   `source/sdn_controller/` between these dates that could affect Tier 1
   (selective sync state machine, coordinator lifecycle, WAN path).

2. **Verify WAN emulation**: Check that `WAN_RTT_MS=10` is correctly applied
   in the RQ1 run environment. Compare `conntrack_entries` and WAN latency
   metrics between golden_config_stability and RQ1 resource_stats.

3. **Verify SS_ENABLED=1**: Confirm the toggle is correctly passed through
   the env override chain to both controller instances.

4. **Run a quick smoke test**: A short push run (e.g., `phases_mini.json`
   with a hotspot phase) to verify Tier 1 activates before committing to
   full 4-run replicates.

## What v1 data remains valid for the thesis

| Measurement | Usable? | Why |
|-------------|---------|-----|
| 1 — Information age | ✅ Yes | ~0 for all modes; validates HTTP cache design |
| 2 — Reaction latency | ⚠️ Partial | Blind-spot effect confirmed; quantification noisy; no Tier 1 baseline |
| 3 — Service quality | ⚠️ Partial | Cross-run comparison valid; but Tier 1 gap limits conclusions |
| 4 — Overhead | ✅ Yes | Indistinguishable between push and poll |
| 5 — Behavioral divergence | ✅ Yes | Poll-12s alone exercised Tier 1; blind spot → mechanism activation pattern |
