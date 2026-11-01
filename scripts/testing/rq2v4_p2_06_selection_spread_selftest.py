#!/usr/bin/env python3
"""rq2v4_p2_06_selection_spread_selftest.py — P2.6 spread + P2.8 band floor.

Exercises the spread in ``sdn_controller/_vip_routing/selection.py``
(``select_server``):

- Band membership: candidates within ``_BACKEND_SELECTION_SPREAD_EPS`` of the
  pool minimum are spread across clients; candidates beyond eps are excluded.
- Client spread: a synchronized burst (many distinct client MACs, identical
  pool state) fans out over the band instead of collapsing to one winner.
- Determinism: the same client gets the same band member for a stable band.
- Legacy: with eps = 0 the exact pre-P2.6 behaviour holds (unique argmin
  wins for every client; a warm claim owns the selection exclusively).
- Warm inclusion: with eps > 0 a live warm claim JOINS the band (gets a
  share) instead of monopolising it.
- Peer guard: a cross-LAN unit beyond eps never enters a local band.
- Band floor (P2.8): with ``_BACKEND_SELECTION_SPREAD_MIN_BAND`` = k > 1 a
  band smaller than k is topped up to k members with the cheapest eligible
  LOCAL units (finite cost, known IP, not peer, slow-start penalty fully
  decayed; MAC tie-break), so a strict single winner cannot take a whole
  burst; the floor never adds peers (even when a peer is cheaper than the
  next local); k clamps to the number of eligible locals; k <= 1 restores
  P2.6 eps-only semantics. The selection log format (`spread band=` ...
  `floor=` ... `warm=`) is pinned as an assertion — it is the evidence
  instrument for the verification gate.

Exit non-zero on any assertion failure. Host Python, stdlib only.
See docs/operation/testing/experiment/v4/rq2/rq2v4_p2_03_relief_diagnosis.md
and docs/operation/testing/experiment/v4/rq2/rq2v4_p2_08_band_floor.md.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "source"
sys.path.insert(0, str(SOURCE))

for _var in ("BACKEND_SELECTION_SPREAD_EPS", "BACKEND_SELECTION_SPREAD_MIN_BAND",
             "BACKEND_SELECTION_POLICY",
             "VIP_SERVER_PER_CONNECTION_FLOWS",
             "W_CPU", "W_RAM", "W_REQUESTS", "W_HOPS"):
    os.environ.pop(_var, None)

from sdn_controller._vip_routing import selection  # noqa: E402
from sdn_controller._vip_routing.config import WarmLease  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {msg}")


def _stats(cpu: float) -> SimpleNamespace:
    return SimpleNamespace(avg_cpu_percent=cpu, avg_ram_used_mb=0.0,
                           request_count=0)


def _controller(units: dict, hop_max: float = 2.0,
                peers: set | None = None) -> SimpleNamespace:
    """units: {mac: stats_or_None}. All units are local unless in peers."""
    peers = peers or set()
    pool = {mac: {"mac": mac, "ip": f"10.0.0.{i + 10}"} for i, mac in enumerate(units)}
    return SimpleNamespace(
        vip_server_pool=pool,
        _server_stats={mac: st for mac, st in units.items() if st is not None},
        peer_hosts=peers,
        hop_cache={},                 # filled per test for the client
        _hop_cache_max=hop_max,
        host_attachment=set(),
        _avg_hop_count=2.0,
        _peer_avg_hop_count=2.0,
        _warm_server_leases={},
        _warm_lock=threading.Lock(),
        _warm_rr_idx=0,
        _rr_server_idx=0,
        _backend_discovery_ts={},
        _mac_to_ip={mac: entry["ip"] for mac, entry in pool.items()},
    )


CLIENTS = [f"aa:bb:cc:00:00:{i:02x}" for i in range(60)]


def _seed_hops(ctrl, macs) -> None:
    if isinstance(macs, dict):
        for client in CLIENTS:
            ctrl.hop_cache[client] = dict(macs)
    else:
        for client in CLIENTS:
            ctrl.hop_cache[client] = {mac: 1.0 for mac in macs}


def test_band_membership_and_spread() -> None:
    # cpu_max = 100 -> A (0) and B (2) are 0.2 * 0.02 = 0.004 apart -> band.
    # C (100) is 0.20 away -> excluded. Floor pinned to 1 here so this test
    # isolates the eps-band semantics (the floor is covered separately).
    a, b, c = "00:00:00:00:00:01", "00:00:00:00:00:02", "00:00:00:00:00:03"
    ctrl = _controller({a: _stats(0.0), b: _stats(2.0), c: _stats(100.0)})
    _seed_hops(ctrl, (a, b, c))

    original_floor = selection._BACKEND_SELECTION_SPREAD_MIN_BAND
    try:
        selection._BACKEND_SELECTION_SPREAD_MIN_BAND = 1
        picks = [selection.select_server(ctrl, client)["mac"] for client in CLIENTS]
        distinct = set(picks)
        _assert(c not in distinct, "beyond-eps candidate never enters the band")
        _assert(distinct == {a, b}, "within-eps candidates both receive clients")
        _assert(picks.count(a) > 10 and picks.count(b) > 10,
                f"spread is roughly balanced (a={picks.count(a)} b={picks.count(b)})")

        first = selection.select_server(ctrl, CLIENTS[0])["mac"]
        again = selection.select_server(ctrl, CLIENTS[0])["mac"]
        _assert(first == again, "same client is deterministic for a stable band")
    finally:
        selection._BACKEND_SELECTION_SPREAD_MIN_BAND = original_floor


def test_legacy_epsilon_zero() -> None:
    a, b, c = "00:00:00:00:00:01", "00:00:00:00:00:02", "00:00:00:00:00:03"
    ctrl = _controller({a: _stats(0.0), b: _stats(10.0), c: _stats(100.0)})
    _seed_hops(ctrl, (a, b, c))

    original = selection._BACKEND_SELECTION_SPREAD_EPS
    try:
        selection._BACKEND_SELECTION_SPREAD_EPS = 0.0
        picks = {selection.select_server(ctrl, client)["mac"] for client in CLIENTS}
        _assert(picks == {a}, "eps=0: unique argmin wins for every client (legacy funnel)")
    finally:
        selection._BACKEND_SELECTION_SPREAD_EPS = original


def test_warm_inclusion() -> None:
    # a and b: identical stats, near hops -> band {a,b}. e: same stats but 2x
    # hops (delta 0.14 > eps) -> excluded. W: no stats -> joins via the claim.
    # Floor pinned to 1 so this test isolates the warm-inclusion semantics.
    a, b, e, w = ("00:00:00:00:00:01", "00:00:00:00:00:02",
                  "00:00:00:00:00:04", "00:00:00:00:00:0f")
    ctrl = _controller({a: _stats(99.0), b: _stats(99.0), e: _stats(99.0), w: None})
    _seed_hops(ctrl, {a: 1.0, b: 1.0, e: 2.0, w: 1.0})
    now = time.monotonic()
    ctrl._warm_server_leases[w] = WarmLease(started_ts=now, expires_ts=now + 45.0)

    original_floor = selection._BACKEND_SELECTION_SPREAD_MIN_BAND
    try:
        selection._BACKEND_SELECTION_SPREAD_MIN_BAND = 1
        picks = [selection.select_server(ctrl, client)["mac"] for client in CLIENTS]
        counts = {mac: picks.count(mac) for mac in (a, b, w)}
        _assert(all(counts[mac] > 0 for mac in (a, b, w)),
                f"warm claim joins the band, all receive clients: {counts}")
        _assert(e not in picks, "beyond-eps candidate stays out with a warm claim live")

        first = selection.select_server(ctrl, CLIENTS[3])["mac"]
        again = selection.select_server(ctrl, CLIENTS[3])["mac"]
        _assert(first == again, "warm band is deterministic per client")
    finally:
        selection._BACKEND_SELECTION_SPREAD_MIN_BAND = original_floor

    # Legacy: the warm claim owns the selection exclusively.
    original = selection._BACKEND_SELECTION_SPREAD_EPS
    try:
        selection._BACKEND_SELECTION_SPREAD_EPS = 0.0
        ctrl._warm_server_leases[w] = WarmLease(started_ts=now, expires_ts=now + 45.0)
        picks = {selection.select_server(ctrl, client)["mac"] for client in CLIENTS}
        _assert(picks == {w}, "eps=0: warm claim monopolises (legacy)")
    finally:
        selection._BACKEND_SELECTION_SPREAD_EPS = original


def test_peer_guard() -> None:
    # Runs under the default floor (k = 4): the floor can only widen with
    # LOCAL units, so the peer must stay out even with the floor active.
    a, p = "00:00:00:00:00:01", "00:00:00:00:00:09"
    ctrl = _controller({a: _stats(0.0), p: _stats(0.0)}, hop_max=4.0, peers={p})
    _seed_hops(ctrl, (a, p))
    picks = {selection.select_server(ctrl, client)["mac"] for client in CLIENTS}
    _assert(picks == {a},
            "cross-LAN unit beyond eps never enters a local band (peer guard)")


def test_single_candidate_and_nan_band() -> None:
    a, c = "00:00:00:00:00:01", "00:00:00:00:00:03"

    # Single candidate: band size 1 (no spread possible, still serves).
    ctrl = _controller({a: _stats(0.0)})
    _seed_hops(ctrl, (a,))
    picks = {selection.select_server(ctrl, client)["mac"] for client in CLIENTS}
    _assert(picks == {a}, "single-candidate band returns the only member")

    # All-NaN costs (malformed telemetry): no comparable minimum -> None.
    nan = float("nan")
    ctrl = _controller({a: _stats(nan), c: _stats(nan)})
    _seed_hops(ctrl, (a, c))
    _assert(selection.select_server(ctrl, CLIENTS[0]) is None,
            "all-NaN costs return None instead of raising")


def test_band_floor() -> None:
    # Six locals, costs spaced 0.04 apart (cpu 0..100, hops equal) -> the
    # eps-band is {a} alone; the default floor k = 4 must widen it to the
    # four cheapest locals, so a strict single winner never takes a burst.
    macs = [f"00:00:00:00:00:{i:02x}" for i in range(1, 7)]
    a, b, c, d, e, f = macs
    ctrl = _controller({m: _stats(i * 20.0) for i, m in enumerate(macs)})
    _seed_hops(ctrl, tuple(macs))

    picks = [selection.select_server(ctrl, client)["mac"] for client in CLIENTS]
    distinct = set(picks)
    _assert(distinct == {a, b, c, d},
            f"floor widens a single-winner band to the 4 cheapest locals: {distinct}")
    _assert(e not in distinct and f not in distinct,
            "units beyond the floor never receive clients")
    _assert(all(picks.count(m) > 4 for m in (a, b, c, d)),
            "floor band is roughly balanced "
            f"({[picks.count(m) for m in (a, b, c, d)]})")
    first = selection.select_server(ctrl, CLIENTS[0])["mac"]
    again = selection.select_server(ctrl, CLIENTS[0])["mac"]
    _assert(first == again, "floor band is deterministic per client")

    # Clamp: only two eligible locals -> band size 2, no error.
    a2, b2 = "00:00:00:00:00:01", "00:00:00:00:00:02"
    ctrl2 = _controller({a2: _stats(0.0), b2: _stats(20.0)})
    _seed_hops(ctrl2, (a2, b2))
    picks2 = {selection.select_server(ctrl2, client)["mac"] for client in CLIENTS}
    _assert(picks2 == {a2, b2}, "floor clamps to the number of eligible locals")

    # Floor never adds peers — even when a peer is cheaper than the next
    # local (x at 0.28 < b at 0.67): widening uses LOCAL units only.
    a3, b3 = "00:00:00:00:00:01", "00:00:00:00:00:02"
    x3, y3 = "00:00:00:00:00:09", "00:00:00:00:00:0a"
    hot = SimpleNamespace(avg_cpu_percent=100.0, avg_ram_used_mb=100.0,
                          request_count=100)
    ctrl3 = _controller({a3: _stats(0.0), b3: hot, x3: _stats(0.0), y3: _stats(20.0)},
                        hop_max=4.0, peers={x3, y3})
    _seed_hops(ctrl3, (a3, b3, x3, y3))
    picks3 = {selection.select_server(ctrl3, client)["mac"] for client in CLIENTS}
    _assert(picks3 == {a3, b3},
            "floor widens with locals only; a cheaper beyond-eps peer stays out")

    # k <= 1 disables the floor: back to the eps-band (strict winner).
    original_floor = selection._BACKEND_SELECTION_SPREAD_MIN_BAND
    try:
        selection._BACKEND_SELECTION_SPREAD_MIN_BAND = 1
        picks4 = {selection.select_server(ctrl, client)["mac"] for client in CLIENTS}
        _assert(picks4 == {a}, "floor k=1 restores the P2.6 eps-only band")
        selection._BACKEND_SELECTION_SPREAD_MIN_BAND = 0
        picks5 = {selection.select_server(ctrl, client)["mac"] for client in CLIENTS}
        _assert(picks5 == {a}, "floor k=0 is inert")
    finally:
        selection._BACKEND_SELECTION_SPREAD_MIN_BAND = original_floor


def test_band_floor_guards() -> None:
    # Floor is a minimum, not a cap: an eps-band already wider than k stays.
    macs = [f"00:00:00:00:00:{i:02x}" for i in range(1, 6)]
    ctrl = _controller({m: _stats(80.0 + 3.0 * i) for i, m in enumerate(macs)})
    _seed_hops(ctrl, tuple(macs))
    picks = {selection.select_server(ctrl, client)["mac"] for client in CLIENTS}
    _assert(picks == set(macs),
            f"eps-band wider than k is left as-is (min, not cap): {picks}")

    # Mixed NaN + finite: the floor tops up with finite locals only.
    a, b, nan_m, c, d = "00:00:00:00:00:01", "00:00:00:00:00:02", \
        "00:00:00:00:00:03", "00:00:00:00:00:04", "00:00:00:00:00:05"
    ctrl2 = _controller({a: _stats(0.0), b: _stats(20.0),
                         nan_m: _stats(float("nan")),
                         c: _stats(40.0), d: _stats(60.0)})
    _seed_hops(ctrl2, (a, b, nan_m, c, d))
    picks2 = {selection.select_server(ctrl2, client)["mac"] for client in CLIENTS}
    _assert(nan_m not in picks2 and a in picks2 and b in picks2
            and c in picks2 and d in picks2,
            f"floor skips NaN-cost locals and tops up with finite ones: {picks2}")

    # Unknown IP (no _mac_to_ip entry): never floor-added (ingress would
    # silently drop the packet).
    a2, noip, c2, d2 = ("00:00:00:00:00:01", "00:00:00:00:00:06",
                        "00:00:00:00:00:03", "00:00:00:00:00:04")
    ctrl3 = _controller({a2: _stats(0.0), noip: _stats(20.0),
                         c2: _stats(40.0), d2: _stats(60.0)})
    ctrl3._mac_to_ip.pop(noip)
    _seed_hops(ctrl3, (a2, noip, c2, d2))
    picks3 = {selection.select_server(ctrl3, client)["mac"] for client in CLIENTS}
    _assert(picks3 == {a2, c2, d2},
            f"floor skips the unknown-IP local and tops up with known-IP ones: {picks3}")

    # Active slow-start penalty: never floor-added (deterrence preserved).
    a3, ramping, c3, d3 = ("00:00:00:00:00:01", "00:00:00:00:00:06",
                           "00:00:00:00:00:03", "00:00:00:00:00:04")
    ctrl4 = _controller({a3: _stats(0.0), ramping: _stats(10.0),
                         c3: _stats(40.0), d3: _stats(60.0)})
    _seed_hops(ctrl4, (a3, ramping, c3, d3))
    now = time.monotonic()
    for m in (a3, c3, d3):
        ctrl4._backend_discovery_ts[m] = now - 60.0   # penalty fully decayed
    ctrl4._backend_discovery_ts[ramping] = now         # penalty = 1.0
    original_policy = selection._BACKEND_SELECTION_POLICY
    try:
        selection._BACKEND_SELECTION_POLICY = "topology_slowstart"
        picks4 = {selection.select_server(ctrl4, client)["mac"] for client in CLIENTS}
    finally:
        selection._BACKEND_SELECTION_POLICY = original_policy
    _assert(ramping not in picks4 and picks4 == {a3, c3, d3},
            f"floor skips units under an active slow-start penalty: {picks4}")


def test_band_floor_with_peer_band() -> None:
    # The eps-band itself holds a peer (legitimately within eps): the floor
    # tops the band up to k total members with the cheapest locals.
    a, b, c, d, e = (f"00:00:00:00:00:{i:02x}" for i in range(1, 6))
    p = "00:00:00:00:00:09"
    hot = SimpleNamespace(avg_cpu_percent=100.0, avg_ram_used_mb=100.0,
                          request_count=100)
    ctrl = _controller({a: hot, b: hot, c: hot, d: hot, e: hot, p: _stats(0.0)},
                       hop_max=4.0, peers={p})
    _seed_hops(ctrl, (a, b, c, d, e, p))
    picks = [selection.select_server(ctrl, client)["mac"] for client in CLIENTS]
    distinct = set(picks)
    _assert(p in distinct, "a peer within eps of the minimum stays in the band")
    _assert(len(distinct) == 4 and distinct - {p} <= {a, b, c, d},
            f"floor tops the peer band up to k total members: {distinct}")


def test_band_floor_with_warm() -> None:
    # Five spaced locals + a live warm claim whose cost is far beyond eps:
    # the warm unit joins as a share (not a monopoly) and counts toward the
    # floor, which fills the remaining seats with the cheapest locals.
    macs = [f"00:00:00:00:00:{i:02x}" for i in range(1, 6)]
    a, b, c, d, e = macs
    w = "00:00:00:00:00:0f"
    ctrl = _controller({**{m: _stats(i * 20.0) for i, m in enumerate(macs)},
                        w: None})
    _seed_hops(ctrl, tuple(macs) + (w,))
    now = time.monotonic()
    ctrl._warm_server_leases[w] = WarmLease(started_ts=now, expires_ts=now + 45.0)

    picks = [selection.select_server(ctrl, client)["mac"] for client in CLIENTS]
    distinct = set(picks)
    _assert(distinct == {a, b, c, w},
            f"warm claim counts toward the floor; band is {{a,b,c,w}}: {distinct}")
    _assert(d not in distinct and e not in distinct,
            "locals beyond the floor stay out")
    _assert(0 < picks.count(w) < len(CLIENTS),
            f"warm unit receives a share, not a monopoly (w={picks.count(w)})")


def test_log_format_pinned() -> None:
    import logging

    a, b = "00:00:00:00:00:01", "00:00:00:00:00:02"
    ctrl = _controller({a: _stats(0.0), b: _stats(20.0)})
    _seed_hops(ctrl, (a, b))

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):  # noqa: D102
            records.append(record.getMessage())

    handler = _Capture()
    original_level = selection.logger.level
    selection.logger.setLevel(logging.DEBUG)
    selection.logger.addHandler(handler)
    try:
        selection.select_server(ctrl, CLIENTS[0])
    finally:
        selection.logger.removeHandler(handler)
        selection.logger.setLevel(original_level)
    _assert(any("spread band=" in m and "floor=" in m and "warm=" in m
                for m in records),
            "selection log pins the evidence format (spread band=/floor=/warm=)")


def main() -> int:
    print("rq2v4 P2.6 selection-spread + P2.8 band-floor selftest")
    _assert(selection._BACKEND_SELECTION_SPREAD_EPS == 0.03,
            "default eps is 0.03 (env unset)")
    _assert(selection._BACKEND_SELECTION_SPREAD_MIN_BAND == 4,
            "default band floor is 4 (env unset)")
    print("-- band membership + spread")
    test_band_membership_and_spread()
    print("-- legacy eps=0")
    test_legacy_epsilon_zero()
    print("-- warm inclusion")
    test_warm_inclusion()
    print("-- peer guard")
    test_peer_guard()
    print("-- single candidate + NaN band")
    test_single_candidate_and_nan_band()
    print("-- band floor (P2.8)")
    test_band_floor()
    print("-- band floor guards (P2.8)")
    test_band_floor_guards()
    print("-- band floor with a peer in the eps-band (P2.8)")
    test_band_floor_with_peer_band()
    print("-- band floor + warm (P2.8)")
    test_band_floor_with_warm()
    print("-- selection log format pin")
    test_log_format_pinned()
    print("PASS: all rq2v4 selection-spread + band-floor assertions hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
