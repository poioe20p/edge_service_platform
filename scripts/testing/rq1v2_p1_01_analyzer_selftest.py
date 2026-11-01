#!/usr/bin/env python3
"""RQ1 v2 — analyzer self-test (Phase 1 gate).

Builds synthetic RQ1 run folders and asserts the reworked
``rq1_delivery_per_run.py`` behaves per the RQ1 v2 contract:

  A. Status-aware service quality: timeout / dropped / canceled rows are
     counted in offered, excluded from latency and failure; failure = completed
     & http_status != 200 (completed-only denominator); timeout_rate =
     status=timeout / offered.
  B. Generator phase-label bucketing is used for client-side service quality
     (not anchored boundaries).
  C. sampled_push arm maps to run_meta arm="sp".
  D. Phase-boundary validation: anchored-vs-generator mismatch > 1 window
     hard-fails the run; --skip-phase-validation bypasses it.

Exit non-zero on any assertion failure.
"""

import csv
import json
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
_ANALYZER = os.path.join(
    _ROOT, "docs", "operation", "testing", "experiment", "v2", "rq1",
    "analysis", "rq1_delivery_per_run.py")

_PHASES = [
    {"name": "baseline", "duration_s": 10.0},
    {"name": "compute_plateau", "duration_s": 20.0},
    {"name": "recovery_gap", "duration_s": 10.0},
    {"name": "demand_drop", "duration_s": 10.0},
]
# Anchored boundaries from traffic_start=100.0:
# baseline [100,110) plateau [110,130) recovery [130,140) demand [140,150)
_TRAFFIC_START = 100.0


def _write_csv(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def _build_run(run_dir, telem_source, plateau_shift=0.0, bad_phase=False):
    """Populate a synthetic run folder; returns nothing (raises on error)."""
    os.makedirs(run_dir, exist_ok=True)

    # client_requests.csv — status-aware rows across phases.
    req_rows = [
        # baseline (2 completed ok)
        {"sent_at": "100.5", "phase": "baseline", "client_lan": "lan1",
         "http_status": "200", "latency_s": "0.05", "status": "completed"},
        {"sent_at": "100.8", "phase": "baseline", "client_lan": "lan2",
         "http_status": "200", "latency_s": "0.06", "status": "completed"},
    ]
    base = 110.0 + plateau_shift
    req_rows += [
        # compute_plateau: 2 ok + 1 failure + 1 timeout + 1 dropped + 1 canceled
        {"sent_at": f"{base + 0.5:.1f}", "phase": "compute_plateau",
         "client_lan": "lan1", "http_status": "200", "latency_s": "0.10",
         "status": "completed"},
        {"sent_at": f"{base + 1.0:.1f}", "phase": "compute_plateau",
         "client_lan": "lan2", "http_status": "200", "latency_s": "0.20",
         "status": "completed"},
        {"sent_at": f"{base + 1.5:.1f}", "phase": "compute_plateau",
         "client_lan": "lan1", "http_status": "500", "latency_s": "3.0",
         "status": "completed"},
        {"sent_at": f"{base + 2.0:.1f}", "phase": "compute_plateau",
         "client_lan": "lan2", "http_status": "000", "latency_s": "30.0",
         "status": "timeout"},
        {"sent_at": f"{base + 2.5:.1f}", "phase": "compute_plateau",
         "client_lan": "lan1", "http_status": "", "latency_s": "",
         "status": "dropped"},
        {"sent_at": f"{base + 3.0:.1f}", "phase": "compute_plateau",
         "client_lan": "lan2", "http_status": "", "latency_s": "",
         "status": "canceled"},
    ]
    req_rows += [
        {"sent_at": "130.5", "phase": "recovery_gap", "client_lan": "lan1",
         "http_status": "200", "latency_s": "0.05", "status": "completed"},
        {"sent_at": "140.5", "phase": "demand_drop", "client_lan": "lan1",
         "http_status": "200", "latency_s": "0.05", "status": "completed"},
    ]
    # Optional deliberately mis-ordered row (tests validation only when used):
    # a recovery_gap request sent before the plateau's first request breaks
    # the monotonic phase-order invariant.
    if bad_phase:
        req_rows.append({"sent_at": "105.0", "phase": "recovery_gap",
                         "client_lan": "lan1", "http_status": "200",
                         "latency_s": "0.05", "status": "completed"})
    _write_csv(os.path.join(run_dir, "client_requests.csv"),
               ["sent_at", "phase", "client_lan", "http_status", "latency_s",
                "status"], req_rows)

    # phases_snapshot.json
    with open(os.path.join(run_dir, "phases_snapshot.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"phases": _PHASES}, fh)

    # controller_env_snapshot.env
    with open(os.path.join(run_dir, "controller_env_snapshot.env"), "w",
              encoding="utf-8") as fh:
        fh.write(f"TELEMETRY_SOURCE={telem_source}\n")
        fh.write("CONTROL_TICK_S=10\n")

    # Minimal window/delivery/decision/ack artifacts (existence-gated).
    for lan in ("lan1", "lan2"):
        with open(os.path.join(run_dir, f"window_log_{lan}.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write("")
        _write_csv(os.path.join(run_dir, f"telemetry_delivery_log_{lan}.csv"),
                   ["network_id", "window_seq", "window_id", "window_end",
                    "delivery_ts", "delay_s", "mode", "release_ts"], [])
        _write_csv(os.path.join(run_dir, f"decision_log_{lan}.csv"),
                   ["ts", "network_id", "window_id", "action_type", "action"], [])
        with open(os.path.join(run_dir, f"ack_log_{lan}.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write("")


def _run_analyzer(run_dir, extra=None):
    cmd = [sys.executable, _ANALYZER, run_dir] + (extra or [])
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def _read_sq(run_dir):
    path = os.path.join(run_dir, "analysis", "rq1_delivery",
                        "phase_service_quality.csv")
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _read_run_meta(run_dir):
    path = os.path.join(run_dir, "analysis", "rq1_delivery", "run_meta.csv")
    out = {}
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            out[r["key"]] = r["value"]
    return out


def main() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        if cond:
            print(f"  [PASS] {name}")
        else:
            ok = False
            print(f"  [FAIL] {name} {detail}")

    tmp = tempfile.mkdtemp(prefix="rq1_analyzer_selftest_")

    # --- A/B: status-aware service quality + phase-label bucketing (ep) -----
    ep_dir = os.path.join(tmp, "run_ep")
    _build_run(ep_dir, telem_source="event_preserving")
    rc, out, err = _run_analyzer(ep_dir)
    check("A1: analyzer exits 0 on a clean synthetic run", rc == 0,
          f"rc={rc} err={err[:300]}")
    sq = _read_sq(ep_dir)
    plateau = [r for r in sq if r["phase"] == "compute_plateau"]
    check("A2: compute_plateau rows present (both LANs)",
          len(plateau) == 2, f"n={len(plateau)}")
    p1 = plateau[0]  # lan1: 1 ok + 1 failure + 1 dropped
    p2 = plateau[1]  # lan2: 1 ok + 1 timeout + 1 canceled
    check("A3: offered == all rows in phase (3 per LAN)",
          p1["offered"] == "3" and p2["offered"] == "3",
          f"lan1={p1['offered']} lan2={p2['offered']}")
    check("A4: completed == ok + failure (lan1 2, lan2 1)",
          p1["completed"] == "2" and p2["completed"] == "1",
          f"lan1={p1['completed']} lan2={p2['completed']}")
    check("A5: failure_count == completed & !=200 (lan1 1, lan2 0)",
          p1["failure_count"] == "1" and p2["failure_count"] == "0",
          f"lan1={p1['failure_count']} lan2={p2['failure_count']}")
    check("A6: failure_rate over completed-only (lan1 1/2, lan2 0)",
          p1["failure_rate"] == "0.5000" and p2["failure_rate"] == "0.0000",
          f"lan1={p1['failure_rate']} lan2={p2['failure_rate']}")
    check("A7: timeout on lan2 (count 1, rate 1/2 of offered-canceled, canceled "
          "excluded); none on lan1",
          p2["timeout_count"] == "1" and p2["timeout_rate"] == "0.5000"
          and p1["timeout_count"] == "0" and p1["timeout_rate"] == "0.0000",
          f"lan2 t={p2['timeout_count']} r={p2['timeout_rate']}; "
          f"lan1 t={p1['timeout_count']} r={p1['timeout_rate']}")
    check("A8: dropped (lan1) / canceled (lan2) separate, never failures",
          p1["dropped_count"] == "1" and p1["canceled_count"] == "0"
          and p2["dropped_count"] == "0" and p2["canceled_count"] == "1",
          f"lan1 d={p1['dropped_count']} c={p1['canceled_count']}; "
          f"lan2 d={p2['dropped_count']} c={p2['canceled_count']}")
    check("A9: latency percentiles over completed+ok only (lan1 p50=0.10,"
          " lan2 p50=0.20)", p1["p50"] == "0.100" and p2["p50"] == "0.200",
          f"lan1={p1['p50']} lan2={p2['p50']}")
    check("B1: phase-label bucketing used (no non-empty 'transition' bucket)",
          all(r["phase"] in ("baseline", "compute_plateau", "recovery_gap",
                             "demand_drop") or r["offered"] == "0"
              for r in sq),
          f"phases={sorted({r['phase'] for r in sq})}")

    # --- C: sampled_push arm mapping -----------------------------------------
    sp_dir = os.path.join(tmp, "run_sp")
    _build_run(sp_dir, telem_source="sampled_push")
    rc, _, err = _run_analyzer(sp_dir)
    check("C1: sampled_push run analyzes clean", rc == 0, f"err={err[:300]}")
    meta = _read_run_meta(sp_dir)
    check("C2: run_meta arm == 'sp' for sampled_push",
          meta.get("arm") == "sp", f"arm={meta.get('arm')}")
    check("C3: run_meta telem_source == 'sampled_push'",
          meta.get("telem_source") == "sampled_push",
          f"src={meta.get('telem_source')}")

    # --- D: phase-boundary validation ----------------------------------------
    bad_dir = os.path.join(tmp, "run_bad")
    _build_run(bad_dir, telem_source="event_preserving", bad_phase=True)
    rc, _, err = _run_analyzer(bad_dir)
    check("D1: mis-ordered phase label (recovery_gap at t=105 before plateau "
          "at t=110) hard-fails", rc == 1 and "not ordered" in err,
          f"rc={rc} err={err[:300]}")
    rc, _, err = _run_analyzer(bad_dir, extra=["--skip-phase-validation"])
    check("D2: --skip-phase-validation bypasses the check (rc=0)", rc == 0,
          f"rc={rc} err={err[:300]}")

    # Shifted-but-consistent plateau (within tolerance) must NOT fail.
    shift_dir = os.path.join(tmp, "run_shift")
    _build_run(shift_dir, telem_source="event_preserving", plateau_shift=5.0)
    rc, _, err = _run_analyzer(shift_dir)
    check("D3: plateau shifted by 5 s (< 1 window) still validates (rc=0)",
          rc == 0, f"rc={rc} err={err[:300]}")

    print()
    if ok:
        print("RQ1 ANALYZER SELF-TEST PASSED (status-aware service quality, "
              "phase-label bucketing, sampled_push arm, phase validation)")
        return 0
    print("RQ1 ANALYZER SELF-TEST FAILED — see [FAIL] lines above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
