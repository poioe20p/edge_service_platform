#!/usr/bin/env python3
"""rq2v4_p2_01_hash_kind_selftest.py — RQ2 v4 P2.1 hashing-kind selftest.

Exercises the hashing request kind
(``docs/operation/testing/experiment/v4/rq2/rq2v4_p2_01_hash_kind.md``):

- ``compute.hash_chain`` (edge image, ``source/docker/edge_server/source``):
  determinism (same nonce + rounds ⇒ same digest), nonce sensitivity,
  linear cost in ``rounds``, and the measured ms/round that P2.3 uses to
  seat ``HASH_WORK_FACTOR``.
- Generator wiring (``source/scripts/testing/traffic_generator.py``):
  ``pick_target("hash_challenge")`` yields a fresh 16-hex nonce;
  ``build_url`` targets ``/compute/hash?nonce=…``.
- Config knob: ``HASH_WORK_FACTOR`` env is read by the edge config via a real
  ``EdgeServerConfig.from_env()`` assertion; the default comes from the
  module constant ``HASH_WORK_FACTOR_DEFAULT`` (single source of truth).

Exit non-zero on any assertion failure. Runs with host Python (stdlib only).
Gate: local validation only.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EDGE_SRC = REPO / "source" / "docker" / "edge_server" / "source"
TESTING = REPO / "source" / "scripts" / "testing"
sys.path.insert(0, str(EDGE_SRC))
sys.path.insert(0, str(TESTING))

from compute import hash_chain  # noqa: E402
from edge_server_config import HASH_WORK_FACTOR_DEFAULT  # noqa: E402
import traffic_generator as tg  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {msg}")


def test_hash_chain() -> None:
    a1 = hash_chain("deadbeef", 1000)
    a2 = hash_chain("deadbeef", 1000)
    b = hash_chain("deadbeef", 1001)
    c = hash_chain("cafebabe", 1000)
    _assert(a1 == a2, "hash_chain is deterministic for (nonce, rounds)")
    _assert(a1 != b, "changing rounds changes the digest")
    _assert(a1 != c, "changing nonce changes the digest")
    _assert(len(a1) == 32, "digest is a full SHA-256 block (32 bytes)")


def test_cost_scaling() -> None:
    def measure(rounds: int) -> float:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            hash_chain("bench", rounds)
            best = min(best, time.perf_counter() - t0)
        return best

    t1k, t10k, t50k = measure(1000), measure(10000), measure(50000)
    _assert(t10k > t1k and t50k > t10k, "cost grows monotonically with rounds")
    ms_per_round = (t50k - t1k) / 49000 * 1000.0  # seconds delta -> ms/round
    est_default_ms = ms_per_round * HASH_WORK_FACTOR_DEFAULT
    print(f"  measured: 1k={t1k*1000:.2f}ms 10k={t10k*1000:.2f}ms 50k={t50k*1000:.2f}ms")
    print(f"  per-round: {ms_per_round:.5f} ms  ({ms_per_round*1000:.2f} us)  "
          f"default({HASH_WORK_FACTOR_DEFAULT}) ~ {est_default_ms:.1f} ms/request")
    # Unit/regression guard: per-round cost must sit in a plausible band
    # (0.1-20 us/round). This catches label/scaling bugs like the 1000x one.
    _assert(0.0001 <= ms_per_round <= 0.02,
            "per-round cost inside the plausible band (0.1-20 us)")
    # Seat sanity for P2.3: the default must plausibly dominate the observed
    # per-request non-handler overhead (2.3-2.6 ms at 0.15 core). Warn only.
    if est_default_ms < 5.0:
        print(f"  WARN: default cost {est_default_ms:.1f}ms < 5ms - P2.3 must re-seat HASH_WORK_FACTOR")


def test_generator_wiring() -> None:
    class _Phase:
        cross_region_ratio = 0.0
        hotspot_direction = ""

    class _Snap:
        content_ids_by_region = {"lan1": ["c1"], "lan2": ["c2"]}
        user_ids_by_region = {"lan1": ["u1"], "lan2": ["u2"]}

    phase, snap = _Phase(), _Snap()
    t1 = tg.pick_target("lan1", phase, snap, "hash_challenge")
    t2 = tg.pick_target("lan1", phase, snap, "hash_challenge")
    _assert(len(t1["nonce"]) == 16 and all(c in "0123456789abcdef" for c in t1["nonce"]),
            "pick_target yields a 16-hex-char nonce")
    _assert(t1["nonce"] != t2["nonce"], "nonces are fresh per request")
    url = tg.build_url("10.0.0.253:5000", "hash_challenge", t1)
    _assert(url == f"http://10.0.0.253:5000/compute/hash?nonce={t1['nonce']}",
            "build_url targets /compute/hash with the nonce")

    # Mix weights are normalised — all shipped mixes sum to 1.
    kinds = {tg.pick_request_type({"hash_challenge": 0.2, "content_lookup": 0.8})
             for _ in range(400)}
    _assert(kinds == {"hash_challenge", "content_lookup"},
            "weighted selection interleaves the new kind with an existing one")


def test_config_knob() -> None:
    from edge_server_config import EdgeServerConfig
    prior = os.environ.get("HASH_WORK_FACTOR")
    try:
        os.environ["HASH_WORK_FACTOR"] = "12345"
        cfg = EdgeServerConfig.from_env()
        _assert(cfg.hash_work_factor == 12345,
                "edge config reads HASH_WORK_FACTOR from the environment")
        os.environ.pop("HASH_WORK_FACTOR")
        cfg_default = EdgeServerConfig.from_env()
        _assert(cfg_default.hash_work_factor == HASH_WORK_FACTOR_DEFAULT,
                f"edge config defaults to HASH_WORK_FACTOR_DEFAULT "
                f"({HASH_WORK_FACTOR_DEFAULT})")
    finally:
        if prior is None:
            os.environ.pop("HASH_WORK_FACTOR", None)
        else:
            os.environ["HASH_WORK_FACTOR"] = prior


def main() -> int:
    print("rq2v4 P2.1 hashing-kind selftest")
    print("-- hash_chain correctness")
    test_hash_chain()
    print("-- cost scaling")
    test_cost_scaling()
    print("-- generator wiring")
    test_generator_wiring()
    print("-- config knob")
    test_config_knob()
    print("PASS: all rq2v4 P2.1 hashing-kind assertions hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
