#!/usr/bin/env python3
"""
rq2v2_p3_01_policy_gate_strict_test.py — Phase-3 gate for the ba-strict
(sticky-commitment) PolicyGate mode.

Exercises the state machine with fabricated ScaleUpVerdicts across simulated
windows. Exit 0 = all checks pass; non-zero = gate failed.

Checks (from rq2_v2_rework_plan.md Phase 3):
  1. both tiers fire   -> exactly the classified tier selected (commit).
  2. after commit, the non-committed tier fires alone -> SUPPRESSED
     (select() returns (), strict_committed() still the committed tier).
  3. a single spurious fire does NOT commit (needs STRICT_COMMIT_N consecutive
     same-tier fires, or a both-fired classification).
  4. N consecutive same-tier fires -> commit to that tier.
  5. release on relief (score_norm < threshold for STRICT_RELEASE_N windows);
     after release, no stale commitment remains.
  6. no opposite-tier re-commit before release.
  7. regression: with BOTTLENECK_STRICT_SINGLE=0 the gate behaves exactly as
     before (per-window selection).

Usage:
    python3 rq2v2_p3_01_policy_gate_strict_test.py
"""
import os
import sys

# Enable strict mode BEFORE importing the gate (module reads env at import).
os.environ["SCALEUP_POLICY"] = "bottleneck_aware"
os.environ["BOTTLENECK_STRICT_SINGLE"] = "1"
os.environ["STRICT_COMMIT_N"] = "2"
os.environ["STRICT_RELEASE_N"] = "3"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sdn_controller.policy_gate import PolicyGate  # noqa: E402
from sdn_controller.scaling_policy import ScaleUpVerdict  # noqa: E402


def V(tier, fired, score_norm, threshold=0.5, eligible=True):
    return ScaleUpVerdict(
        tier=tier, eligible=eligible, fired=fired,
        score_norm=score_norm, score=score_norm, threshold=threshold)


def main() -> int:
    failures = []

    # -- 1. both fire -> classified tier (compute score_norm higher) --------
    g = PolicyGate()
    sel = g.select(V("compute", True, 0.80), V("storage", True, 0.30))
    assert g.strict_enabled()
    assert sel == ("compute",), f"1: expected (compute,), got {sel}"
    assert g.strict_committed() == "compute", "1: should commit to compute"

    # -- 2. committed: storage fires alone -> suppressed ---------------------
    sel = g.select(V("compute", False, 0.40), V("storage", True, 0.60))
    assert sel == (), f"2: expected suppression (), got {sel}"
    assert g.strict_committed() == "compute", "2: commitment must persist"

    # -- 3. single spurious fire does NOT commit -----------------------------
    g = PolicyGate()
    # First: storage fires alone once (spurious). Not enough to commit (N=2).
    sel = g.select(V("compute", False, 0.30), V("storage", True, 0.60))
    assert sel == ("storage",), f"3a: expected current behavior (storage), got {sel}"
    assert g.strict_committed() is None, "3a: single fire must not commit"
    # Second consecutive storage fire -> commits to storage.
    sel = g.select(V("compute", False, 0.30), V("storage", True, 0.60))
    assert g.strict_committed() == "storage", "3b: N consecutive fires must commit"
    # Now compute fires alone -> suppressed.
    sel = g.select(V("compute", True, 0.70), V("storage", False, 0.20))
    assert sel == (), f"3c: expected suppression (), got {sel}"

    # -- 4. wrong-first-commit scenario: spurious early storage fire in a
    #      compute-bound episode must NOT lock in storage --------------------
    g = PolicyGate()
    # One storage fire, then one compute-only fire (not consecutive for either
    # tier, so no commit), then the compute episode persists:
    g.select(V("compute", False, 0.30), V("storage", True, 0.60))   # storage streak 1
    g.select(V("compute", True, 0.80), V("storage", False, 0.20))   # streaks reset
    assert g.strict_committed() is None, "4: spurious storage fire must not lock"
    sel = g.select(V("compute", True, 0.80), V("storage", False, 0.20))
    assert sel == ("compute",), f"4: compute episode should act on compute, got {sel}"

    # -- 5. release on relief ------------------------------------------------
    g = PolicyGate()
    g.select(V("compute", True, 0.80), V("storage", True, 0.30))   # commit compute
    assert g.strict_committed() == "compute"
    # Committed tier under threshold for STRICT_RELEASE_N=3 windows.
    for _ in range(3):
        g.select(V("compute", False, 0.30), V("storage", False, 0.20))
    assert g.strict_committed() is None, "5: should release after relief"

    # -- 6. no opposite-tier re-commit before release ------------------------
    g = PolicyGate()
    g.select(V("compute", True, 0.80), V("storage", True, 0.30))   # commit compute
    for _ in range(2):  # storage keeps firing while committed, no relief yet
        g.select(V("compute", False, 0.55), V("storage", True, 0.60))
    assert g.strict_committed() == "compute", \
        "6: must NOT re-commit to storage before release"

    # -- 7. regression: strict off -> per-window behavior (subprocess, because
    #      the module reads BOTTLENECK_STRICT_SINGLE at import time) ---------
    import subprocess
    import textwrap
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = textwrap.dedent(f"""
        import os, sys
        os.environ['SCALEUP_POLICY'] = 'bottleneck_aware'
        os.environ['BOTTLENECK_STRICT_SINGLE'] = '0'
        sys.path.insert(0, {root!r})
        from sdn_controller.policy_gate import PolicyGate
        from sdn_controller.scaling_policy import ScaleUpVerdict
        def V(tier, fired, score_norm, threshold=0.5, eligible=True):
            return ScaleUpVerdict(tier=tier, eligible=eligible, fired=fired,
                                  score_norm=score_norm, score=score_norm,
                                  threshold=threshold)
        g = PolicyGate()
        assert not g.strict_enabled(), 'strict must be OFF'
        assert g.select(V('compute', True, 0.80), V('storage', True, 0.30)) == ('compute',)
        assert g.select(V('compute', False, 0.30), V('storage', True, 0.60)) == ('storage',)
        assert g.select(V('compute', False, 0.30), V('storage', False, 0.20)) == ()
        # No commitment state must exist when strict is off.
        assert g.strict_committed() is None
        print('  [PASS] 7: regression (strict off) byte-identical per-window behavior')
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        failures.append(f"7: regression subprocess failed: {r.stderr.strip()}")

    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print("POLICY-GATE STRICT TEST FAILED")
        return 1
    print("POLICY-GATE STRICT TEST PASSED "
          "(commit, suppression, spurious-safe, release, no-oscillation, regression)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
