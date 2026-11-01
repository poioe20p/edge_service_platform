#!/usr/bin/env python3
"""rq2v4_p1_02_liveness_selftest.py — RQ2 v4 P1.2 liveness/evidence selftest.

Exercises the P1 liveness/cull fix
(``docs/operation/testing/experiment/v4/rq2/rq2v4_p1_01_liveness_fix.md``):

- ``DynamicNodeRegistry.detect_absent``: tier-evidence gating (no HTTP
  evidence -> no compute absence; no storage evidence -> no storage absence;
  heartbeat-only presence counts; genuine absence still detected past the
  timeout; ``selective_storage`` follows the storage branch).
- ``DynamicNodeRegistry.refresh_presence``: delivery-path presence refresh
  independent of the housekeeping churn guard.
- ``update_server_stats``: heartbeats never overwrite measurements — an
  existing entry gets only its freshness stamp and drain state refreshed
  (an idle-but-alive node stays a scale-down candidate); a never-served
  node keeps a flagged placeholder; a removed node's cached summary is
  dropped so a recycled MAC never inherits foreign stats.
- Model/decode compatibility: legacy payloads (missing the new fields) default
  to pre-fix behaviour (``request_evidence=True``, ``heartbeat_only=False``);
  the fields flow through ``TelemetrySummary.model_validate`` in every
  delivery source (RQ1 mode compatibility).
- ``scaling_config._HOUSEKEEPING_STALE_S``: bounded staleness default.
- Aggregator window contract (subprocess): empty windows carry
  ``request_evidence=False``; edge heartbeat-only entries carry
  ``heartbeat_only=True``; HTTP-bearing windows carry ``request_evidence=True``.

The wrapper-level guards (append gate, recall feed, scale-down gate, stale
bound in ``main_n1``/``main_n2``) are covered by the P1.4 battery. The
``_pick_compute_scale_down_candidate`` wiring itself needs the full
controller runtime (os_ken); this selftest asserts the stats-level
invariants it relies on, and the battery exercises the wiring end to end.

Exit non-zero on any assertion failure. Runs on the Windows host with the
repo's Python (stdlib + pydantic + pyzmq). Gate: local validation only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Import path: this file lives at <repo>/source/scripts/testing/, so
# parents[2] is <repo>/source — the package root for ``sdn_controller``.
SOURCE = Path(__file__).resolve().parents[2]
REPO = SOURCE.parent
sys.path.insert(0, str(SOURCE))

import sdn_controller.node_registry as node_registry_mod  # noqa: E402
import sdn_controller.scaling_config as scaling_config  # noqa: E402
from sdn_controller._vip_routing.state import (  # noqa: E402
    drop_backend_caches,
    unregister_server_backend,
    unregister_storage_backend,
    update_server_stats,
    update_storage_stats,
)
from sdn_controller.elasticity.node_common import NodeInfo  # noqa: E402
from sdn_controller.node_registry import DynamicNodeRegistry  # noqa: E402
from sdn_controller.telemetry.models import (  # noqa: E402
    ServerSummary,
    StorageServerSummary,
    TelemetrySummary,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {msg}")


def _edge_summary(mac: str, heartbeat_only: bool = False) -> ServerSummary:
    return ServerSummary(
        avg_time_total_ms=5.0, avg_time_db_ms=1.0, avg_time_proc_ms=4.0,
        request_count=10, error_rate=0.0, avg_cpu_percent=20.0,
        avg_ram_used_mb=64.0, heartbeat_only=heartbeat_only,
    )


def _summary(request_evidence: bool = True, servers: dict | None = None,
             storage_servers: dict | None = None) -> TelemetrySummary:
    return TelemetrySummary.model_validate({
        "network_id": "lan1",
        "window_end": 0.0,
        "request_evidence": request_evidence,
        "servers": servers or {},
        "storage_servers": storage_servers or {},
    })


def _register(reg: DynamicNodeRegistry, mac: str, node_type: str = "compute",
              age_s: float | None = None) -> None:
    if age_s is None:
        age_s = node_registry_mod._TELEMETRY_TIMEOUT_S + 120.0
    now = time.monotonic()
    reg._dynamic_node_macs.add(mac)
    reg._active[mac] = NodeInfo(
        mac=mac, lan=1, network_id="lan1",
        name=f"selftest_{mac[-2:]}", ip="10.0.0.9", node_type=node_type,
    )
    reg._birth_ts[mac] = now - age_s
    reg._last_seen_mono[mac] = now - age_s


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_json(port: int, path: str, timeout_s: float = 3.0) -> dict:
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=timeout_s) as resp:
        return json.loads(resp.read().decode())


def _wait_json(port: int, path: str, timeout_s: float = 15.0) -> dict:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            return _get_json(port, path)
        except Exception as exc:  # noqa: BLE001 — retry until deadline
            last_err = exc
            time.sleep(0.2)
    raise RuntimeError(f"HTTP wait failed for {path}: {last_err}")


def _find_window(port: int, predicate, timeout_s: float = 15.0) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            data = _get_json(port, "/windows?after_seq=0&limit=200")
        except Exception:  # noqa: BLE001 — retry until deadline
            time.sleep(0.2)
            continue
        for win in data.get("windows", []):
            if predicate(win):
                return win
        time.sleep(0.3)
    return None


def test_registry_evidence_gating() -> None:
    timeout = node_registry_mod._TELEMETRY_TIMEOUT_S
    c_mac = "aa:00:00:00:00:01"

    reg = DynamicNodeRegistry()
    _register(reg, c_mac)
    _assert(reg.detect_absent(_summary(request_evidence=False)) == [],
            "no-evidence window does not age compute nodes")

    reg = DynamicNodeRegistry()
    _register(reg, c_mac, age_s=timeout - 60.0)
    _assert(reg.detect_absent(_summary(request_evidence=True)) == [],
            "request-evidenced window keeps a node below the timeout")
    reg = DynamicNodeRegistry()
    _register(reg, c_mac)
    _assert(reg.detect_absent(_summary(request_evidence=True)) == [c_mac],
            "request-evidenced window removes a node past the timeout")

    s_mac = "aa:00:00:00:00:02"
    reg = DynamicNodeRegistry()
    _register(reg, s_mac, node_type="storage")
    _assert(reg.detect_absent(_summary(request_evidence=True)) == [],
            "storage node is not aged on a storage-evidence-free window")
    other = "aa:00:00:00:00:03"
    win = _summary(request_evidence=True,
                   storage_servers={other: StorageServerSummary()})
    _assert(reg.detect_absent(win) == [s_mac],
            "storage-evidenced window removes an absent storage node")
    reg = DynamicNodeRegistry()
    _register(reg, s_mac, node_type="storage")
    win = _summary(request_evidence=True,
                   storage_servers={s_mac: StorageServerSummary()})
    _assert(reg.detect_absent(win) == [],
            "storage presence refreshes and prevents absence")

    sel_mac = "aa:00:00:00:00:04"
    reg = DynamicNodeRegistry()
    _register(reg, sel_mac, node_type="selective_storage")
    _assert(reg.detect_absent(_summary(request_evidence=True)) == [],
            "selective_storage node is not aged on a storage-evidence-free window")
    _assert(reg.detect_absent(_summary(
        request_evidence=True,
        storage_servers={other: StorageServerSummary()})) == [sel_mac],
            "storage-evidenced window removes an absent selective_storage node")

    reg = DynamicNodeRegistry()
    _register(reg, c_mac)
    before = reg._last_seen_mono[c_mac]
    win = _summary(request_evidence=False,
                   servers={c_mac: _edge_summary(c_mac, heartbeat_only=True)})
    _assert(reg.detect_absent(win) == [],
            "heartbeat-only presence is not absent")
    _assert(reg._last_seen_mono[c_mac] > before,
            "heartbeat-only presence refreshes the presence age")


def test_refresh_presence() -> None:
    c_mac = "aa:00:00:00:00:05"
    reg = DynamicNodeRegistry()
    _register(reg, c_mac)
    before = reg._last_seen_mono[c_mac]
    reg.refresh_presence(_summary(servers={c_mac: _edge_summary(c_mac)}))
    _assert(reg._last_seen_mono[c_mac] > before,
            "refresh_presence resets the age without housekeeping")
    older = reg._last_seen_mono[c_mac]
    reg.refresh_presence(_summary(servers={}))
    _assert(reg._last_seen_mono[c_mac] == older,
            "refresh_presence leaves silent nodes untouched")


def test_model_compat() -> None:
    legacy = {
        "network_id": "lan1", "window_end": 0.0,
        "servers": {"aa:00:00:00:00:06": {
            "avg_time_total_ms": 1.0, "avg_time_db_ms": 0.0,
            "avg_time_proc_ms": 1.0, "request_count": 1, "error_rate": 0.0,
            "avg_cpu_percent": 1.0, "avg_ram_used_mb": 1.0,
        }},
    }
    s = TelemetrySummary.model_validate(legacy)
    _assert(s.request_evidence is True,
            "legacy payload defaults request_evidence=True (pre-fix behaviour)")
    _assert(s.servers["aa:00:00:00:00:06"].heartbeat_only is False,
            "legacy payload defaults heartbeat_only=False")
    s2 = TelemetrySummary.model_validate({
        "network_id": "lan1", "window_end": 0.0, "request_evidence": False,
        "servers": {},
    })
    _assert(s2.request_evidence is False, "explicit request_evidence flows")

    tick = scaling_config._CONTROL_TICK_S
    window_s = float(os.environ.get("WINDOW_S", "10"))
    expected = max(6.0 * tick, 6.0 * window_s) + tick
    _assert(abs(scaling_config._HOUSEKEEPING_STALE_S - expected) < 1e-9,
            f"stale bound == max(6*TICK, 6*WINDOW_S)+TICK ({expected:.0f} s)")
    _assert(scaling_config._HOUSEKEEPING_STALE_S >= 2.0 * 3.0 * window_s,
            "stale bound >= twice the longest delivery period (sampled push)")


def test_update_server_stats() -> None:
    class _DummyController:
        def __init__(self) -> None:
            self._server_stats: dict = {}
            self._backend_discovery_ts: dict = {}

    mac = "aa:00:00:00:00:07"

    # Never-served node: the heartbeat keeps a flagged placeholder so the
    # node stays a graceful scale-down candidate.
    ctrl = _DummyController()
    update_server_stats(ctrl, {mac: _edge_summary(mac, heartbeat_only=True)})
    _assert(mac in ctrl._server_stats
            and ctrl._server_stats[mac].heartbeat_only,
            "heartbeat placeholder created for a never-served node")
    _assert(mac in ctrl._backend_discovery_ts,
            "heartbeat stamps backend discovery")

    # Served node: a later heartbeat refreshes only freshness + drain state.
    ctrl = _DummyController()
    real = _edge_summary(mac)
    real.last_report_ts = 100.0
    update_server_stats(ctrl, {mac: real})
    hb = _edge_summary(mac, heartbeat_only=True)
    hb.last_report_ts = 200.0
    hb.state = "draining"
    update_server_stats(ctrl, {mac: hb})
    stored = ctrl._server_stats[mac]
    _assert(stored.request_count == 10 and stored.avg_time_total_ms == 5.0,
            "heartbeat does not overwrite real measurements")
    _assert(stored.last_report_ts == 200.0 and stored.state == "draining",
            "heartbeat refreshes freshness stamp and drain state")

    # Out-of-order heartbeat does not regress the stamp or the state.
    stale_hb = _edge_summary(mac, heartbeat_only=True)
    stale_hb.last_report_ts = 150.0
    update_server_stats(ctrl, {mac: stale_hb})
    _assert(ctrl._server_stats[mac].last_report_ts == 200.0,
            "older heartbeat does not regress the freshness stamp")

    # The refreshed stamp keeps the node inside the candidate staleness gate.
    fresh_hb = _edge_summary(mac, heartbeat_only=True)
    fresh_hb.last_report_ts = time.time()
    update_server_stats(ctrl, {mac: fresh_hb})
    staleness = time.time() - ctrl._server_stats[mac].last_report_ts
    _assert(staleness <= scaling_config._SCALE_DOWN_CANDIDATE_MAX_STALENESS_S,
            "refresh keeps the node inside the scale-down staleness gate")


def test_unregister_clears_cached_stats() -> None:
    import threading

    class _Dummy:
        def __init__(self) -> None:
            self._server_stats: dict = {}
            self._storage_stats: dict = {}
            self._backend_discovery_ts: dict = {}
            self._warm_lock = threading.Lock()
            self._warm_server_leases: dict = {}
            self._warm_storage_leases: dict = {}
            self._local_server_macs: set = set()
            self._vip_server_client_map: dict = {}
            self._vip_data_conn_map: dict = {}
            self._vip_data_conn_lock = threading.Lock()
            self.datapaths: list = []

        def remove_server_mac(self, mac: str) -> None:
            self._local_server_macs.discard(mac)

        def remove_storage_mac(self, mac: str, domain: str) -> None:
            self._local_server_macs.discard(mac)

    mac = "aa:00:00:00:00:08"
    ctrl = _Dummy()
    update_server_stats(ctrl, {mac: _edge_summary(mac, heartbeat_only=True)})
    _assert(mac in ctrl._server_stats, "placeholder present before removal")
    unregister_server_backend(ctrl, mac)
    _assert(mac not in ctrl._server_stats,
            "unregister drops the cached summary (no recycled-MAC inheritance)")
    _assert(mac not in ctrl._backend_discovery_ts,
            "unregister drops the discovery stamp")

    smac = "aa:00:00:00:00:09"
    update_storage_stats(ctrl, {smac: StorageServerSummary()})
    unregister_storage_backend(ctrl, smac, "n1")
    _assert(smac not in ctrl._storage_stats,
            "storage unregister drops the cached summary")

    rmac = "aa:00:00:00:00:0a"
    update_storage_stats(ctrl, {rmac: StorageServerSummary()})
    drop_backend_caches(ctrl, rmac)
    _assert(rmac not in ctrl._storage_stats
            and rmac not in ctrl._backend_discovery_ts,
            "drop_backend_caches clears a reserve-style removal")


def test_aggregator_window_contract() -> None:
    import zmq  # local import: the aggregator test is the only zmq user

    agg_dir = REPO / "source" / "docker" / "local_state_server"
    pull_port, pub_port, cache_port = _free_port(), _free_port(), _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="rq2v4_liveness_selftest_"))
    env = dict(os.environ)
    env.update({
        "NETWORK_ID": "selftest",
        "PULL_ADDR": f"tcp://127.0.0.1:{pull_port}",
        "PUB_ADDR": f"tcp://127.0.0.1:{pub_port}",
        "SUMMARY_CACHE_PORT": str(cache_port),
        "WINDOW_S": "1",
        "WINDOW_LOG_PATH": str(tmp / "window_log.jsonl"),
        "ACK_LOG_PATH": str(tmp / "ack_log.jsonl"),
        "LOG_LEVEL": "WARNING",
    })
    proc = subprocess.Popen(
        [sys.executable, "aggregator.py"], cwd=str(agg_dir), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctx = zmq.Context()
    pusher = None
    try:
        _wait_json(cache_port, "/latest_summary", timeout_s=10.0)
        pusher = ctx.socket(zmq.PUSH)
        pusher.connect(f"tcp://127.0.0.1:{pull_port}")
        time.sleep(1.0)  # slow-joiner: give the PULL side time to connect

        hb_mac = "aa:bb:cc:dd:ee:01"
        for _ in range(2):  # duplicate heartbeats are harmless; guards a drop
            pusher.send_json({
                "event_type": "heartbeat", "server_id": hb_mac,
                "ts": time.time(), "cpu_percent": 1.0, "ram_used_mb": 1.0,
                "state": "active",
            })
            time.sleep(0.2)
        hb_win = _find_window(
            cache_port, lambda w: hb_mac in (w.get("servers") or {}))
        _assert(hb_win is not None, "heartbeat-only window is published")
        _assert(hb_win.get("request_evidence") is False,
                "heartbeat-only window carries request_evidence=False")
        _assert(hb_win["servers"][hb_mac].get("heartbeat_only") is True,
                "heartbeat-only edge entry carries heartbeat_only=True")

        http_mac = "aa:bb:cc:dd:ee:02"
        pusher.send_json({
            "server_id": http_mac, "ts": time.time(), "time_total_ms": 5.0,
            "time_db_ms": 1.0, "time_db_read_ms": 1.0, "time_db_write_ms": 0.0,
            "time_db_cmd_count": 1, "status_code": 200, "request_type": "read",
            "cpu_percent": 2.0, "ram_used_mb": 2.0, "state": "active",
        })
        http_win = _find_window(
            cache_port,
            lambda w: w.get("request_evidence") is True
            and (w.get("domain_summary") or {}).get("total_requests", 0) >= 1)
        _assert(http_win is not None,
                "HTTP-bearing window carries request_evidence=True")
        _assert(http_win["servers"][http_mac].get("heartbeat_only", False) is False,
                "HTTP stats entry is not flagged heartbeat_only")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if pusher is not None:
            pusher.close()
        ctx.destroy(linger=0)


def main() -> int:
    print("rq2v4 P1.2 liveness/evidence selftest")
    print("-- registry evidence gating")
    test_registry_evidence_gating()
    print("-- presence refresh")
    test_refresh_presence()
    print("-- model/decode compatibility + stale bound")
    test_model_compat()
    print("-- routing stats exclusion + unregister hygiene")
    test_update_server_stats()
    test_unregister_clears_cached_stats()
    print("-- aggregator window contract (subprocess)")
    test_aggregator_window_contract()
    print("PASS: all rq2v4 P1.2 liveness assertions hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
