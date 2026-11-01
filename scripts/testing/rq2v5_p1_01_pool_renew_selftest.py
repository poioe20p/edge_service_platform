#!/usr/bin/env python3
"""rq2v5_p1_01_pool_renew_selftest.py — RQ2 v5 W1 selftest (plan S3.7).

Covers the W1 platform fix of
docs/operation/testing/experiment/v5/rq2/rq2v5_plan.md:

Pool renewal (edge, S3.4b) — cases a-g:
  a. flag off -> ``disabled``; no epoch change, no timers;
  b. flag on  -> wave A: ``epoch_id`` increments (``_get_current_epoch_snapshot``)
     and the previous epoch appears in ``_epoch_states[lan].retiring`` (the
     snapshot alone cannot show the retiring list); a leased retiring epoch is
     NOT closed by the sweep (live streams are never re-pinned);
  c. immediate second POST -> ``debounced``;
  d. lane mismatch -> 400; unknown LAN -> 400; malformed body -> 400;
  e. wave B: with ``EDGE_POOL_RENEW_WAVE_GAP_S=1`` a second rotation appears
     within ~2 s AND not before ~1 s (wave-B timing bound);
  f. supersession: two POSTs spaced > debounce and < gap -> the first wave B
     is cancelled, counted ``superseded``, exactly one wave B remains;
  g. retire/close: after rotation and lease drain the housekeeping sweep
     closes the previous client — asserted via the new close INFO line
     (S3.4b) and the retiring list emptying.

Storage spread band (S3.2b / S3.7.3), synthetic member costs [0.80, 0.82]:
  - EPS=0.03 / floor=2 -> both eligible members are in the band and selection
    round-robins within it (alternating picks);
  - EPS=0 -> strict argmin restored (v4 behaviour unchanged);
  - storage code never emits compute's ``spread band=`` token.

Env is read at import time by both modules under test, so every case runs in
a subprocess with its own environment (``--child <case>``) — the frozen plan's
design. Standalone house style (``_assert``, ``main()``, non-zero exit on any
failure), mirroring source/scripts/testing/rq2v4_p1_02_liveness_selftest.py.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# Import paths: this file lives at <repo>/source/scripts/testing/, so parents[2]
# is <repo>/source (the ``sdn_controller`` package root) and the edge image
# sources sit under <repo>/source/docker/edge_server/source.
SOURCE = Path(__file__).resolve().parents[2]
REPO = SOURCE.parent
EDGE_SOURCE = SOURCE / "docker" / "edge_server" / "source"

CLIENT_MAC = "aa:00:00:00:00:ff"
BAND_MAC_A = "aa:00:00:00:01:0a"
BAND_MAC_B = "aa:00:00:00:01:0b"
BAND_MAC_C = "aa:00:00:00:01:0c"

_EDGE_ON = {
    "EDGE_POOL_RENEW_ENABLED": "1",
    "EDGE_POOL_RENEW_MIN_INTERVAL_S": "15",
    "EDGE_POOL_RENEW_WAVES": "2",
    "EDGE_POOL_RENEW_WAVE_GAP_S": "45",
}

# The band cases pin the selection knobs their synthetic cost geometry depends
# on (defaults restated), so an ambient campaign env or a fresh shell cannot
# perturb them: with topology_slowstart every stub member would be
# undiscovered (penalty 1.0 -> ineligible -> empty band), and non-default
# weights would move the 0.80/0.82/0.90 costs the assertions are pinned to.
_BAND_ENV = {
    "BACKEND_SELECTION_POLICY": "topology_lifecycle",
    "W_STORAGE_CPU": "0.2",
    "W_STORAGE_RAM": "0.2",
    "W_STORAGE_CONNECTIONS": "0.1",
    "W_STORAGE_LAG": "0.2",
    "W_STORAGE_HOPS": "0.3",
}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {msg}")


class _LogCapture(logging.Handler):
    """Capture formatted log records from the module under test (child side)."""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _capture_logger(name: str) -> _LogCapture:
    handler = _LogCapture()
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def _child_edge_runtime():
    if str(EDGE_SOURCE) not in sys.path:
        sys.path.insert(0, str(EDGE_SOURCE))
    import vip_data_mongo_runtime as runtime  # noqa: PLC0415 — child-only import
    return runtime


def _child_client(lan_id: str):
    """Flask test client with the real control-plane routes (no listener)."""
    from flask import Flask  # noqa: PLC0415 — child-only import

    from control_plane_routes import register_control_plane_routes  # noqa: PLC0415

    app = Flask("pool_renew_selftest")
    register_control_plane_routes(
        app, SimpleNamespace(config=SimpleNamespace(lan_id=lan_id)),
    )
    return app.test_client()


def _post(client, lan: str, reason: str = "selftest") -> tuple[int, dict]:
    resp = client.post("/pool_renew", json={"lan": lan, "reason": reason})
    return resp.status_code, resp.get_json()


def _child_pool_renew_flag_off() -> dict:
    runtime = _child_edge_runtime()
    client = _child_client("lan1")
    before = runtime._get_current_epoch_snapshot("lan1")
    status, body = _post(client, "lan1")
    after = runtime._get_current_epoch_snapshot("lan1")
    state = runtime._get_lan_epoch_state("lan1")
    return {
        "status": status,
        "body": body,
        "before": before,
        "after": after,
        "retiring": [e.epoch_id for e in state.retiring],
        "timers": len(runtime._pool_renew_timers.get("lan1", [])),
        "mismatch": client.post("/pool_renew", json={"lan": "lan2"}).status_code,
        "unknown": client.post("/pool_renew", json={"lan": "lan9"}).status_code,
        "malformed": client.post(
            "/pool_renew", data="{not json", content_type="application/json",
        ).status_code,
        "empty": client.post("/pool_renew").status_code,
    }


def _child_pool_renew_wave_a() -> dict:
    runtime = _child_edge_runtime()
    client = _child_client("lan1")
    logs = _capture_logger("vip_data_mongo_runtime")
    runtime._lease_current_epoch("lan1")          # live read path on epoch 1
    before = runtime._get_current_epoch_snapshot("lan1")
    status, body = _post(client, "lan1")
    after = runtime._get_current_epoch_snapshot("lan1")
    state = runtime._get_lan_epoch_state("lan1")
    # A retiring epoch that still holds a lease must NOT be closed.
    runtime._close_drained_epochs()
    return {
        "status": status,
        "body": body,
        "before": before,
        "after": after,
        "retiring": [
            {"epoch_id": e.epoch_id, "retiring": e.retiring,
             "lease_count": e.lease_count}
            for e in state.retiring
        ],
        "timers": len(runtime._pool_renew_timers.get("lan1", [])),
        "renew_logs": [m for m in logs.messages if m.startswith("pool_renew:")],
        "close_logs": [m for m in logs.messages if "closed (leases=0)" in m],
    }


def _child_pool_renew_debounce() -> dict:
    runtime = _child_edge_runtime()
    client = _child_client("lan1")
    status1, first = _post(client, "lan1")
    snap1 = runtime._get_current_epoch_snapshot("lan1")
    status2, second = _post(client, "lan1")
    snap2 = runtime._get_current_epoch_snapshot("lan1")
    return {
        "status1": status1, "first": first, "snap1": snap1,
        "status2": status2, "second": second, "snap2": snap2,
    }


def _child_pool_renew_wave_b() -> dict:
    runtime = _child_edge_runtime()
    client = _child_client("lan1")
    logs = _capture_logger("vip_data_mongo_runtime")
    wave_a_epoch = None
    seen_at = None
    t0 = time.monotonic()
    status, body = _post(client, "lan1")
    wave_a_epoch = body.get("epoch_id")
    snap = runtime._get_current_epoch_snapshot("lan1")
    deadline = t0 + 5.0
    while time.monotonic() < deadline:
        snap = runtime._get_current_epoch_snapshot("lan1")
        if snap["epoch_id"] != wave_a_epoch:
            seen_at = time.monotonic() - t0
            break
        time.sleep(0.02)
    return {
        "status": status, "body": body, "wave_a_epoch": wave_a_epoch,
        "seen_at": seen_at, "final": snap,
        "wave2_logs": [m for m in logs.messages if "wave=2" in m],
    }


def _child_pool_renew_supersede() -> dict:
    runtime = _child_edge_runtime()
    client = _child_client("lan1")
    logs = _capture_logger("vip_data_mongo_runtime")
    t0 = time.monotonic()
    status1, first = _post(client, "lan1")
    timers_after_first = len(runtime._pool_renew_timers.get("lan1", []))
    time.sleep(1.2)                       # > debounce (1 s), < wave gap (2 s)
    status2, second = _post(client, "lan1")
    timers_after_second = len(runtime._pool_renew_timers.get("lan1", []))
    snap = runtime._get_current_epoch_snapshot("lan1")
    deadline = t0 + 6.0
    while time.monotonic() < deadline and snap["epoch_id"] == second.get("epoch_id"):
        time.sleep(0.02)
        snap = runtime._get_current_epoch_snapshot("lan1")
    time.sleep(0.2)                       # let a (wrongly) surviving timer log
    return {
        "status1": status1, "first": first, "timers_after_first": timers_after_first,
        "status2": status2, "second": second, "timers_after_second": timers_after_second,
        "final": snap,
        "superseded_logs": [m for m in logs.messages if "superseded" in m],
        "wave2_logs": [m for m in logs.messages if "wave=2" in m],
    }


def _child_pool_renew_close() -> dict:
    runtime = _child_edge_runtime()
    client = _child_client("lan1")
    logs = _capture_logger("vip_data_mongo_runtime")
    epoch = runtime._lease_current_epoch("lan1")
    client_created = runtime._get_or_create_epoch_client("lan1", epoch) is not None
    runtime._release_epoch("lan1", epoch)
    status, body = _post(client, "lan1")
    state = runtime._get_lan_epoch_state("lan1")
    retiring_before = [e.epoch_id for e in state.retiring]
    runtime._close_drained_epochs()               # the housekeeping sweep body
    retiring_after = [e.epoch_id for e in state.retiring]
    return {
        "status": status, "body": body,
        "old_epoch": epoch.epoch_id, "lease_count": epoch.lease_count,
        "client_created": client_created,
        "retiring_before": retiring_before, "retiring_after": retiring_after,
        "close_logs": [m for m in logs.messages if "closed (leases=0)" in m],
    }


def _band_stub_controller(members):
    """Stub controller for band tests (`members`: (mac, ip, cpu%, hops) rows).

    Cost = 0.2 * cpu/cpu_max + 0.3 * hops/_hop_cache_max (ram/conn/lag zero),
    with `_hop_cache_max` = 30 — so cpu 90/hops 62 is exactly 0.80,
    cpu 100/hops 62 is 0.82, cpu 100/hops 70 is 0.90 and cpu 100/hops 100
    is 1.20.
    """
    import threading

    from sdn_controller.telemetry.models import StorageServerSummary

    class _Stub:
        pass

    ctrl = _Stub()
    ctrl.peer_hosts = {}
    ctrl._mac_to_ip = {mac: ip for mac, ip, _, _ in members}
    ctrl._storage_stats = {
        mac: StorageServerSummary(
            avg_cpu_percent=float(cpu), avg_ram_used_mb=0.0,
            avg_connections=0.0, avg_repl_lag_s=0.0, member_state="SECONDARY")
        for mac, _, cpu, _ in members
    }
    ctrl.vip_storage_pool_n1 = {
        mac: {"mac": mac, "ip": ip} for mac, ip, _, _ in members
    }
    ctrl.vip_storage_pool_n2 = {}
    ctrl.hop_cache = {CLIENT_MAC: {mac: hops for mac, _, _, hops in members}}
    ctrl._hop_cache_max = 30
    ctrl.host_attachment = {}
    ctrl._avg_hop_count = 1.0
    ctrl._peer_avg_hop_count = 1.0
    ctrl._backend_discovery_ts = {}
    ctrl._warm_lock = threading.Lock()
    ctrl._warm_rr_idx = 0
    ctrl._warm_storage_leases = {"n1": {}, "n2": {}}
    ctrl._rr_storage_idx = {}
    return ctrl


_BAND_PAIR = [
    (BAND_MAC_A, "10.0.0.4", 90, 62),     # cost 0.80
    (BAND_MAC_B, "10.0.0.5", 100, 62),    # cost 0.82 (inside eps=0.03)
]
_BAND_FLOOR_TRIPLE = [
    (BAND_MAC_A, "10.0.0.4", 90, 62),     # cost 0.80 (outside eps of 0.90)
    (BAND_MAC_B, "10.0.0.5", 100, 70),    # cost 0.90 (floor top-up member)
    (BAND_MAC_C, "10.0.0.6", 100, 100),   # cost 1.20 (never band-eligible)
]


def _child_storage_band(variant: str = "pair") -> dict:
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from sdn_controller._vip_routing import selection  # noqa: PLC0415

    logs = _capture_logger("os_ken.vip_routing")
    ctrl = _band_stub_controller(_BAND_FLOOR_TRIPLE if variant == "floor"
                                 else _BAND_PAIR)
    picks = [selection.select_storage(ctrl, "n1", CLIENT_MAC)["mac"] for _ in range(4)]
    return {
        "picks": picks,
        "rr_idx": dict(ctrl._rr_storage_idx),
        "band_logs": [m for m in logs.messages if "storage_band=" in m],
        "spread_band_logs": [m for m in logs.messages if "spread band=" in m],
    }


def _child_storage_band_floor() -> dict:
    return _child_storage_band("floor")


_CHILD_CASES = {
    "pool_renew_flag_off": _child_pool_renew_flag_off,
    "pool_renew_wave_a": _child_pool_renew_wave_a,
    "pool_renew_debounce": _child_pool_renew_debounce,
    "pool_renew_wave_b": _child_pool_renew_wave_b,
    "pool_renew_supersede": _child_pool_renew_supersede,
    "pool_renew_close": _child_pool_renew_close,
    "storage_band": _child_storage_band,
    "storage_band_floor": _child_storage_band_floor,
}


def _run_child(case: str, env_overrides: dict) -> dict:
    env = dict(os.environ)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", case],
        env=env, capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"FAIL: child case {case} exited {proc.returncode}")
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("CHILD_RESULT "):
            return json.loads(line[len("CHILD_RESULT "):])
    raise SystemExit(f"FAIL: child case {case} produced no CHILD_RESULT")


def test_pool_renew_flag_off() -> None:
    r = _run_child("pool_renew_flag_off", {
        "EDGE_POOL_RENEW_ENABLED": "0",
        "EDGE_POOL_RENEW_MIN_INTERVAL_S": "15",
        "EDGE_POOL_RENEW_WAVES": "2",
        "EDGE_POOL_RENEW_WAVE_GAP_S": "45",
    })
    _assert(r["status"] == 200, "(a) route answers 200 with the flag off")
    _assert(r["body"]["rotated"] is False and r["body"]["reason"] == "disabled",
            "(a) flag off -> disabled, no rotation")
    _assert(r["after"]["epoch_id"] == r["before"]["epoch_id"],
            "(a) flag off -> current epoch unchanged")
    _assert(r["retiring"] == [] and r["timers"] == 0,
            "(a) flag off -> no retired epoch, no wave timers")
    _assert((r["mismatch"], r["unknown"], r["malformed"], r["empty"])
            == (400, 400, 400, 400),
            "(d) lane mismatch / unknown LAN / malformed body / empty body -> 400")


def test_pool_renew_wave_a() -> None:
    r = _run_child("pool_renew_wave_a", dict(_EDGE_ON))
    _assert(r["status"] == 200 and r["body"]["rotated"] is True,
            "(b) flag on -> route rotates the epoch")
    _assert(r["body"]["wave"] == 1 and r["body"]["reason"] == "selftest",
            "(b) wave A reported (wave=1, reason echoed)")
    before_id = r["before"]["epoch_id"]
    after_id = r["after"]["epoch_id"]
    _assert(isinstance(before_id, int) and after_id == before_id + 1,
            f"(b) epoch_id increments ({before_id} -> {after_id})")
    _assert(len(r["retiring"]) == 1
            and r["retiring"][0]["epoch_id"] == before_id
            and r["retiring"][0]["retiring"] is True,
            "(b) previous epoch appears in _epoch_states[lan].retiring")
    _assert(r["retiring"][0]["lease_count"] == 1,
            "(b) the live lease stays on the retired epoch (no re-pin)")
    _assert(r["close_logs"] == [],
            "(b) a leased retiring epoch is not closed by the sweep")
    _assert(r["timers"] == 1,
            "(b) exactly one wave-B timer pending after wave A")
    _assert(any(m == f"pool_renew: lan=lan1 reason=selftest wave=1 new_epoch={after_id}"
                for m in r["renew_logs"]),
            "(b) wave-A INFO line carries lane/reason/wave/new_epoch")


def test_pool_renew_debounce() -> None:
    r = _run_child("pool_renew_debounce", dict(_EDGE_ON))
    _assert(r["first"]["rotated"] is True and r["first"]["wave"] == 1,
            "(c) first POST rotates (wave A)")
    _assert(r["second"]["rotated"] is False
            and r["second"]["reason"] == "debounced"
            and r["second"]["wave"] == 0,
            "(c) immediate second POST -> debounced")
    _assert(r["second"]["epoch_id"] == r["snap1"]["epoch_id"]
            == r["snap2"]["epoch_id"],
            "(c) debounced POST leaves the current epoch untouched")


def test_pool_renew_wave_b() -> None:
    env = dict(_EDGE_ON)
    env.update({"EDGE_POOL_RENEW_WAVE_GAP_S": "1",
                "EDGE_POOL_RENEW_MIN_INTERVAL_S": "1"})
    r = _run_child("pool_renew_wave_b", env)
    _assert(r["status"] == 200 and r["body"]["wave"] == 1,
            "(e) wave A fires on the POST")
    _assert(r["seen_at"] is not None, "(e) wave B rotation observed")
    _assert(r["seen_at"] >= 0.9,
            f"(e) wave B not before the gap (seen at {r['seen_at']:.2f}s >= 0.9s)")
    _assert(r["seen_at"] <= 2.5,
            f"(e) wave B within the timing bound (seen at {r['seen_at']:.2f}s <= 2.5s)")
    _assert(r["final"]["epoch_id"] == r["wave_a_epoch"] + 1,
            "(e) wave B created exactly one further epoch")
    _assert(len(r["wave2_logs"]) == 1 and "wave=2" in r["wave2_logs"][0],
            "(e) exactly one wave-B INFO line")


def test_pool_renew_supersede() -> None:
    env = dict(_EDGE_ON)
    env.update({"EDGE_POOL_RENEW_WAVE_GAP_S": "2",
                "EDGE_POOL_RENEW_MIN_INTERVAL_S": "1"})
    r = _run_child("pool_renew_supersede", env)
    _assert(r["timers_after_first"] == 1,
            "(f) one wave-B timer pending after the first POST")
    _assert(r["second"]["rotated"] is True and r["second"]["wave"] == 1,
            "(f) the second admission past the debounce rotates immediately")
    _assert(r["second"]["epoch_id"] == r["first"]["epoch_id"] + 1,
            "(f) the second POST created one further epoch")
    _assert(r["timers_after_second"] == 1,
            "(f) exactly one wave B remains after supersession")
    _assert(r["superseded_logs"]
            == [f"pool_renew: lan=lan1 superseded=1 pending wave(s)"],
            "(f) the cancelled wave B is counted and logged as superseded")
    _assert(len(r["wave2_logs"]) == 1,
            "(f) only the surviving wave B fires (the cancelled one never runs)")
    _assert(r["final"]["epoch_id"] == r["second"]["epoch_id"] + 1,
            "(f) exactly one post-supersession wave B landed")


def test_pool_renew_close() -> None:
    r = _run_child("pool_renew_close", dict(_EDGE_ON))
    _assert(r["client_created"] is True and r["lease_count"] == 0,
            "(g) previous epoch holds a client and no leases (drained)")
    _assert(r["body"]["rotated"] is True,
            "(g) renewal rotated the epoch")
    _assert(r["retiring_before"] == [r["old_epoch"]],
            "(g) previous epoch is retiring before the sweep")
    _assert(r["retiring_after"] == [],
            "(g) the sweep empties the retiring list")
    _assert(r["close_logs"]
            == [f"retiring epoch {r['old_epoch']} for lan1 closed (leases=0)"],
            "(g) the new INFO close line names the closed epoch and lane")


def _band_env(**overrides) -> dict:
    env = dict(_BAND_ENV)
    env.update(overrides)
    return env


def test_storage_band() -> None:
    r = _run_child("storage_band", _band_env(
        STORAGE_SELECTION_SPREAD_EPS="0.03",
        STORAGE_SELECTION_SPREAD_MIN_BAND="2",
    ))
    ordered = sorted([BAND_MAC_A, BAND_MAC_B])
    _assert(r["picks"] == [ordered[0], ordered[1], ordered[0], ordered[1]],
            "(band) two-member band round-robins (alternating picks)")
    _assert(len(set(r["picks"])) == 2,
            "(band) both near-best members carry selections (spread, not one-hot)")
    _assert(len(r["band_logs"]) == 4,
            "(band) one band log line per selection")
    line = r["band_logs"][0]
    _assert("storage_band=2" in line and "eps=0.030" in line and "floor=2" in line,
            "(band) log token carries band size, eps and floor")
    _assert("warm=False" in line,
            "(band) log token carries the warm witness (False by design — "
            "deviation (ii): a warm claim early-returns)")
    _assert("min=0.8000" in line,
            "(band) band minimum is the synthetic 0.80 cost")
    _assert(r["spread_band_logs"] == [],
            "(band) storage never emits compute's `spread band=` token")


def test_storage_band_alternate_policy() -> None:
    """Regression: the band path must not depend on the warm-claim branch.

    Under topology_host/topology_slowstart no warm claim is attempted, so the
    band log's warm witness must be bound before the branch (pre-fix this
    raised UnboundLocalError inside select_storage).
    """
    r = _run_child("storage_band", _band_env(
        BACKEND_SELECTION_POLICY="topology_host",
        STORAGE_SELECTION_SPREAD_EPS="0.03",
        STORAGE_SELECTION_SPREAD_MIN_BAND="2",
    ))
    ordered = sorted([BAND_MAC_A, BAND_MAC_B])
    _assert(r["picks"] == [ordered[0], ordered[1], ordered[0], ordered[1]],
            "(alt policy) band still round-robins without a warm claim")
    _assert(len(r["band_logs"]) == 4 and "warm=False" in r["band_logs"][0],
            "(alt policy) band log emitted (no UnboundLocalError)")


def test_storage_band_floor() -> None:
    """P2.8 floor witness: eps-band alone is one member; the floor tops it up."""
    env_on = {"STORAGE_SELECTION_SPREAD_EPS": "0.03",
              "STORAGE_SELECTION_SPREAD_MIN_BAND": "2"}
    r = _run_child("storage_band_floor", _band_env(**env_on))
    ordered = sorted([BAND_MAC_A, BAND_MAC_B])
    _assert(r["picks"] == [ordered[0], ordered[1], ordered[0], ordered[1]],
            "(floor) floor top-up brings the second member into the band (RR)")
    _assert(BAND_MAC_C not in r["picks"],
            "(floor) the third member (cost 1.20) is never selected")
    _assert(len(r["band_logs"]) == 4 and "storage_band=2" in r["band_logs"][0],
            "(floor) band size 2 with eps=0.030 (floor active at 2)")
    _assert("min=0.8000" in r["band_logs"][0],
            "(floor) band minimum is still the strict 0.80 winner")
    _assert(r["spread_band_logs"] == [],
            "(floor) storage never emits compute's `spread band=` token")

    env_floor_off = dict(env_on)
    env_floor_off["STORAGE_SELECTION_SPREAD_MIN_BAND"] = "0"
    r2 = _run_child("storage_band_floor", _band_env(**env_floor_off))
    _assert(r2["picks"] == [BAND_MAC_A] * 4,
            "(floor off) eps-band alone collapses to the strict winner")
    _assert(len(r2["band_logs"]) == 4 and "storage_band=1" in r2["band_logs"][0],
            "(floor off) band size 1 (the top-up is what adds the second member)")


def test_storage_band_off() -> None:
    r = _run_child("storage_band", _band_env(
        STORAGE_SELECTION_SPREAD_EPS="0.0",
        STORAGE_SELECTION_SPREAD_MIN_BAND="0",
    ))
    _assert(r["picks"] == [BAND_MAC_A] * 4,
            "(band off) strict argmin restored (v4 behaviour)")
    _assert(r["band_logs"] == [] and r["rr_idx"] == {"n1": 4},
            "(band off) no band log; legacy tie counter advance only")
    _assert(r["spread_band_logs"] == [],
            "(band off) no compute `spread band=` token either")


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        case = sys.argv[2]
        if case not in _CHILD_CASES:
            print(f"FAIL: unknown child case {case}", file=sys.stderr)
            return 1
        print("CHILD_RESULT " + json.dumps(_CHILD_CASES[case]()))
        return 0

    print("rq2v5 P1.1 pool-renew + storage-band selftest")
    print("-- (a)/(d) flag off, lane validation")
    test_pool_renew_flag_off()
    print("-- (b) wave A rotation + retire")
    test_pool_renew_wave_a()
    print("-- (c) debounce")
    test_pool_renew_debounce()
    print("-- (e) wave B timing")
    test_pool_renew_wave_b()
    print("-- (f) supersession")
    test_pool_renew_supersede()
    print("-- (g) retire/close sweep")
    test_pool_renew_close()
    print("-- storage band (EPS=0.03/floor=2)")
    test_storage_band()
    print("-- storage band without a warm-claim branch (topology_host)")
    test_storage_band_alternate_policy()
    print("-- storage band floor top-up (MIN_BAND=2 vs 0)")
    test_storage_band_floor()
    print("-- storage band off (EPS=0)")
    test_storage_band_off()
    print("PASS: all rq2v5 W1 pool-renew/storage-band assertions hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
