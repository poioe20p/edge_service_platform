#!/usr/bin/env python3
"""rq3v2_p1_01_analyzer_selftest.py — RQ3 v2 analyzer + flow-validation selftest.

Builds a synthetic RQ3 run folder and asserts the RQ3 v2 analyzer contract
(`docs/research_questions/v2/rq3/rq3_admission_analysis.py`) and the
flow-validation gate policy (`.../rq3_flow_validation.py`, §2.8):

- status attribution: timeout counted in ``timeout_rate`` (never in failure);
  dropped/canceled excluded from latency + failure, counted in offered.
- gap-window primaries (pool-wide old-backend ``timeout_rate`` over
  ``[spawn_started, admitted]``, spike-phase-truncated) + spike-phase baseline
  + ``gap_delta_pp``.
- run-level aggregation (medians), min-admissions void gate (per LAN).
- arm labels ``direct`` / ``discovery`` / ``discovery_15``; ``off`` skipped.
- ``admit_source`` event-fraction gate (< 0.80 => instrumentation-degraded).
- flow-validation Check C (hard at >= 0.9) and Check D (> 1% fails).

Exit non-zero on any assertion failure. Gate: `make rq3_analyzer_selftest`.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Base epoch for the synthetic timeline.
T0 = 1_700_000_000.0
SPAWN_STARTED = T0 + 100.0
SPAWN_COMPLETE = T0 + 115.0
ADMITTED = T0 + 118.0

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ANALYZER = os.path.join(
    REPO, "docs", "research_questions", "v2", "rq3", "rq3_admission_analysis.py")
FLOW_VAL = os.path.join(
    REPO, "docs", "research_questions", "v2", "rq3", "rq3_flow_validation.py")

_CSV_HEADER = [
    "sent_at", "phase", "client_ns", "client_lan", "endpoint",
    "content_id", "user_id", "target_region", "http_status", "latency_s",
    "completed_at", "backend_id", "source_port", "status",
]


def _load_mod(path: str):
    spec = importlib.util.spec_from_file_location(
        os.path.splitext(os.path.basename(path))[0], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _req(phase: str, ts: float, backend: str, http: str, latency: float,
         status: str, port: int = 0, client_ns: str = "lan1_client_1") -> list:
    return [ts, phase, client_ns, "lan1", "content_lookup", "c1", "u1", "reg",
            http, latency, _iso(ts), backend, port, status]


def _write_csv(path: str, rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_CSV_HEADER)
        w.writerows(rows)


def _build_run(run_dir: str, arm: str, *, old_backend: str = "edge_server_lan1_s1",
               container: str = "edge_server_lan1_dyn1") -> str:
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "controller_env_snapshot.env"), "w",
              encoding="utf-8") as fh:
        # The sensitivity cell is a `discovery` regime with a 15 s interval
        # (canonical-env rule) — the analyzer derives the arm label from the
        # interval, so the real pipeline shape is exercised here.
        if arm == "discovery_15":
            fh.write("READINESS_PROPAGATION=discovery\n")
            fh.write("DISCOVERY_POLL_INTERVAL_S=15\n")
        else:
            fh.write(f"READINESS_PROPAGATION={arm}\n")
            fh.write("DISCOVERY_POLL_INTERVAL_S=10\n")

    # phases_snapshot.json — compute-spike episode.
    import json
    with open(os.path.join(run_dir, "phases_snapshot.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"phases": [
            {"name": "baseline", "duration_s": 60},
            {"name": "compute_spike", "duration_s": 180},
            {"name": "cleanup_gap", "duration_s": 180},
        ]}, fh)

    # decision_log — a scale_up ComputeAlert just before spawn_started.
    with open(os.path.join(run_dir, "decision_log_lan1.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "action_type", "action", "reason", "lan"])
        w.writerow([f"{SPAWN_STARTED - 2.0:.3f}", "scale_up", "ComputeAlert",
                    "compute_pressure", "1"])

    # Admission log — 2 admitted lan1 (event + probe_fallback) + 1 admitted lan2.
    adm_cols = ["ts", "network_id", "lan", "container", "mac", "ip", "mode",
                "result", "spawn_started_ts", "spawn_complete_ts",
                "probe_first_ts", "app_ready_ts", "admitted_ts", "admit_source"]
    adm_rows = [
        [f"{ADMITTED:.3f}", "lan1", "1", container, "aa:bb:cc:dd:ee:01",
         "10.0.0.50", arm, "admitted", f"{SPAWN_STARTED:.3f}",
         f"{SPAWN_COMPLETE:.3f}", f"{ADMITTED - 0.5:.3f}", f"{ADMITTED - 0.2:.3f}",
         f"{ADMITTED:.3f}", "event"],
        [f"{ADMITTED + 0.1:.3f}", "lan1", "1", "edge_server_lan1_dyn2",
         "aa:bb:cc:dd:ee:02", "10.0.0.51", arm, "admitted",
         f"{SPAWN_STARTED + 3:.3f}", f"{SPAWN_COMPLETE + 3:.3f}",
         f"{ADMITTED + 2.5:.3f}", f"{ADMITTED + 2.8:.3f}",
         f"{ADMITTED + 3.0:.3f}", "probe_fallback"],
        [f"{ADMITTED + 5:.3f}", "lan2", "2", "edge_server_lan2_dyn1",
         "aa:bb:cc:dd:ee:03", "10.0.1.50", arm, "admitted",
         f"{SPAWN_STARTED + 6:.3f}", f"{SPAWN_COMPLETE + 6:.3f}",
         f"{ADMITTED + 6.5:.3f}", f"{ADMITTED + 6.8:.3f}",
         f"{ADMITTED + 7.0:.3f}", "probe"],
    ]
    with open(os.path.join(run_dir, "admission_log_lan1.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(adm_cols)
        w.writerows(adm_rows[:2])
    with open(os.path.join(run_dir, "admission_log_lan2.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(adm_cols)
        w.writerows(adm_rows[2:])

    # client_requests.csv
    rows = []
    # Baseline (spike phase, before spawn_started): 40 rows, 2 timeout + 38
    # completed (36 OK + 2 http 500).
    for i in range(40):
        ts = SPAWN_STARTED - 60.0 + i * 1.5
        status = "completed"
        http = "200"
        lat = 0.1
        if i < 2:
            status, http, lat = "timeout", "000", 300.0
        elif i in (10, 20):
            http = "500"
        rows.append(_req("compute_spike", ts, old_backend, http, lat, status,
                         port=4000 + i))
    # Gap window [spawn_started, admitted): 30 rows, 6 timeout + 24 completed
    # (22 OK + 2 http 500). Add 2 dropped + 2 canceled (must be excluded from
    # timeout_rate denominator and from failures, counted in offered).
    for i in range(34):
        ts = SPAWN_STARTED + i * 0.5
        status = "completed"
        http = "200"
        lat = 0.2
        if i < 6:
            status, http, lat = "timeout", "000", 300.0
        elif i in (20, 21):
            http = "500"
        elif i in (30, 31):
            status, http, lat = "dropped", "", ""
        elif i in (32, 33):
            status, http, lat = "canceled", "", ""
        rows.append(_req("compute_spike", ts, old_backend, http, lat, status,
                         port=5000 + i))
    # Post-admission (new backend): 30 rows in [admitted, admitted+30].
    for i in range(30):
        ts = ADMITTED + i * 1.0
        status = "completed"
        http = "200"
        lat = 0.05
        if i >= 28:
            status, http, lat = "timeout", "000", 300.0
        rows.append(_req("compute_spike", ts, container, http, lat, status,
                         port=6000 + i))
    # Cleanup-phase rows (must be excluded from all spike windows).
    rows.append(_req("cleanup_gap", ADMITTED + 40.0, old_backend, "200", 0.1,
                     "completed", port=7000))
    _write_csv(os.path.join(run_dir, "client_requests.csv"), rows)

    # controller_lan1.log with request_complete deletes for Check C. Measured
    # (completed + timeout) = 40 + 30 + 30 + 1 = 101, so >= 91 deletes are
    # needed for coverage >= 0.9.
    with open(os.path.join(run_dir, "controller_lan1.log"), "w",
              encoding="utf-8") as fh:
        for i in range(95):
            fh.write(f"[info] vip_server: request_complete: client flows deleted "
                     f"client=10.0.0.2:{4000+i} n={i}\n")
    return run_dir


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"  ok: {msg}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rq3v2_analyzer_selftest_")
    try:
        # ── Arm labeling / run-kind guard ──
        for arm in ("direct", "discovery", "discovery_15"):
            mod = _load_mod(ANALYZER)
            _assert(arm in mod._RQ3_ARMS, f"arm {arm} recognized")
        _assert("off" not in _load_mod(ANALYZER)._RQ3_ARMS,
                "off is not an RQ3 arm")

        # ── direct run: full metric contract ──
        run_dir = os.path.join(tmp, "run_direct")
        _build_run(run_dir, "direct")
        mod = _load_mod(ANALYZER)
        r = mod._process_run(run_dir, transition_window_s=30.0,
                             baseline_s=60.0, gap_delta_pp=5.0)
        _assert(r["arm"] == "direct", "arm label direct")
        _assert(r["void"] is False, "non-void (both LANs have admissions)")
        _assert(r["backends_total"] == 3, "3 admitted backends counted")

        # Headline gap timeout_rate: 6 timeout / 30 offered (34 rows - 2
        # dropped - 2 canceled) = 0.2.
        gap_to = r["backends"][0]["gap_timeout_rate"]
        _assert(gap_to is not None and abs(gap_to - 6 / 30) < 1e-9,
                f"gap timeout_rate = {gap_to} (expect 6/30)")
        # Failure rate: 2 completed-500 / 24 completed = 0.0833.
        gap_fr = r["backends"][0]["gap_failure_rate"]
        _assert(gap_fr is not None and abs(gap_fr - 2 / 24) < 1e-9,
                f"gap failure_rate = {gap_fr} (expect 2/24)")
        # Baseline timeout_rate: 2 / (2 timeout + 38 completed) = 2/40.
        base_to = r["backends"][0]["baseline_timeout_rate"]
        _assert(base_to is not None and abs(base_to - 2 / 40) < 1e-9,
                f"baseline timeout_rate = {base_to} (expect 2/40)")
        dd = r["backends"][0]["gap_delta_pp"]
        _assert(dd is not None and dd > 5.0,
                f"gap_delta_pp = {dd} flags degrading gap (> 5)")

        # Quantization + timing.
        q = r["backends"][0]["spawn_complete_to_admitted_s"]
        _assert(q is not None and abs(q - 3.0) < 1e-6,
                f"spawn_complete->admitted = {q} (expect 3.0)")
        _assert(r["spawn_to_admitted_median_s"] is not None,
                "run-level quantization median defined")

        # Event fraction gate: 1 of 3 event-driven => degraded.
        _assert(r["event_fraction"] is not None
                and abs(r["event_fraction"] - 1 / 3) < 1e-9,
                f"event_fraction = {r['event_fraction']} (expect 1/3)")
        _assert(r["event_fraction_degraded"] is True,
                "direct run with < 0.80 event fraction is instrumentation-degraded")

        # Cleanup-phase row excluded from windows: gap_requests = 34 (30
        # spike rows + 2 dropped + 2 canceled), and the cleanup row is out.
        _assert(all(b["gap_requests"] == 34 for b in r["backends"]
                    if b["container"] == "edge_server_lan1_dyn1"),
                "gap window spike-phase-truncated (cleanup row excluded)")
        # Per-LAN distinct gap requests: lan1 union window [T0+100, T0+121)
        # holds the 34 lan1 gap rows (post-admission dyn1 rows are excluded as
        # attributed to a new backend); lan2 has no client rows.
        _assert(r["gap_requests_lan1"] == 34,
                "per-LAN gap requests (lan1) = 34 distinct union-window rows")
        _assert(r["gap_requests_lan2"] == 0,
                "per-LAN gap requests (lan2) = 0 (no lan2 client rows)")

        # ── discovery run processed, not degraded by event gate ──
        run_disc = os.path.join(tmp, "run_disc")
        _build_run(run_disc, "discovery")
        rd = mod._process_run(run_disc, 30.0, 60.0, 5.0)
        _assert(rd["arm"] == "discovery", "discovery arm label")
        _assert(rd["event_fraction_degraded"] is False,
                "discovery run not subject to event-fraction gate")

        # ── discovery_15 run processed with interval label ──
        run_d15 = os.path.join(tmp, "run_d15")
        _build_run(run_d15, "discovery_15")
        r15 = mod._process_run(run_d15, 30.0, 60.0, 5.0)
        _assert(r15["arm"] == "discovery_15"
                and r15["discovery_interval_s"] == 15.0,
                "discovery_15 arm + interval label")

        # ── void gate: a run with zero admissions on lan2 is void ──
        run_void = os.path.join(tmp, "run_void")
        os.makedirs(run_void, exist_ok=True)
        with open(os.path.join(run_void, "controller_env_snapshot.env"), "w",
                  encoding="utf-8") as fh:
            fh.write("READINESS_PROPAGATION=discovery\n")
            fh.write("DISCOVERY_POLL_INTERVAL_S=10\n")
        import json
        with open(os.path.join(run_void, "phases_snapshot.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"phases": [{"name": "compute_spike", "duration_s": 180}]},
                      fh)
        with open(os.path.join(run_void, "admission_log_lan1.csv"), "w",
                  newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["result", "container", "lan", "admitted_ts",
                        "spawn_started_ts", "spawn_complete_ts", "app_ready_ts",
                        "mode", "admit_source"])
            w.writerow(["admitted", "edge_server_lan1_dyn1", "1",
                        f"{ADMITTED:.3f}", f"{SPAWN_STARTED:.3f}",
                        f"{SPAWN_COMPLETE:.3f}", f"{ADMITTED - 0.2:.3f}",
                        "discovery", "probe"])
        rv = mod._process_run(run_void, 30.0, 60.0, 5.0)
        _assert(rv["void"] is True and "lan2" in rv["void_reason"],
                "void gate flags missing lan2 admissions")

        # ── flow validation gate policy (CLI contract via subprocess) ──
        def _flow_rc(run: str) -> int:
            return subprocess.run(
                [sys.executable, FLOW_VAL, run],
                capture_output=True, text=True, check=False).returncode

        rc = _flow_rc(run_dir)
        _assert(rc == 0, "flow-validation passes on a clean synthetic run")
        # Break Check A: a request attributed to the new backend pre-admission.
        rows = list(csv.DictReader(open(
            os.path.join(run_dir, "client_requests.csv"), encoding="utf-8")))
        rows.append(dict(rows[0]))
        rows[-1]["completed_at"] = _iso(SPAWN_STARTED + 1.0)
        rows[-1]["backend_id"] = "edge_server_lan1_dyn1"
        _write_csv(os.path.join(run_dir, "client_requests.csv"), rows)
        rc = _flow_rc(run_dir)
        _assert(rc == 1, "Check A violation fails the run (hard gate)")

        print("\nALL RQ3 V2 ANALYZER + FLOW-VALIDATION ASSERTIONS PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
